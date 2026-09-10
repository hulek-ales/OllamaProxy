"""Webové GUI na /ui — přihlášení, log, detail, poskytovatelé, klíče, nastavení."""

import asyncio
import json
import os
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config
from .auth import (SESSION_COOKIE, csrf_token, hash_password, make_session, new_api_key,
                   parse_model_patterns, parse_session, verify_password)
from .db import PROVIDER_KINDS, db
from .jobs import job_view, worker
from .providers import (DEFAULT_BASE_URL, KIND_LABELS, client_base_url, fetch_models,
                        mask_key, parse_pricing)
from .scheduler import sched

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# ----------------------------------------------------------------- filtry

def fmt(val, digits=1):
    if val is None or val == "":
        return "—"
    if isinstance(val, float):
        return ("%." + str(digits) + "f") % val
    return str(val)


def fmt_ts(val):
    return (val or "").replace("T", " ").replace("+00:00", "")


def fmt_money(val):
    if val is None:
        return "—"
    if val == 0:
        return "0"
    return ("%.4f" % val).rstrip("0").rstrip(".") + " $"


def fmt_int(val):
    if val is None:
        return "—"
    try:
        return "{:,}".format(int(val)).replace(",", " ")
    except (TypeError, ValueError):
        return str(val)


templates.env.filters["fmt"] = fmt
templates.env.filters["ts"] = fmt_ts
templates.env.filters["money"] = fmt_money
templates.env.filters["num"] = fmt_int


# ---------------------------------------------------------------- pomocné

def proxy_root(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return proto + "://" + host


def current_user(request: Request):
    uid = parse_session(db.secret, request.cookies.get(SESSION_COOKIE))
    return db.get_user(uid) if uid is not None else None


def login_redirect(request: Request):
    return RedirectResponse("/ui/login?next=" + quote(str(request.url.path)), status_code=303)


def render(request: Request, name: str, user, **ctx):
    ctx.update({
        "user": user,
        "csrf": csrf_token(db.secret, request.cookies.get(SESSION_COOKIE, "")),
        "version": config.VERSION,
        "commit": config.GIT_COMMIT,
        "upstream": config.UPSTREAM,
        "msg": ctx.get("msg") or request.query_params.get("msg"),
        "err": ctx.get("err") or request.query_params.get("err"),
        "warn_default_pw": bool(user and user.get("must_change_pw")),
        "path": request.url.path,
    })
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = None, err: str = None):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


async def form_with_csrf(request: Request):
    """Vrátí formulář, nebo None když CSRF token nesedí."""
    form = await request.form()
    expected = csrf_token(db.secret, request.cookies.get(SESSION_COOKIE, ""))
    if form.get("csrf") != expected:
        return None
    return form


