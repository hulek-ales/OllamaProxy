"""Vlastní proxy: streamuje odpovědi beze změny, cestou sbírá metriky.

Cesty:
  /providers/<slug>/...   komerční nebo další upstream z tabulky providers (vždy s proxy klíčem)
  /healthz                stav bez autentizace
  /...                    holé Ollama API (OLLAMA_UPSTREAM); klíč volitelně dle nastavení

Klíč může mít seznam povolených modelů: dotaz na jiný model dostane 403 a
seznamy modelů (/api/tags, /v1/models) se mu ořežou. Inference na lokální
Ollamě navíc prochází plánovačem modelů (scheduler.py); hlavička
`X-Opx-Wait: <s>` omezí, jak dlouho smí dotaz čekat na uvolnění GPU (0 = vůbec,
pak 503 + Retry-After).
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
from .scheduler import sched
from .telemetry import active, bump, gpu_snapshot, host_snapshot

router = APIRouter()

ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# hlavičky klienta, které se do upstreamu nikdy nepřeposílají
STRIP_REQUEST = {"host", "content-length", "cookie", "x-opx-wait"}
CLIENT_AUTH = {"authorization", "x-api-key", "x-goog-api-key"}
WAIT_HEADER = "x-opx-wait"

# POST na tyto cesty se logují jako inference
INFERENCE_RE = re.compile(
    r"(/api/generate|/api/chat|/chat/completions|/completions|/messages|/responses"
    r"|:generateContent|:streamGenerateContent)$"
)
# POST na tyto cesty lokální Ollamy jdou přes plánovač modelů (embeddingy model taky nahrávají)
SCHED_RE = re.compile(r"(/api/generate|/api/chat|/api/embed|/api/embeddings"
                      r"|/chat/completions|/completions|/embeddings)$")
# Gemini má model v cestě: /v1beta/models/gemini-x:generateContent
GEMINI_MODEL_RE = re.compile(r"/models/([^/:]+):")
# seznamy modelů, které se klíči s omezením ořežou
LIST_PATHS = ("/api/tags", "/v1/models", "/v1beta/models")


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


def parse_body(body: bytes) -> dict:
    try:
        obj = json.loads(body) if body else {}
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def request_model(path: str, body_obj: dict):
    """Model, na který dotaz míří: pole `model` v JSON těle, u Gemini z cesty."""
    m = body_obj.get("model")
    if isinstance(m, str) and m:
        return m.strip()
    found = GEMINI_MODEL_RE.search(path)
    if found:
        return found.group(1)
    return None


def wait_limit(request: Request):
    """X-Opx-Wait: kolik sekund smí dotaz čekat v plánovači; None = bez limitu."""
    raw = request.headers.get(WAIT_HEADER)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def filter_model_list(path: str, payload, allowed) -> object:
    """Ořeže odpověď /api/tags, /v1/models nebo /v1beta/models na modely, které klíč smí."""
    if not isinstance(payload, dict):
        return payload
    if path.endswith("/api/tags") and isinstance(payload.get("models"), list):
        payload["models"] = [m for m in payload["models"] if isinstance(m, dict)
                             and allowed(m.get("name") or m.get("model") or "")]
    elif path.endswith("/v1/models") and isinstance(payload.get("data"), list):
        payload["data"] = [m for m in payload["data"] if isinstance(m, dict) and allowed(m.get("id") or "")]
    elif path.endswith("/v1beta/models") and isinstance(payload.get("models"), list):
        payload["models"] = [m for m in payload["models"] if isinstance(m, dict)
                             and allowed((m.get("name") or "").replace("models/", ""))]
    return payload


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


def _base_row(request: Request, provider: str, principal, req_obj: dict, log_bodies: bool) -> dict:
    return {
        "endpoint": request.url.path,
        "provider": provider,
        "key_name": principal.name if principal is not None else None,
        "client_ip": client_ip(request),
        "client_user": client_user(request),
        "request_json": json.dumps(req_obj, ensure_ascii=False) if log_bodies else None,
    }


def deny_model(request: Request, provider: str, principal, model: str, req_obj: dict):
    """403 pro model mimo seznam klíče; inference se zapíše do logu, ať je vidět, kdo to zkoušel."""
    msg = "model '" + model + "' is not allowed for key '" + principal.name + "'"
    if request.method == "POST" and INFERENCE_RE.search(request.url.path):
        db.log_request({
            "ts": now_iso(), "model": model, "status": 403, "wall_time_ms": 0.0, "error": msg,
            **_base_row(request, provider, principal, req_obj, db.setting("log_bodies") == "1"),
        })
    return JSONResponse({"error": msg}, status_code=403)


async def forward(request: Request, url: str, headers: dict, body: bytes, *,
                  provider: str, principal, pricing: dict = None,
                  params=None, telemetry: bool = False, req_obj: dict = None,
                  sched_model: str = None, model_filter=None):
    """Přepošle dotaz a odpověď streamuje dál. `sched_model` = jít přes plánovač modelů,
    `model_filter` = ořezat seznam modelů v odpovědi (callable název → bool)."""
    client: httpx.AsyncClient = request.app.state.client
    path = request.url.path
    should_log = request.method == "POST" and INFERENCE_RE.search(path) is not None
    log_bodies = db.setting("log_bodies") == "1"

    if req_obj is None:
        req_obj = parse_body(body) if should_log else {}
    req_model = req_obj.get("model") if isinstance(req_obj.get("model"), str) else None

    base_row = _base_row(request, provider, principal, req_obj, should_log and log_bodies)

    started = time.time()
    queue_ms = None
    if sched_model:
        try:
            queue_ms = round(await sched.acquire(sched_model, timeout=wait_limit(request)) * 1000)
        except TimeoutError:
            snap = sched.snapshot()
            msg = ("model '" + sched_model + "' is not loaded; GPU is held by '"
                   + str(snap["admitted"]) + "'")
            if should_log:
                db.log_request({"ts": now_iso(), "model": req_model, "status": 503,
                                "wall_time_ms": (time.time() - started) * 1000, "error": msg,
                                "queue_ms": round((time.time() - started) * 1000), **base_row})
            return JSONResponse({"error": msg, "scheduler": snap}, status_code=503,
                                headers={"Retry-After": str(int(max(1, min(30, snap["hold_s"]))))})

    snap = {}
    if should_log:
        concurrent = bump(1)
        snap = host_snapshot()
        if telemetry:
            snap.update(await gpu_snapshot(client, config.UPSTREAM, req_model))
        snap["concurrent"] = concurrent
        snap["queue_ms"] = queue_ms

    upstream_req = client.build_request(request.method, url, content=body, headers=headers,
                                        params=params)
    try:
        upstream = await client.send(upstream_req, stream=True)
    except Exception as exc:
        if sched_model:
            sched.release(sched_model)
        if should_log:
            bump(-1)
            db.log_request({
                "ts": now_iso(), "model": req_model, "status": 502,
                "wall_time_ms": (time.time() - started) * 1000,
                "error": "upstream unreachable: " + str(exc), **base_row, **snap,
            })
        return JSONResponse({"error": "upstream unreachable: " + str(exc)}, status_code=502)

    if model_filter is not None and upstream.status_code == 200:
        # seznam modelů: přečíst celý, ořezat, poslat jako JSON
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        try:
            payload = json.loads(raw)
        except Exception:
            return StreamingResponse(iter([raw]), status_code=200, headers=strip_hop_headers(upstream.headers))
        return JSONResponse(filter_model_list(path, payload, model_filter))

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
            if sched_model:
                sched.release(sched_model)
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
    snap["scheduler"] = sched.snapshot()
    return snap


def _model_filter(principal):
    if principal is None or not principal.models:
        return None
    return principal.may_model


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
    req_obj = parse_body(body) if request.method == "POST" else {}
    model = request_model("/" + path, req_obj)
    if model and not principal.may_model(model):
        return deny_model(request, slug, principal, model, req_obj)

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
    model_filter = _model_filter(principal) if request.method == "GET" and ("/" + path).endswith(LIST_PATHS) else None
    return await forward(request, url, headers, body, provider=slug, principal=principal,
                         pricing=pricing, params=params, telemetry=False, req_obj=req_obj,
                         model_filter=model_filter)


@router.api_route("/{path:path}", methods=ALL_METHODS, include_in_schema=False)
async def ollama_proxy(path: str, request: Request):
    principal = principal_from_request(request, db)
    if db.setting("ollama_require_key") == "1" and principal is None:
        return _unauthorized()
    body = await request.body()
    req_obj = parse_body(body) if request.method == "POST" else {}
    model = request_model("/" + path, req_obj)
    if model and principal is not None and not principal.may_model(model):
        return deny_model(request, "ollama", principal, model, req_obj)

    skip = set(STRIP_REQUEST)
    if uses_proxy_key(request):
        skip |= CLIENT_AUTH  # náš klíč do Ollamy neposílat
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    url = config.UPSTREAM + "/" + path
    sched_model = model if request.method == "POST" and SCHED_RE.search("/" + path) else None
    model_filter = _model_filter(principal) if request.method == "GET" and ("/" + path).endswith(LIST_PATHS) else None
    return await forward(request, url, headers, body, provider="ollama", principal=principal,
                         params=request.query_params, telemetry=True, req_obj=req_obj,
                         sched_model=sched_model, model_filter=model_filter)
