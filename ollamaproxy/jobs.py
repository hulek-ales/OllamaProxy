"""Fronta odložených úloh pro agenty + limit dotazů za minutu na klíč.

Agent pošle dotaz přes POST /mgmt/v1/jobs, dostane id a proxy ho vyřídí, až se
to hodí. Pracovník (`worker`) běží v jednom tasku a rozhoduje takto:

  * interaktivní provoz (průchozí dotazy z proxy.py) má vždy přednost — dokud
    nějaký běží nebo čeká, nová úloha se nezačne; běžící úloha se nechá doběhnout
    (měkké přerušení);
  * volitelně tvrdé přerušení: `jobs_preempt_s` > 0 a interaktivní dotaz na jiný
    model čeká déle → běžící úloha se zruší a vrátí do fronty;
  * po interaktivním dotazu se model kvůli úloze nepřehazuje `jobs_idle_s`
    sekund (člověk v chatu nedostane reload uprostřed konverzace); úlohy na
    model, který GPU zrovna drží, jdou hned;
  * drží se modelu ve VRAM, dokud pro něj má úlohy; na jiný model přepne, až
    pro aktuální nic nezbývá, nebo když nejstarší úloha jiného modelu čeká déle
    než `jobs_max_wait_s`;
  * úlohy pro komerční poskytovatele GPU nepotřebují a jedou hned.

Výsledek se uloží do tabulky jobs (celá odpověď upstreamu, vždy bez streamu),
zapíše se i do běžného logu `requests` (tokeny, cena, Spotřeba) a volitelně
se pošle na `callback_url`.
"""

import asyncio
import json
import time
from collections import deque

import httpx

from . import config
from .collector import Collector
from .db import db, now_iso
from .providers import auth_headers, cost_usd, parse_pricing
from .scheduler import sched
from .telemetry import bump, gpu_snapshot, host_snapshot

MAX_ATTEMPTS = 3
ALLOWED_JOB_PATHS = (
    "/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
    "/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses",
    "/v1/messages",
)


# ---------------------------------------------------------- limit dotazů

class RateLimiter:
    """Klouzavé okno 60 s na klíč. `hit()` vrátí None, nebo za kolik sekund to zkusit znovu."""

    def __init__(self):
        self._hits = {}

    def hit(self, key_id, limit: int):
        if not limit or limit <= 0 or key_id is None:
            return None
        now = time.monotonic()
        q = self._hits.setdefault(key_id, deque())
        while q and now - q[0] >= 60.0:
            q.popleft()
        if len(q) >= limit:
            return max(1, int(60.0 - (now - q[0])) + 1)
        q.append(now)
        return None

    def reset(self):
        self._hits.clear()


limiter = RateLimiter()


def key_limit(key_row: dict, column: str, setting: str) -> int:
    """Limit klíče, nebo výchozí z nastavení, když je u klíče 0."""
    own = (key_row or {}).get(column) or 0
    if own:
        return int(own)
    try:
        return int(float(db.setting(setting) or 0))
    except ValueError:
        return 0


# ---------------------------------------------------------------- pracovník

