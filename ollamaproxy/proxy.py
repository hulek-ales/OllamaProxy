"""Vlastní proxy: streamuje odpovědi beze změny, cestou sbírá metriky.

Cesty:
  /providers/<slug>/...   komerční nebo další upstream z tabulky providers (vždy s proxy klíčem)
  /healthz                stav bez autentizace
  /...                    holé Ollama API (OLLAMA_UPSTREAM); klíč volitelně dle nastavení
"""

import json
import re
import time
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .auth import principal_from_request, uses_proxy_key
from .collector import Collector
from .db import db, now_iso
from .providers import auth_headers, cost_usd, parse_pricing
from .telemetry import active, bump, gpu_snapshot, host_snapshot

router = APIRouter()

ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# hlavičky klienta, které se do upstreamu nikdy nepřeposílají
STRIP_REQUEST = {"host", "content-length", "cookie"}
CLIENT_AUTH = {"authorization", "x-api-key", "x-goog-api-key"}

# POST na tyto cesty se logují jako inference
INFERENCE_RE = re.compile(
    r"(/api/generate|/api/chat|/chat/completions|/completions|/messages|/responses"
    r"|:generateContent|:streamGenerateContent)$"
)


def strip_hop_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in config.HOP_HEADERS}


def client_ip(request: Request):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def client_user(request: Request):
    """Uživatel z Open WebUI (ENABLE_FORWARD_USER_INFO_HEADERS=true): jméno, jinak e-mail.
    Jméno posílá Open WebUI URL-encoded (`quote(name, safe=" ")`), proto unquote."""
    for h in ("x-openwebui-user-name", "x-openwebui-user-email"):
        v = request.headers.get(h)
        if v:
            return unquote(v).strip()[:120]
    return None


def _unauthorized(msg="missing or invalid proxy API key"):
    return JSONResponse({"error": msg}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def inject_stream_usage(kind: str, path: str, body: bytes) -> bytes:
    """OpenAI posílá usage ve streamu jen na vyžádání — doplníme stream_options."""
    if kind != "openai" or not (path.endswith("/chat/completions") or path.endswith("/completions")):
        return body
    try:
        obj = json.loads(body)
    except Exception:
        return body
    if not isinstance(obj, dict) or not obj.get("stream"):
        return body
    opts = obj.get("stream_options")
    if not isinstance(opts, dict):
        opts = {}
    if "include_usage" in opts:
        return body
    opts["include_usage"] = True
    obj["stream_options"] = opts
    return json.dumps(obj).encode()


async def forward(request: Request, url: str, headers: dict, body: bytes, *,
                  provider: str, principal, pricing: dict = None,
                  params=None, telemetry: bool = False):
    client: httpx.AsyncClient = request.app.state.client
    path = request.url.path
    should_log = request.method == "POST" and INFERENCE_RE.search(path) is not None
    log_bodies = db.setting("log_bodies") == "1"

    req_obj = {}
    if should_log:
        try:
            parsed = json.loads(body) if body else {}
            req_obj = parsed if isinstance(parsed, dict) else {}
        except Exception:
            req_obj = {}
    req_model = req_obj.get("model") if isinstance(req_obj.get("model"), str) else None

    base_row = {
        "endpoint": path,
        "provider": provider,
        "key_name": principal.name if principal is not None else None,
        "client_ip": client_ip(request),
        "client_user": client_user(request),
        "request_json": json.dumps(req_obj, ensure_ascii=False) if (should_log and log_bodies) else None,
    }

    snap = {}
    started = time.time()
    if should_log:
        concurrent = bump(1)
        snap = host_snapshot()
        if telemetry:
            snap.update(await gpu_snapshot(client, config.UPSTREAM, req_model))
        snap["concurrent"] = concurrent

    upstream_req = client.build_request(request.method, url, content=body, headers=headers,
                                        params=params)
    try:
        upstream = await client.send(upstream_req, stream=True)
    except Exception as exc:
        if should_log:
            bump(-1)
            db.log_request({
                "ts": now_iso(), "model": req_model, "status": 502,
                "wall_time_ms": (time.time() - started) * 1000,
                "error": "upstream unreachable: " + str(exc), **base_row, **snap,
            })
        return JSONResponse({"error": "upstream unreachable: " + str(exc)}, status_code=502)

    collector = Collector(log_bodies) if should_log else None

    async def gen():
        try:
            async for chunk in upstream.aiter_bytes():
                if collector is not None:
                    collector.feed(chunk)
                yield chunk
            if collector is not None:
                collector.finish()
        finally:
            await upstream.aclose()
            if collector is not None:
                bump(-1)
                pt, ct = collector.prompt_tokens, collector.completion_tokens
                model = collector.model or req_model
                error = collector.error
                if error is None and upstream.status_code >= 400:
                    error = "HTTP " + str(upstream.status_code)
                db.log_request({
                    "ts": now_iso(),
                    "model": model,
                    "status": upstream.status_code,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_duration_ms": collector.total_ns / 1e6 if collector.total_ns else None,
                    "eval_duration_ms": collector.eval_ns / 1e6 if collector.eval_ns else None,
                    "tokens_per_sec": collector.tokens_per_sec(),
                    "wall_time_ms": (time.time() - started) * 1000,
                    "response_text": collector.text() if log_bodies else None,
                    "cost_usd": cost_usd(pricing, model, pt, ct) if pricing else None,
                    "error": error,
                    **base_row, **snap,
                })

    return StreamingResponse(gen(), status_code=upstream.status_code,
                             headers=strip_hop_headers(upstream.headers))


# ----------------------------------------------------------------- routy

@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request):
    snap = host_snapshot()
    snap.update(await gpu_snapshot(request.app.state.client, config.UPSTREAM))
    snap["concurrent"] = active()
    snap["upstream"] = config.UPSTREAM
    snap["version"] = config.VERSION
    snap["commit"] = config.GIT_COMMIT
    return snap