# ------------------------------------------------------------- přihlášení

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/ui", status_code=303)
    return render(request, "login.html", None, next=request.query_params.get("next", "/ui"))


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    user = db.get_user_by_name(username)
    if not user or not verify_password(password, user["password_hash"]):
        await asyncio.sleep(1.0)  # brzda proti hádání hesla
        return render(request, "login.html", None, next=form.get("next", "/ui"),
                      err="Špatné jméno nebo heslo.")
    nxt = form.get("next") or "/ui"
    if not nxt.startswith("/ui"):
        nxt = "/ui"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session(db.secret, user["id"]), httponly=True,
                    samesite="lax", max_age=30 * 24 * 3600, path="/")
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------- log

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui_list(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    qs = request.query_params
    try:
        page_no = max(1, int(qs.get("page", "1") or 1))
    except ValueError:
        page_no = 1
    per = 100
    filters = {
        "model": qs.get("model", "").strip(),
        "provider": qs.get("provider", "").strip(),
        "placement": qs.get("placement", "").strip(),
        "key_name": qs.get("key", "").strip(),
        "user": qs.get("user", "").strip(),
        "status": qs.get("status", "").strip(),
        "since": qs.get("since", "").strip(),
        "q": qs.get("q", "").strip(),
    }
    rows, total = db.query_requests(filters, limit=per, offset=(page_no - 1) * per)
    day = db.stats("24h")
    week = db.stats("7d")
    base_qs = "&".join(k + "=" + quote(v) for k, v in {
        "model": filters["model"], "provider": filters["provider"], "placement": filters["placement"],
        "key": filters["key_name"], "user": filters["user"], "status": filters["status"],
        "since": filters["since"], "q": filters["q"]}.items() if v)
    return render(request, "list.html", user, rows=rows, total=total, page=page_no, per=per,
                  filters=filters, day=day, week=week, base_qs=base_qs,
                  providers=["ollama"] + [p["slug"] for p in db.list_providers()],
                  keys=db.distinct("key_name"))


@router.get("/usage", response_class=HTMLResponse)
async def ui_usage(request: Request):
    """Spotřeba tokenů po aplikacích (klíčích), modelech a uživatelích Open WebUI."""
    user = current_user(request)
    if not user:
        return login_redirect(request)
    since = request.query_params.get("since", "7d").strip()
    if since not in ("24h", "7d", "30d", ""):
        since = "7d"
    return render(request, "usage.html", user, s=db.stats(since or None), since=since)


@router.get("/r/{rid}", response_class=HTMLResponse)
async def ui_detail(rid: int, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    r = db.get_request(rid)
    if r is None:
        return render(request, "detail.html", user, row=None, rid=rid)
    pretty = None
    if r.get("request_json"):
        try:
            pretty = json.dumps(json.loads(r["request_json"]), indent=2, ensure_ascii=False)
        except Exception:
            pretty = r["request_json"]
    return render(request, "detail.html", user, row=r, rid=rid, request_pretty=pretty)


# ------------------------------------------------------------------ úlohy

@router.get("/jobs", response_class=HTMLResponse)
async def ui_jobs(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    status = request.query_params.get("status", "").strip()
    batch = request.query_params.get("batch", "").strip()
    rows, total = db.list_jobs(status=status or None, batch_id=batch or None, limit=200)
    return render(request, "jobs.html", user, rows=rows, total=total, status=status, batch=batch,
                  jobs=worker.snapshot(), sched=sched.snapshot())


@router.get("/jobs/{jid}", response_class=HTMLResponse)
async def ui_job_detail(jid: int, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    job = db.get_job(jid)
    view = job_view(job, with_bodies=True) if job else None
    pretty = {}
    if view:
        for k in ("request", "result"):
            if view.get(k) is not None:
                pretty[k] = json.dumps(view[k], indent=2, ensure_ascii=False)
    return render(request, "job.html", user, job=view, jid=jid, pretty=pretty)


@router.post("/jobs/{jid}/cancel")
async def ui_job_cancel(jid: int, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    if await form_with_csrf(request) is None:
        return back("/ui/jobs", err="Formulář vypršel, zkus to znovu.")
    res = db.cancel_job(jid)
    if res == "running" and worker.current and worker.current["id"] == jid and worker.current_task:
        worker._preempting = True
        worker.current_task.cancel()
        db.finish_job(jid, "cancelled", error="cancelled in GUI")
        res = "cancelled"
    return back("/ui/jobs", msg="Úloha " + str(jid) + ": " + res + ".")


# ----------------------------------------------------------- poskytovatelé

@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    edit_slug = request.query_params.get("edit")
    edit = db.get_provider(edit_slug) if edit_slug else None
    rows = db.list_providers()
    root = proxy_root(request)
    for p in rows:
        p["masked"] = mask_key(p["api_key"])
        p["client_base_url"] = client_base_url(root, p["slug"], p["kind"])
    return render(request, "providers.html", user, rows=rows, edit=edit, kinds=PROVIDER_KINDS,
                  kind_labels=KIND_LABELS, defaults=DEFAULT_BASE_URL, root=root)


@router.post("/providers")
async def providers_save(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = await form_with_csrf(request)
    if form is None:
        return back("/ui/providers", err="Formulář vypršel, zkus to znovu.")
    slug = (form.get("slug") or "").strip().lower()
    kind = form.get("kind") or ""
    base_url = (form.get("base_url") or "").strip()
    name = (form.get("name") or "").strip() or slug
    api_key = (form.get("api_key") or "").strip()
    if not slug or not slug.replace("-", "").replace("_", "").isalnum() or slug == "ollama":
        return back("/ui/providers", err="Neplatný identifikátor (jen a-z, 0-9, - a _; 'ollama' je rezervované).")
    if kind not in PROVIDER_KINDS or not base_url:
        return back("/ui/providers", err="Vyber typ a vyplň adresu.")
    try:
        pricing = parse_pricing(form.get("pricing") or "{}")
    except Exception as exc:
        return back("/ui/providers?edit=" + slug, err="Ceník není platný JSON: " + str(exc))
    models = parse_model_patterns(form.get("models") or "") if kind == "gpu" else []
    if kind == "gpu" and not models:
        return back("/ui/providers?edit=" + slug,
                    err="GPU služba potřebuje seznam modelů, které obsluhuje (podle nich se směruje).")
    existing = db.get_provider(slug)
    if form.get("clear_key"):
        key_arg = ""
    elif api_key:
        key_arg = api_key
    else:
        key_arg = None if existing else ""
    db.save_provider(slug, name, kind, base_url, api_key=key_arg, pricing=pricing,
                     inject_usage=bool(form.get("inject_usage")), enabled=bool(form.get("enabled")),
                     models=models)
    return back("/ui/providers", msg=("Poskytovatel „" + name + "“ uložen."))


@router.post("/providers/{slug}/delete")
async def providers_delete(slug: str, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    if await form_with_csrf(request) is None:
        return back("/ui/providers", err="Formulář vypršel, zkus to znovu.")
    db.delete_provider(slug)
    return back("/ui/providers", msg="Poskytovatel smazán.")


@router.post("/providers/{slug}/test")
async def providers_test(slug: str, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    if await form_with_csrf(request) is None:
        return back("/ui/providers", err="Formulář vypršel, zkus to znovu.")
    row = db.get_provider(slug)
    if row is None:
        return back("/ui/providers", err="Poskytovatel neexistuje.")
    try:
        models = await fetch_models(request.app.state.client, row["kind"], row["base_url"], row["api_key"])
    except Exception as exc:
        return back("/ui/providers", err=slug + ": " + str(exc))
    sample = ", ".join(models[:8]) + (" …" if len(models) > 8 else "")
    return back("/ui/providers", msg=slug + " odpovídá, modelů: " + str(len(models)) + " (" + sample + ")")


# ------------------------------------------------------------------ klíče

@router.get("/keys", response_class=HTMLResponse)
async def keys_page(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    return render(request, "keys.html", user, rows=db.list_keys(),
                  providers=[p["slug"] for p in db.list_providers()], root=proxy_root(request))


@router.post("/keys")
async def keys_create(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = await form_with_csrf(request)
    if form is None:
        return back("/ui/keys", err="Formulář vypršel, zkus to znovu.")
    name = (form.get("name") or "").strip()
    role = form.get("role") or "client"
    if not name:
        return back("/ui/keys", err="Klíč potřebuje název (podle něj se pozná v logu).")
    if role not in ("admin", "client"):
        role = "client"
    allowed = [s for s in form.getlist("allowed") if s]
    models = parse_model_patterns(form.get("allowed_models") or "")
    plain, h, prefix = new_api_key()
    kid = db.create_key(name, h, prefix, role, allowed, models)
    db.update_key_limits(kid, _int(form.get("max_jobs")), _int(form.get("rate_per_min")))
    return render(request, "keys.html", user, rows=db.list_keys(),
                  providers=[p["slug"] for p in db.list_providers()], root=proxy_root(request),
                  new_key=plain, new_key_name=name)


@router.post("/keys/{kid}/models")
async def keys_models(kid: int, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = await form_with_csrf(request)
    if form is None:
        return back("/ui/keys", err="Formulář vypršel, zkus to znovu.")
    models = parse_model_patterns(form.get("allowed_models") or "")
    if not db.update_key_models(kid, models):
        return back("/ui/keys", err="Klíč neexistuje.")
    db.update_key_limits(kid, _int(form.get("max_jobs")), _int(form.get("rate_per_min")))
    return back("/ui/keys", msg="Klíč " + str(kid) + " uložen: modely "
                + (", ".join(models) or "všechny") + ".")


def _int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


@router.post("/keys/{kid}/delete")
async def keys_delete(kid: int, request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    if await form_with_csrf(request) is None:
        return back("/ui/keys", err="Formulář vypršel, zkus to znovu.")
    db.delete_key(kid)
    return back("/ui/keys", msg="Klíč smazán.")


# -------------------------------------------------------------- nastavení

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    status = sched.snapshot()
    try:
        status["loaded"] = sorted(await sched.loaded(force=True))
    except Exception:
        status["loaded"] = []
    return render(request, "settings.html", user, settings=db.public_settings(),
                  db_mb=round(db.size_bytes() / 1048576, 1), root=proxy_root(request), sched=status,
                  jobs=worker.snapshot(), gpu_backends=[p["slug"] for p in db.gpu_providers()])


@router.post("/settings")
async def settings_save(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = await form_with_csrf(request)
    if form is None:
        return back("/ui/settings", err="Formulář vypršel, zkus to znovu.")
    try:
        days = max(0, int(form.get("retention_days") or 0))
    except ValueError:
        days = 0
    db.set_setting("retention_days", days)
    db.set_setting("log_bodies", "1" if form.get("log_bodies") else "0")
    db.set_setting("ollama_require_key", "1" if form.get("ollama_require_key") else "0")
    db.set_setting("sched_enabled", "1" if form.get("sched_enabled") else "0")
    db.set_setting("jobs_enabled", "1" if form.get("jobs_enabled") else "0")
    for key, default in (("sched_hold_s", 10), ("sched_max_wait_s", 90), ("jobs_max_wait_s", 900),
                         ("jobs_idle_s", 60), ("jobs_preempt_s", 0), ("gpu_evict_timeout_s", 60),
                         ("gpu_request_timeout_s", 900)):
        try:
            db.set_setting(key, max(0.0, float(form.get(key) or default)))
        except ValueError:
            db.set_setting(key, default)
    for key, default in (("jobs_max_queued", 200), ("jobs_retention_days", 7), ("rate_limit_per_min", 0)):
        db.set_setting(key, _int(form.get(key), default))
    sched.refresh(db)
    worker.wake()
    return back("/ui/settings", msg="Nastavení uloženo.")


@router.post("/settings/password")
async def settings_password(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = await form_with_csrf(request)
    if form is None:
        return back("/ui/settings", err="Formulář vypršel, zkus to znovu.")
    if not verify_password(form.get("current") or "", user["password_hash"]):
        return back("/ui/settings", err="Současné heslo nesedí.")
    new = form.get("new") or ""
    if len(new) < 8:
        return back("/ui/settings", err="Nové heslo musí mít aspoň 8 znaků.")
    if new != (form.get("again") or ""):
        return back("/ui/settings", err="Nová hesla se neshodují.")
    db.update_password(user["id"], hash_password(new))
    return back("/ui/settings", msg="Heslo změněno.")
