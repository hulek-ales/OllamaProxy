"""JSON API pro správu a ladění — /mgmt/v1/…  (dokumentace na /mgmt/docs).

Autentizace: `Authorization: Bearer opx_…` (klíč z GUI) nebo přihlášená session.
Čtení logu a statistik smí každý platný klíč; správa poskytovatelů, klíčů a
nastavení jen klíč s rolí admin (nebo session).
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import config
from .auth import hash_key, new_api_key, principal_from_request
from .db import PROVIDER_KINDS, SETTING_DEFAULTS, db
from .providers import client_base_url, fetch_models, mask_key, parse_pricing
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
    return snap


# ------------------------------------------------------------------- log

@router.get("/requests")
async def list_requests(request: Request, limit: int = 100, offset: int = 0,
                        model: Optional[str] = None, provider: Optional[str] = None,
                        placement: Optional[str] = None, status: Optional[str] = None,
                        key: Optional[str] = None, since: Optional[str] = None,
                        q: Optional[str] = None, bodies: int = 0):
    """Seznam dotazů, nejnovější první. `since` = 24h / 7d / ISO datum, `status` = číslo nebo `error`."""
    require(request)
    limit = max(1, min(limit, 500))
    filters = {"model": model, "provider": provider, "placement": placement,
               "status": status, "key_name": key, "since": since, "q": q}
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
    """Součty a průměry: celkem a po poskytovatelích, modelech, umístění a klíčích."""
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
                         "models": await fetch_models(client, "ollama", config.UPSTREAM, "")}
    except Exception as exc:
        out["ollama"] = {"ok": False, "error": str(exc), "models": []}
    for prov in db.list_providers():
        if not prov["enabled"] or not p.may_use(prov["slug"]):
            continue
        entry = {"kind": prov["kind"],
                 "base_url": client_base_url(proxy_root(request), prov["slug"], prov["kind"])}
        try:
            entry["models"] = await fetch_models(client, prov["kind"], prov["base_url"], prov["api_key"])
            entry["ok"] = True
        except Exception as exc:
            entry.update({"ok": False, "error": str(exc), "models": []})
        out[prov["slug"]] = entry
    return out


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
    kid = db.create_key(body.name, h, prefix, body.role, [str(s) for s in body.allowed_providers])
    return {"id": kid, "name": body.name, "role": body.role, "key": plain}


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
    return db.public_settings()


__all__ = ["router", "SETTING_DEFAULTS", "hash_key"]
