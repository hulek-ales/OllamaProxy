"""Sestavení aplikace: DB, výchozí admin, HTTP klient, retence, pořadí rout."""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import config, mgmt, proxy, ui
from .auth import hash_password
from .db import db
from .jobs import worker
from .providers import backend_ps, backend_unload, provider_models
from .scheduler import sched
from .telemetry import ps_models


# ------------------------------------------------ více backendů na jedné GPU

def backend_of(model: str) -> str:
    """Kam model patří: slug GPU služby (poskytovatel typu gpu), jinak hlavní Ollama."""
    return db.provider_for_model(model) or "ollama"


async def all_loaded(client: httpx.AsyncClient) -> list:
    """Modely v paměti napříč backendy. Nedostupná GPU služba = prázdná (nesmí shodit Ollamu)."""
    names = list(await ps_models(client, config.UPSTREAM))
    for prov in db.gpu_providers():
        try:
            names.extend(await backend_ps(client, prov["base_url"], prov["api_key"], provider_models(prov)))
        except Exception:
            pass
    return names


async def _wait_empty(probe, poll: float = 0.5):
    while await probe():
        await asyncio.sleep(poll)


async def evict_backend(client: httpx.AsyncClient, slug: str):
    """Řekne backendu, ať uvolní VRAM, a počká, až podle jeho /api/ps opravdu nic nedrží.
    Časový strop hlídá plánovač (gpu_evict_timeout_s)."""
    if slug == "ollama":
        for name in await ps_models(client, config.UPSTREAM):
            # prázdný generate s keep_alive 0 = Ollama model vyhodí; embedding model přes /api/embed
            r = await client.post(config.UPSTREAM + "/api/generate",
                                  json={"model": name, "keep_alive": 0}, timeout=60.0)
            if r.status_code >= 400:
                await client.post(config.UPSTREAM + "/api/embed",
                                  json={"model": name, "input": "", "keep_alive": 0}, timeout=60.0)
        await _wait_empty(lambda: ps_models(client, config.UPSTREAM))
        return
    prov = db.get_provider(slug)
    if prov is None or prov["kind"] != "gpu":
        return
    await backend_unload(client, prov["base_url"], prov["api_key"])
    await _wait_empty(lambda: backend_ps(client, prov["base_url"], prov["api_key"], provider_models(prov)))


def ensure_admin():
    if db.user_count() > 0:
        return
    password = config.ADMIN_PASSWORD or config.DEFAULT_ADMIN_PASSWORD
    db.create_user(config.ADMIN_USERNAME, hash_password(password),
                   must_change_pw=not config.ADMIN_PASSWORD)
    if config.ADMIN_PASSWORD:
        print("[init] admin účet '" + config.ADMIN_USERNAME + "' založen", flush=True)
    else:
        print("[init] admin účet '" + config.ADMIN_USERNAME + "' založen s VÝCHOZÍM heslem '"
              + config.DEFAULT_ADMIN_PASSWORD + "' — změň ho hned po přihlášení!", flush=True)


async def retention_loop():
    while True:
        try:
            days = int(db.setting("retention_days") or 0)
        except ValueError:
            days = 0
        if days > 0:
            try:
                n = db.purge(days)
                if n:
                    print("[retence] smazáno " + str(n) + " záznamů starších než " + str(days) + " dní",
                          flush=True)
            except Exception as exc:
                print("[retence] selhala:", exc, flush=True)
        try:
            jdays = int(float(db.setting("jobs_retention_days") or 0))
            n = db.purge_jobs(jdays)
            if n:
                print("[retence] smazáno " + str(n) + " hotových úloh starších než " + str(jdays) + " dní",
                      flush=True)
        except Exception as exc:
            print("[retence úloh] selhala:", exc, flush=True)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(config.DB_PATH)
    ensure_admin()
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
    os.makedirs(config.JOBS_DIR, exist_ok=True)
    sched.refresh(db)
    sched.loaded_probe = lambda: all_loaded(app.state.client)
    sched.backend_of = backend_of
    sched.evictor = lambda slug: evict_backend(app.state.client, slug)
    worker.start(lambda: app.state.client)
    task = asyncio.create_task(retention_loop())
    print("proxy " + config.VERSION + " ready, upstream = " + config.UPSTREAM
          + (", commit " + config.GIT_COMMIT if config.GIT_COMMIT else ""), flush=True)
    try:
        yield
    finally:
        task.cancel()
        await worker.stop()
        await app.state.client.aclose()
        db.close()


app = FastAPI(
    title="Ollama logging proxy",
    version=config.VERSION,
    description=(
        "Reverse proxy před Ollamou a komerčními LLM API s logem tokenů a výkonu. "
        "Autentizace: `Authorization: Bearer opx_…` (klíč z GUI /ui/keys)."
    ),
    docs_url="/mgmt/docs",
    openapi_url="/mgmt/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)

# pořadí je důležité: konkrétní cesty před catch-all proxy
app.include_router(ui.router)
app.include_router(mgmt.router)
app.include_router(proxy.router)
