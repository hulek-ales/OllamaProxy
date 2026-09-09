"""JSON API pro správu a ladění — /mgmt/v1/…  (dokumentace na /mgmt/docs).

Autentizace: `Authorization: Bearer opx_…` (klíč z GUI) nebo přihlášená session.
Čtení logu a statistik smí každý platný klíč; správa poskytovatelů, klíčů a
nastavení jen klíč s rolí admin (nebo session).
"""

import asyncio
import json
import secrets
import time
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import config
from .auth import hash_key, new_api_key, parse_model_patterns, principal_from_request
from .db import PROVIDER_KINDS, SETTING_DEFAULTS, db, parse_since
from .jobs import ALLOWED_JOB_PATHS, job_view, key_limit, limiter, worker
from .providers import client_base_url, fetch_models, mask_key, parse_pricing
from .scheduler import sched
from .telemetry import active, gpu_snapshot, host_snapshot

router = APIRouter(prefix="/mgmt/v1", tags=["mgmt"])


def require(request: Request, admin: bool = False):
    p = principal_from_request(request, db)
    if p is None:
        raise HTTPException(401, "missing or invalid proxy API key",
                            headers={"WWW-Authenticate": "Bearer"})
    if admin and not p.is_admin:
        raise HTTPException(403, "admin role required")
    return p