class JobWorker:
    def __init__(self):
        self.task = None            # smyčka
        self.current = None         # běžící úloha (dict) nebo None
        self.current_task = None    # asyncio task běžící úlohy
        self.last_model = None      # model poslední úlohy (lepivost)
        self.runs = 0
        self.preempted = 0
        self.client_getter = None   # () → httpx.AsyncClient
        self._wake = None
        self._preempting = False

    # ------------------------------------------------------------- nastavení

    def _num(self, key, default):
        try:
            return float(db.setting(key) or default)
        except ValueError:
            return default

    @property
    def enabled(self) -> bool:
        return db.setting("jobs_enabled") == "1"

    def snapshot(self) -> dict:
        cur = self.current
        return {
            "enabled": self.enabled,
            "running": {"id": cur["id"], "model": cur.get("model"), "key_name": cur.get("key_name"),
                        "provider": cur.get("provider"), "started_at": cur.get("started_at")} if cur else None,
            "runs": self.runs,
            "preempted": self.preempted,
            "last_model": self.last_model,
            "max_wait_s": self._num("jobs_max_wait_s", 900),
            "idle_s": self._num("jobs_idle_s", 60),
            "preempt_s": self._num("jobs_preempt_s", 0),
            **db.jobs_summary(),
        }

    def wake(self):
        if self._wake is not None:
            self._wake.set()

    # --------------------------------------------------------------- smyčka

    def start(self, client_getter):
        self.client_getter = client_getter
        self._wake = asyncio.Event()
        n = db.requeue_running_jobs()
        if n:
            print("[jobs] " + str(n) + " úloh vráceno do fronty po restartu", flush=True)
        self.task = asyncio.get_running_loop().create_task(self.loop())

    async def stop(self):
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def loop(self):
        while True:
            try:
                job = self.pick_next() if self.enabled else None
                if job is None:
                    await self._sleep(1.0)
                    continue
                await self.run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[jobs] chyba pracovníka:", exc, flush=True)
                await self._sleep(2.0)

    async def _sleep(self, seconds: float):
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    # ---------------------------------------------------------------- výběr

    def pick_next(self):
        """Další úloha podle pravidel v hlavičce modulu, nebo None (nic vhodného teď)."""
        queued = db.queued_jobs()
        if not queued:
            return None
        commercial = [j for j in queued if j["provider"] != "ollama"]
        if commercial:
            return commercial[0]
        if sched.interactive_pending():
            return None
        now = time.monotonic()
        current = sched.admitted or self.last_model
        idle_s = self._num("jobs_idle_s", 60)
        recent_human = (sched.last_interactive_done
                        and now - sched.last_interactive_done < idle_s)
        max_wait = self._num("jobs_max_wait_s", 900)

        def age(j):
            try:
                return time.time() - _ts(j["created_at"])
            except Exception:
                return 0.0

        starving = [j for j in queued if j["model"] != current and age(j) > max_wait]
        same = [j for j in queued if j["model"] == current]
        if starving and not recent_human:
            target = min(starving, key=lambda j: j["created_at"])["model"]
            return next(j for j in queued if j["model"] == target)
        if same:
            return same[0]
        if recent_human:
            return None      # jiný model by člověku v chatu vyhodil ten jeho z VRAM
        return queued[0]

    # ----------------------------------------------------------------- běh

    async def run(self, light: dict):
        job = db.get_job(light["id"])
        if job is None or not db.start_job(job["id"]):
            return
        job["status"] = "running"
        job["started_at"] = now_iso()
        self.current = job
        self.current_task = asyncio.get_running_loop().create_task(self._execute(job))
        self._preempting = False
        preempt_s = self._num("jobs_preempt_s", 0)
        try:
            while True:
                done, _ = await asyncio.wait({self.current_task}, timeout=0.5)
                if done:
                    break
                if (preempt_s > 0 and job["provider"] == "ollama" and not self._preempting
                        and sched.oldest_interactive_wait_s() > preempt_s
                        and any(w.model != job["model"] for w in sched.waiting if w.interactive)):
                    self._preempting = True
                    self.current_task.cancel()
                    self.preempted += 1
                    print("[jobs] úloha " + str(job["id"]) + " přerušena kvůli interaktivnímu dotazu",
                          flush=True)
            try:
                await self.current_task
            except asyncio.CancelledError:
                if not self._preempting:
                    raise                      # ruší se sám pracovník (stop)
                fresh = db.get_job(job["id"])
                if fresh and fresh["status"] == "running":   # jinak ji zrušil klient / GUI
                    attempts = job["attempts"] + 1  # start_job už přičetl
                    if attempts >= MAX_ATTEMPTS:
                        db.finish_job(job["id"], "error",
                                      error="preempted " + str(attempts) + "×, giving up")
                    else:
                        db.requeue_job(job["id"], error="preempted by interactive request")
        finally:
            if self.current_task is not None and not self.current_task.done():
                self.current_task.cancel()
            self.current = None
            self.current_task = None

    async def _execute(self, job: dict):
        client = self.client_getter()
        try:
            body_obj = json.loads(job["request_json"] or "{}")
        except Exception:
            body_obj = {}
        if isinstance(body_obj, dict):
            body_obj["stream"] = False
        body = json.dumps(body_obj, ensure_ascii=False).encode()
        model = job.get("model")
        provider = job["provider"]
        headers = {"content-type": "application/json"}
        pricing = {}
        if provider == "ollama":
            url = config.UPSTREAM + job["path"]
        else:
            prov = db.get_provider(provider)
            if prov is None or not prov["enabled"]:
                db.finish_job(job["id"], "error", error="provider '" + provider + "' is not configured")
                return
            headers = auth_headers(prov["kind"], prov["api_key"], headers)
            url = prov["base_url"].rstrip("/") + job["path"]
            try:
                pricing = parse_pricing(prov["pricing_json"])
            except Exception:
                pricing = {}

        acquired = False
        queue_ms = 0
        started = time.time()
        try:
            if provider == "ollama":
                queue_ms = round(await sched.acquire(model, interactive=False) * 1000)
                acquired = True
            snap = host_snapshot()
            if provider == "ollama":
                snap.update(await gpu_snapshot(client, config.UPSTREAM, model))
            snap["concurrent"] = bump(1)
            try:
                resp = await client.post(url, content=body, headers=headers, timeout=3600.0)
            finally:
                bump(-1)
            raw = resp.content
            collector = Collector(True)
            collector.feed(raw)
            collector.finish()
            pt, ct = collector.prompt_tokens, collector.completion_tokens
            used_model = collector.model or model
            error = collector.error
            if error is None and resp.status_code >= 400:
                error = "HTTP " + str(resp.status_code) + ": " + raw[:300].decode("utf-8", "replace")
            log_bodies = db.setting("log_bodies") == "1"
            rid = db.log_request({
                "ts": now_iso(), "endpoint": job["path"], "provider": provider, "model": used_model,
                "status": resp.status_code, "prompt_tokens": pt, "completion_tokens": ct,
                "total_duration_ms": collector.total_ns / 1e6 if collector.total_ns else None,
                "eval_duration_ms": collector.eval_ns / 1e6 if collector.eval_ns else None,
                "tokens_per_sec": collector.tokens_per_sec(),
                "wall_time_ms": (time.time() - started) * 1000,
                "request_json": job["request_json"] if log_bodies else None,
                "response_text": collector.text() if log_bodies else None,
                "cost_usd": cost_usd(pricing, used_model, pt, ct) if pricing else None,
                "error": error, "key_name": job.get("key_name"), "queue_ms": queue_ms,
                "job_id": job["id"], **snap,
            })
            try:
                result_json = json.dumps(json.loads(raw), ensure_ascii=False)
            except Exception:
                result_json = json.dumps({"raw": raw.decode("utf-8", "replace")})
            db.finish_job(job["id"], "done" if error is None else "error", status_code=resp.status_code,
                          result_json=result_json, error=error, request_id=rid)
            self.runs += 1
            self.last_model = model if provider == "ollama" else self.last_model
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db.finish_job(job["id"], "error", error="upstream: " + str(exc))
        finally:
            if acquired:
                sched.release(model, interactive=False)
        if job.get("callback_url"):
            await self._callback(client, db.get_job(job["id"]))

    async def _callback(self, client, job: dict):
        if not job:
            return
        try:
            r = await client.post(job["callback_url"], json=job_view(job, with_bodies=True), timeout=10.0)
            db.set_job_callback_status(job["id"], "HTTP " + str(r.status_code))
        except Exception as exc:
            db.set_job_callback_status(job["id"], "failed: " + str(exc)[:200])


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def job_view(job: dict, with_bodies: bool = False) -> dict:
    out = {k: job.get(k) for k in (
        "id", "batch_id", "key_name", "status", "priority", "provider", "path", "model",
        "callback_url", "not_before", "created_at", "started_at", "finished_at", "attempts",
        "status_code", "error", "request_id", "callback_status")}
    if with_bodies:
        for src, dst in (("request_json", "request"), ("result_json", "result")):
            raw = job.get(src)
            try:
                out[dst] = json.loads(raw) if raw else None
            except Exception:
                out[dst] = raw
    return out


worker = JobWorker()