@router.api_route("/providers/{slug}/{path:path}", methods=ALL_METHODS, include_in_schema=False)
async def provider_proxy(slug: str, path: str, request: Request):
    prov = db.get_provider(slug)
    if prov is None or not prov["enabled"]:
        return JSONResponse({"error": "provider '" + slug + "' is not configured"}, status_code=404)
    principal = principal_from_request(request, db)
    if principal is None:
        return _unauthorized()
    if not principal.may_use(slug):
        return JSONResponse({"error": "this key may not use provider '" + slug + "'"}, status_code=403)

    body = await request.body()
    incoming = {k: v for k, v in request.headers.items()
                if k.lower() not in STRIP_REQUEST and k.lower() not in CLIENT_AUTH}
    headers = auth_headers(prov["kind"], prov["api_key"], incoming)
    params = dict(request.query_params)
    if prov["kind"] == "google":
        params.pop("key", None)
    if prov["inject_usage"]:
        body = inject_stream_usage(prov["kind"], path, body)
    try:
        pricing = parse_pricing(prov["pricing_json"])
    except Exception:
        pricing = {}
    url = prov["base_url"].rstrip("/") + "/" + path
    return await forward(request, url, headers, body, provider=slug, principal=principal,
                         pricing=pricing, params=params, telemetry=False)


@router.api_route("/{path:path}", methods=ALL_METHODS, include_in_schema=False)
async def ollama_proxy(path: str, request: Request):
    principal = principal_from_request(request, db)
    if db.setting("ollama_require_key") == "1" and principal is None:
        return _unauthorized()
    body = await request.body()
    skip = set(STRIP_REQUEST)
    if uses_proxy_key(request):
        skip |= CLIENT_AUTH  # náš klíč do Ollamy neposílat
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    url = config.UPSTREAM + "/" + path
    return await forward(request, url, headers, body, provider="ollama", principal=principal,
                         params=request.query_params, telemetry=True)
