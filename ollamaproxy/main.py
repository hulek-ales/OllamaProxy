"""Sestavení aplikace: DB, výchozí admin, HTTP klient, retence, pořadí rout."""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import config, mgmt, proxy, ui
from .auth import hash_password
from .db import db
from .scheduler import sched
from .telemetry import ps_models


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
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(config.DB_PATH)
    ensure_admin()
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
    sched.refresh(db)
    sched.loaded_probe = lambda: ps_models(app.state.client, config.UPSTREAM)
    task = asyncio.create_task(retention_loop())
    print("proxy " + config.VERSION + " ready, upstream = " + config.UPSTREAM
          + (", commit " + config.GIT_COMMIT if config.GIT_COMMIT else ""), flush=True)
    try:
        yield
    finally:
        task.cancel()
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