def proxy_root(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return proto + "://" + host


def provider_view(row: dict, request: Request) -> dict:
    return {
        "slug": row["slug"],
        "name": row["name"],
        "kind": row["kind"],
        "base_url": row["base_url"],
        "api_key_masked": mask_key(row["api_key"]),
        "has_key": bool(row["api_key"]),
        "pricing": parse_pricing(row["pricing_json"]) if row["pricing_json"] else {},
        "inject_usage": bool(row["inject_usage"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "client_base_url": client_base_url(proxy_root(request), row["slug"], row["kind"]),
    }


# ------------------------------------------------------------------ stav

@router.get("/health")
async def health(request: Request):
    require(request)
    snap = host_snapshot()
    snap.update(await gpu_snapshot(request.app.state.client, config.UPSTREAM))
    snap["concurrent"] = active()
    snap["upstream"] = config.UPSTREAM
    snap["version"] = config.VERSION
    snap["commit"] = config.GIT_COMMIT
    snap["db_bytes"] = db.size_bytes()
    snap["scheduler"] = sched.snapshot()
    snap["jobs"] = worker.snapshot()
    return snap


# ------------------------------------------------------------------- log

@router.get("/requests")
async def list_requests(request: Request, limit: int = 100, offset: int = 0,
                        model: Optional[str] = None, provider: Optional[str] = None,
                        placement: Optional[str] = None, status: Optional[str] = None,
                        key: Optional[str] = None, user: Optional[str] = None,
                        since: Optional[str] = None, q: Optional[str] = None, bodies: int = 0):
    """Seznam dotazů, nejnovější první. `since` = 24h / 7d / ISO datum, `status` = číslo nebo `error`."""
    require(request)
    limit = max(1, min(limit, 500))
    filters = {"model": model, "provider": provider, "placement": placement,
               "status": status, "key_name": key, "user": user, "since": since, "q": q}
    rows, total = db.query_requests(filters, limit=limit, offset=max(0, offset),
                                    with_bodies=bool(bodies))
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.get("/requests/{rid}")
async def get_request(rid: int, request: Request):
    require(request)
    row = db.get_request(rid)
    if row is None:
        raise HTTPException(404, "no such request")
    return row


@router.get("/stats")
async def stats(request: Request, since: Optional[str] = "24h"):
    """Součty a průměry: celkem, po poskytovatelích, modelech, umístění, klíčích, uživatelích
    Open WebUI a kombinaci klíč × model (`by_key_model`)."""
    require(request)
    return db.stats(since)


# ---------------------------------------------------------------- modely

@router.get("/models")
async def models(request: Request):
    """Modely dostupné přes proxy: lokální Ollama + každý zapnutý poskytovatel."""
    p = require(request)
    client = request.app.state.client
    out = {}
    try:
        out["ollama"] = {"ok": True, "base_url": proxy_root(request),
                         "models": [m for m in await fetch_models(client, "ollama", config.UPSTREAM, "")
                                    if p.may_model(m)]}
    except Exception as exc:
        out["ollama"] = {"ok": False, "error": str(exc), "models": []}
    for prov in db.list_providers():
        if not prov["enabled"] or not p.may_use(prov["slug"]):
            continue
        entry = {"kind": prov["kind"],
                 "base_url": client_base_url(proxy_root(request), prov["slug"], prov["kind"])}
        try:
            entry["models"] = [m for m in await fetch_models(client, prov["kind"], prov["base_url"], prov["api_key"])
                               if p.may_model(m)]
            entry["ok"] = True
        except Exception as exc:
            entry.update({"ok": False, "error": str(exc), "models": []})
        out[prov["slug"]] = entry
    return out


# ------------------------------------------------- plánovač lokální Ollamy

class LoadIn(BaseModel):
    model: str = Field(min_length=1)
    wait_s: float = Field(default=0, ge=0, le=300)   # jak dlouho smí volání blokovat
    keep_alive: Optional[str] = None                 # předá se Ollamě (např. "30m", "-1")


_loading = {}      # model → task, který ho v Ollamě nahrává (drží místo v plánovači)
_interest = {}     # model → (first_ts, last_poll_ts): pořadí ve frontě přes opakované dotazy


async def _load_in_ollama(client, model: str, keep_alive):
    """Prázdný /api/generate = Ollama model jen nahraje. Místo v plánovači se vrátí po doběhnutí."""
    try:
        body = {"model": model, "prompt": "", "stream": False}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = await client.post(config.UPSTREAM + "/api/generate", json=body, timeout=600.0)
        r.raise_for_status()
        return None
    except Exception as exc:
        return str(exc)
    finally:
        sched.release(model)
        _loading.pop(model, None)


def _load_view(model: str, loaded: bool, status: str, error=None) -> dict:
    snap = sched.snapshot()
    return {"model": model, "loaded": loaded, "status": status, "error": error,
            "admitted": snap["admitted"], "in_flight": snap["in_flight"], "waiting": snap["waiting"],
            "hold_s": snap["hold_s"], "scheduler_enabled": snap["enabled"]}


@router.get("/models/status")
async def models_status(request: Request):
    """Co Ollama drží v paměti a co dělá plánovač (fronta po modelech, kdo má GPU)."""
    require(request)
    snap = sched.snapshot()
    snap["loaded"] = sorted(await sched.loaded(force=True))
    snap["jobs"] = worker.snapshot()
    return snap


@router.post("/models/load")
async def models_load(body: LoadIn, request: Request):
    """Požádá o načtení modelu do Ollamy. Vrátí `loaded: true`, když je model na GPU a
    dotazy na něj jdou hned; `false` se `status` `queued` (GPU drží jiný model, jsme ve frontě)
    nebo `loading` (Ollama ho zrovna nahrává). `wait_s` = kolik sekund smí volání blokovat;
    opakuj dotaz, dokud není `loaded: true`, a pak pošli inference do `hold_s` sekund."""
    p = require(request)
    model = body.model.strip()
    if not p.may_model(model):
        raise HTTPException(403, "model '" + model + "' is not allowed for this key")
    deadline = time.monotonic() + body.wait_s

    task = _loading.get(model)
    if task is None:
        now = time.monotonic()
        first, last = _interest.get(model, (now, now))
        since = first if now - last < 60 else now
        _interest[model] = (since, now)
        try:
            await sched.acquire(model, timeout=body.wait_s, since=since)
        except TimeoutError:
            return _load_view(model, False, "queued")
        _interest.pop(model, None)
        task = _loading.get(model)
        if task is not None:                      # mezitím ho začal nahrávat souběžný dotaz
            sched.release(model)
        elif model in await sched.loaded(force=True):
            sched.release(model)
            return _load_view(model, True, "ready")
        else:
            task = asyncio.get_running_loop().create_task(
                _load_in_ollama(request.app.state.client, model, body.keep_alive))
            _loading[model] = task

    remaining = max(0.0, deadline - time.monotonic())
    try:
        error = await asyncio.wait_for(asyncio.shield(task), remaining)
    except asyncio.TimeoutError:
        return _load_view(model, False, "loading")
    if error:
        return _load_view(model, False, "error", error)
    return _load_view(model, True, "ready")


# ------------------------------------------------------------- fronta úloh

class JobIn(BaseModel):
    path: str = "/api/chat"                    # kam by šel průchozí dotaz (bez /providers/<slug>)
    body: dict                                 # tělo dotazu ve formátu daného API
    provider: str = "ollama"                   # 'ollama' = lokální, jinak slug poskytovatele
    priority: int = Field(default=5, ge=0, le=9)
    callback_url: Optional[str] = None         # proxy sem po dokončení pošle výsledek (POST JSON)
    not_before: Optional[str] = None           # ISO čas, dřív se úloha nespustí


class JobsIn(BaseModel):
    jobs: list                                  # seznam JobIn
    priority: Optional[int] = Field(default=None, ge=0, le=9)
    callback_url: Optional[str] = None
    not_before: Optional[str] = None


def _check_job(p, j: JobIn):
    path = "/" + j.path.strip("/")
    if path not in ALLOWED_JOB_PATHS:
        raise HTTPException(422, "path must be one of " + ", ".join(ALLOWED_JOB_PATHS))
    provider = (j.provider or "ollama").strip()
    if provider != "ollama":
        prov = db.get_provider(provider)
        if prov is None or not prov["enabled"]:
            raise HTTPException(404, "provider '" + provider + "' is not configured")
        if not p.may_use(provider):
            raise HTTPException(403, "this key may not use provider '" + provider + "'")
    model = j.body.get("model") if isinstance(j.body.get("model"), str) else None
    if provider == "ollama" and not model:
        raise HTTPException(422, "body.model is required")
    if model and not p.may_model(model):
        raise HTTPException(403, "model '" + model + "' is not allowed for this key")
    not_before = None
    if j.not_before:
        not_before = parse_since(j.not_before)
        if not_before is None:
            raise HTTPException(422, "not_before must be an ISO datetime")
    return {"path": path, "provider": provider, "model": model, "not_before": not_before,
            "priority": j.priority, "callback_url": j.callback_url,
            "request_json": json.dumps(j.body, ensure_ascii=False)}


def _enqueue(p, items: list, batch_id: str) -> list:
    key_row = db.get_key(p.key_id) if p.key_id else None
    limit = key_limit(key_row, "max_jobs", "jobs_max_queued")
    if limit and db.count_active_jobs(p.key_id) + len(items) > limit:
        raise HTTPException(429, "too many queued jobs for this key (limit " + str(limit) + ")",
                            headers={"Retry-After": "60"})
    rate = key_limit(key_row, "rate_per_min", "rate_limit_per_min")
    for _ in items:
        retry = limiter.hit(p.key_id, rate)
        if retry is not None:
            raise HTTPException(429, "rate limit " + str(rate) + "/min exceeded",
                                headers={"Retry-After": str(retry)})
    for it in items:
        it.update({"batch_id": batch_id, "key_id": p.key_id, "key_name": p.name})
    ids = db.create_jobs(items)
    worker.wake()
    return ids


@router.post("/jobs", status_code=202)
async def create_jobs(body: Union[JobsIn, JobIn], request: Request):
    """Odložená úloha (nebo dávka `jobs: [...]`). Vrátí `id`/`batch_id`, výsledek pak
    `GET /jobs/{id}` nebo `GET /jobs?batch=…&bodies=1`. Interaktivní dotazy mají přednost,
    úlohy se seskupují podle modelu, aby se model nahrával co nejméně."""
    p = require(request)
    batch_id = secrets.token_hex(6)
    if isinstance(body, JobsIn):
        if not body.jobs:
            raise HTTPException(422, "jobs must not be empty")
        items = []
        for raw in body.jobs:
            try:
                j = JobIn(**raw) if isinstance(raw, dict) else raw
            except Exception as exc:
                raise HTTPException(422, "job: " + str(exc))
            if body.priority is not None:
                j.priority = body.priority
            j.callback_url = j.callback_url or body.callback_url
            j.not_before = j.not_before or body.not_before
            items.append(_check_job(p, j))
        ids = _enqueue(p, items, batch_id)
        return {"batch_id": batch_id, "jobs": [{"id": i, "status": "queued"} for i in ids]}
    ids = _enqueue(p, [_check_job(p, body)], batch_id)
    return {"id": ids[0], "batch_id": batch_id, "status": "queued"}


@router.get("/jobs")
async def list_jobs(request: Request, status: Optional[str] = None, batch: Optional[str] = None,
                    limit: int = 100, offset: int = 0, bodies: int = 0):
    """Seznam úloh (klíč `client` vidí jen svoje). `bodies=1` = i dotaz a výsledek — celá dávka jedním voláním."""
    p = require(request)
    key_id = None if p.is_admin else p.key_id
    rows, total = db.list_jobs(status=status, batch_id=batch, key_id=key_id,
                               limit=max(1, min(limit, 500)), offset=max(0, offset),
                               with_bodies=bool(bodies))
    items = [job_view(r, with_bodies=bool(bodies)) for r in rows]
    done = sum(1 for r in rows if r["status"] in ("done", "error", "cancelled"))
    return {"total": total, "limit": limit, "offset": offset, "finished": done, "items": items}


@router.get("/jobs/{jid}")
async def get_job(jid: int, request: Request):
    """Stav úlohy; po dokončení `result` = celá odpověď upstreamu (bez streamu)."""
    p = require(request)
    job = db.get_job(jid)
    if job is None or (not p.is_admin and job["key_id"] != p.key_id):
        raise HTTPException(404, "no such job")
    return job_view(job, with_bodies=True)


@router.delete("/jobs/{jid}")
async def cancel_job(jid: int, request: Request):
    """Zruší čekající úlohu; běžící se přeruší (vrátí se `status: running`, dokončí ji pracovník)."""
    p = require(request)
    res = db.cancel_job(jid, None if p.is_admin else p.key_id)
    if res == "missing":
        raise HTTPException(404, "no such job")
    if res == "running" and worker.current and worker.current["id"] == jid and worker.current_task:
        worker._preempting = True
        worker.current_task.cancel()
        db.finish_job(jid, "cancelled", error="cancelled by client")
        return {"id": jid, "status": "cancelled"}
    return {"id": jid, "status": res if res != "done" else db.get_job(jid)["status"]}


# ---------------------------------------------------------- poskytovatelé

class ProviderIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = ""
    kind: str
    base_url: str
    api_key: Optional[str] = None       # None = neměnit, "" = smazat
    pricing: dict = Field(default_factory=dict)
    inject_usage: bool = True
    enabled: bool = True


@router.get("/providers")
async def list_providers(request: Request):
    require(request)
    return [provider_view(r, request) for r in db.list_providers()]


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderIn, request: Request):
    require(request, admin=True)
    if body.kind not in PROVIDER_KINDS:
        raise HTTPException(422, "kind must be one of " + ", ".join(PROVIDER_KINDS))
    if body.slug == "ollama":
        raise HTTPException(422, "slug 'ollama' is reserved for the local upstream")
    try:
        pricing = parse_pricing(body.pricing)
    except Exception as exc:
        raise HTTPException(422, "pricing: " + str(exc))
    db.save_provider(body.slug, body.name or body.slug, body.kind, body.base_url,
                     api_key=body.api_key or "", pricing=pricing,
                     inject_usage=body.inject_usage, enabled=body.enabled)
    return provider_view(db.get_provider(body.slug), request)


@router.get("/providers/{slug}")
async def get_provider(slug: str, request: Request):
    require(request)
    row = db.get_provider(slug)
    if row is None:
        raise HTTPException(404, "no such provider")
    return provider_view(row, request)


@router.put("/providers/{slug}")
async def update_provider(slug: str, body: ProviderIn, request: Request):
    require(request, admin=True)
    if db.get_provider(slug) is None:
        raise HTTPException(404, "no such provider")
    if body.slug != slug:
        raise HTTPException(422, "slug cannot be changed")
    if body.kind not in PROVIDER_KINDS:
        raise HTTPException(422, "kind must be one of " + ", ".join(PROVIDER_KINDS))
    try:
        pricing = parse_pricing(body.pricing)
    except Exception as exc:
        raise HTTPException(422, "pricing: " + str(exc))
    db.save_provider(slug, body.name or slug, body.kind, body.base_url, api_key=body.api_key,
                     pricing=pricing, inject_usage=body.inject_usage, enabled=body.enabled)
    return provider_view(db.get_provider(slug), request)


@router.delete("/providers/{slug}", status_code=204)
async def delete_provider(slug: str, request: Request):
    require(request, admin=True)
    if not db.delete_provider(slug):
        raise HTTPException(404, "no such provider")


@router.post("/providers/{slug}/test")
async def test_provider(slug: str, request: Request):
    require(request, admin=True)
    row = db.get_provider(slug)
    if row is None:
        raise HTTPException(404, "no such provider")
    try:
        models_ = await fetch_models(request.app.state.client, row["kind"], row["base_url"], row["api_key"])
        return {"ok": True, "models": models_}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- klíče

class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = "client"
    allowed_providers: list = Field(default_factory=list)
    allowed_models: list = Field(default_factory=list)   # glob vzory, prázdné = všechny
    max_jobs: int = Field(default=0, ge=0)               # strop čekajících úloh; 0 = výchozí z nastavení
    rate_per_min: int = Field(default=0, ge=0)           # dotazů za minutu; 0 = výchozí z nastavení


class KeyModelsIn(BaseModel):
    allowed_models: list = Field(default_factory=list)


class KeyUpdateIn(BaseModel):
    allowed_models: Optional[list] = None
    max_jobs: Optional[int] = Field(default=None, ge=0)
    rate_per_min: Optional[int] = Field(default=None, ge=0)


@router.get("/keys")
async def list_keys(request: Request):
    require(request, admin=True)
    return db.list_keys()


@router.post("/keys", status_code=201)
async def create_key(body: KeyIn, request: Request):
    """Vrátí klíč v plaintextu — jen tady a jen jednou."""
    require(request, admin=True)
    if body.role not in ("admin", "client"):
        raise HTTPException(422, "role must be admin or client")
    plain, h, prefix = new_api_key()
    models_ = parse_model_patterns(body.allowed_models)
    kid = db.create_key(body.name, h, prefix, body.role, [str(s) for s in body.allowed_providers], models_)
    db.update_key_limits(kid, body.max_jobs, body.rate_per_min)
    return {"id": kid, "name": body.name, "role": body.role, "allowed_models": models_,
            "max_jobs": body.max_jobs, "rate_per_min": body.rate_per_min, "key": plain}


@router.put("/keys/{kid}")
async def update_key(kid: int, body: KeyUpdateIn, request: Request):
    """Změní povolené modely a limity existujícího klíče (vynechané = neměnit)."""
    require(request, admin=True)
    if db.get_key(kid) is None:
        raise HTTPException(404, "no such key")
    if body.allowed_models is not None:
        db.update_key_models(kid, parse_model_patterns(body.allowed_models))
    db.update_key_limits(kid, body.max_jobs, body.rate_per_min)
    row = db.get_key(kid)
    return {"id": kid, "allowed_models": parse_model_patterns(row["allowed_models"]),
            "max_jobs": row["max_jobs"], "rate_per_min": row["rate_per_min"]}


@router.put("/keys/{kid}/models")
async def update_key_models(kid: int, body: KeyModelsIn, request: Request):
    """Změní seznam povolených modelů existujícího klíče (prázdný = všechny)."""
    require(request, admin=True)
    models_ = parse_model_patterns(body.allowed_models)
    if not db.update_key_models(kid, models_):
        raise HTTPException(404, "no such key")
    return {"id": kid, "allowed_models": models_}


@router.delete("/keys/{kid}", status_code=204)
async def delete_key(kid: int, request: Request):
    p = require(request, admin=True)
    if p.kind == "key" and p.key_id == kid:
        raise HTTPException(409, "cannot delete the key you are using")
    if not db.delete_key(kid):
        raise HTTPException(404, "no such key")


# ------------------------------------------------------------- nastavení

class SettingsIn(BaseModel):
    retention_days: Optional[int] = None
    log_bodies: Optional[bool] = None
    ollama_require_key: Optional[bool] = None
    sched_enabled: Optional[bool] = None
    sched_hold_s: Optional[float] = Field(default=None, ge=0, le=3600)
    sched_max_wait_s: Optional[float] = Field(default=None, ge=0, le=3600)
    jobs_enabled: Optional[bool] = None
    jobs_max_wait_s: Optional[float] = Field(default=None, ge=0, le=86400)
    jobs_idle_s: Optional[float] = Field(default=None, ge=0, le=3600)
    jobs_preempt_s: Optional[float] = Field(default=None, ge=0, le=3600)
    jobs_max_queued: Optional[int] = Field(default=None, ge=0)
    jobs_retention_days: Optional[int] = Field(default=None, ge=0)
    rate_limit_per_min: Optional[int] = Field(default=None, ge=0)


@router.get("/settings")
async def get_settings(request: Request):
    require(request, admin=True)
    return db.public_settings()


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request):
    require(request, admin=True)
    if body.retention_days is not None:
        db.set_setting("retention_days", max(0, body.retention_days))
    if body.log_bodies is not None:
        db.set_setting("log_bodies", "1" if body.log_bodies else "0")
    if body.ollama_require_key is not None:
        db.set_setting("ollama_require_key", "1" if body.ollama_require_key else "0")
    if body.sched_enabled is not None:
        db.set_setting("sched_enabled", "1" if body.sched_enabled else "0")
    if body.sched_hold_s is not None:
        db.set_setting("sched_hold_s", body.sched_hold_s)
    if body.sched_max_wait_s is not None:
        db.set_setting("sched_max_wait_s", body.sched_max_wait_s)
    if body.jobs_enabled is not None:
        db.set_setting("jobs_enabled", "1" if body.jobs_enabled else "0")
    for key in ("jobs_max_wait_s", "jobs_idle_s", "jobs_preempt_s", "jobs_max_queued",
                "jobs_retention_days", "rate_limit_per_min"):
        val = getattr(body, key)
        if val is not None:
            db.set_setting(key, val)
    sched.refresh(db)
    worker.wake()
    return db.public_settings()


__all__ = ["router", "SETTING_DEFAULTS", "hash_key"]
