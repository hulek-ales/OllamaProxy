"""SQLite vrstva: schéma, migrace, nastavení, uživatelé, API klíče, poskytovatelé, log dotazů.

Jedno spojení chráněné zámkem — provoz je homelabový, na to bohatě stačí.
Schéma je zpětně kompatibilní s původní tabulkou `requests`; nové sloupce se
přidávají přes ALTER TABLE, takže se dá připojit i stará databáze.
"""

import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config
from .auth import model_allowed

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT,
    endpoint          TEXT,
    model             TEXT,
    status            INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_duration_ms REAL,
    eval_duration_ms  REAL,
    tokens_per_sec    REAL,
    wall_time_ms      REAL,
    request_json      TEXT,
    response_text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    must_change_pw INTEGER DEFAULT 0,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    key_hash          TEXT UNIQUE NOT NULL,
    prefix            TEXT,
    role              TEXT DEFAULT 'client',
    allowed_providers TEXT DEFAULT '',
    created_at        TEXT,
    last_used_at      TEXT,
    disabled          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT,
    key_id       INTEGER,
    key_name     TEXT,
    status       TEXT DEFAULT 'queued',   -- queued | running | done | error | cancelled
    priority     INTEGER DEFAULT 5,       -- 0 = nejvyšší, 9 = nejnižší
    provider     TEXT DEFAULT 'ollama',
    path         TEXT,
    model        TEXT,
    request_json TEXT,
    callback_url TEXT,
    not_before   TEXT,
    created_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    attempts     INTEGER DEFAULT 0,
    status_code  INTEGER,
    result_json  TEXT,
    error        TEXT,
    request_id   INTEGER,
    callback_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);

CREATE TABLE IF NOT EXISTS providers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT UNIQUE NOT NULL,
    name         TEXT,
    kind         TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    api_key      TEXT DEFAULT '',
    pricing_json TEXT DEFAULT '{}',
    inject_usage INTEGER DEFAULT 1,
    enabled      INTEGER DEFAULT 1,
    created_at   TEXT
);
"""

# sloupce tabulky requests přidané po první verzi (bezpečné i na existující DB)
EXTRA_COLUMNS = [
    ("placement", "TEXT"),
    ("vram_pct", "REAL"),
    ("loaded_model", "TEXT"),
    ("load1", "REAL"),
    ("load5", "REAL"),
    ("mem_avail_pct", "REAL"),
    ("concurrent", "INTEGER"),
    ("provider", "TEXT"),
    ("key_name", "TEXT"),
    ("client_ip", "TEXT"),
    ("cost_usd", "REAL"),
    ("error", "TEXT"),
    ("client_user", "TEXT"),   # uživatel z hlavičky X-OpenWebUI-User-Name (Open WebUI)
    ("queue_ms", "REAL"),      # kolik dotaz čekal v plánovači na uvolnění GPU
    ("job_id", "INTEGER"),     # dotaz vznikl z úlohy ve frontě (tabulka jobs)
]

# sloupce tabulky api_keys přidané později
EXTRA_KEY_COLUMNS = [
    ("allowed_models", "TEXT DEFAULT ''"),   # glob vzory povolených modelů, oddělené čárkou
    ("max_jobs", "INTEGER DEFAULT 0"),       # strop čekajících úloh klíče; 0 = výchozí z nastavení
    ("rate_per_min", "INTEGER DEFAULT 0"),   # dotazů za minutu; 0 = výchozí z nastavení
]

# sloupce tabulky providers přidané později
EXTRA_PROVIDER_COLUMNS = [
    ("models", "TEXT DEFAULT ''"),   # typ gpu: vzory modelů, které služba obsluhuje (směrování podle modelu)
]

# sloupce tabulky jobs přidané později
EXTRA_JOB_COLUMNS = [
    ("result_path", "TEXT"),         # binární výsledek (audio…) leží v souboru, ne v result_json
]

LIGHT_COLS = (
    "id, ts, endpoint, model, status, prompt_tokens, completion_tokens, "
    "total_duration_ms, eval_duration_ms, tokens_per_sec, wall_time_ms, "
    "placement, vram_pct, loaded_model, load1, load5, mem_avail_pct, concurrent, "
    "provider, key_name, client_ip, cost_usd, error, client_user, queue_ms, job_id"
)

JOB_LIGHT_COLS = (
    "id, batch_id, key_id, key_name, status, priority, provider, path, model, callback_url,"
    " not_before, created_at, started_at, finished_at, attempts, status_code, error, request_id,"
    " callback_status, result_path"
)

SETTING_DEFAULTS = {
    "retention_days": "0",          # 0 = nemazat
    "log_bodies": config.LOG_BODIES_DEFAULT,
    "ollama_require_key": "0",      # 1 = i holé Ollama API chce proxy klíč
    "sched_enabled": "1",           # plánovač modelů pro lokální Ollamu (viz scheduler.py)
    "sched_hold_s": "10",           # po posledním dotazu drží GPU model ještě tolik sekund
    "sched_max_wait_s": "90",       # déle nikdo nečeká: nové dotazy na aktuální model jdou do fronty
    "jobs_enabled": "1",            # pracovník fronty úloh (jobs.py)
    "jobs_max_wait_s": "900",       # úloha jiného modelu čeká nejdéle tolik, pak se model přepne
    "jobs_idle_s": "60",            # po interaktivním dotazu se model kvůli úloze nepřehazuje tolik sekund
    "jobs_preempt_s": "0",          # >0: běžící úlohu jiného modelu zrušit, když interaktivní dotaz čeká déle
    "jobs_max_queued": "200",       # výchozí strop čekajících úloh na klíč
    "jobs_retention_days": "7",     # hotové úlohy mazat po tolika dnech (0 = nemazat)
    "rate_limit_per_min": "0",      # výchozí limit dotazů za minutu na klíč (0 = bez limitu)
    "gpu_evict_timeout_s": "60",    # jak dlouho čekat, než backend (Ollama / GPU služba) uvolní VRAM
    "gpu_request_timeout_s": "900", # dotaz na GPU službu: nejdelší ticho na lince, pak se považuje za mrtvý
}

PROVIDER_KINDS = ("openai", "anthropic", "google", "ollama", "gpu")

_SINCE_RE = re.compile(r"^(\d+)([mhd])$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# tabulka původní proxy (v1): UUID klíč, naivní UTC čas s mikrosekundami
LEGACY_TABLE = "ollama_requests"
LEGACY_TABLE_DONE = "ollama_requests_migrated"


def legacy_ts(value):
    """ts z v1 ('2026-09-07T11:02:45.123456', naivní UTC) → formát now_iso()."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_since(value):
    """'24h' / '7d' / '30m' / ISO datum → ISO řetězec ve formátu, v jakém se ukládá ts."""
    if not value:
        return None
    value = str(value).strip()
    m = _SINCE_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


class Database:
    def __init__(self):
        self.conn = None
        self.path = None
        self.lock = threading.Lock()
        self._settings = {}

    # ------------------------------------------------------------ životní cyklus

    def init(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        for table, columns in (("requests", EXTRA_COLUMNS), ("api_keys", EXTRA_KEY_COLUMNS),
                               ("providers", EXTRA_PROVIDER_COLUMNS), ("jobs", EXTRA_JOB_COLUMNS)):
            for col, typ in columns:
                try:
                    self.conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + typ)
                except sqlite3.OperationalError:
                    pass  # sloupec už existuje
        self.conn.commit()
        self.migrate_legacy()
        self._settings = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}
        if not self._settings.get("secret_key"):
            self.set_setting("secret_key", secrets.token_hex(32))

    def migrate_legacy(self) -> int:
        """Jednorázově překopíruje log původní proxy (tabulka `ollama_requests`)
        do `requests`. Stará tabulka se pak přejmenuje, takže se migrace při
        dalším startu neopakuje a původní data zůstanou v souboru."""
        found = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (LEGACY_TABLE,)
        ).fetchone()
        if found is None:
            return 0
        cols = ["ts", "client_ip", "endpoint", "model", "request_json", "response_text",
                "prompt_tokens", "completion_tokens", "total_duration_ms",
                "eval_duration_ms", "tokens_per_sec", "wall_time_ms", "provider"]
        sql = ("INSERT INTO requests (" + ", ".join(cols) + ") VALUES ("
               + ", ".join("?" for _ in cols) + ")")
        try:
            old = [dict(r) for r in self.conn.execute(
                "SELECT * FROM " + LEGACY_TABLE + " ORDER BY ts").fetchall()]
            values = []
            for r in old:
                row = {c: r.get(c) for c in cols}
                row["ts"] = legacy_ts(r.get("ts"))
                row["provider"] = "ollama"
                values.append([row[c] for c in cols])
            self.conn.executemany(sql, values)
            self.conn.execute("ALTER TABLE " + LEGACY_TABLE + " RENAME TO " + LEGACY_TABLE_DONE)
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            print("[db] migrace " + LEGACY_TABLE + " selhala: " + str(exc), flush=True)
            return 0
        print("[db] migrováno " + str(len(values)) + " záznamů z " + LEGACY_TABLE
              + " do requests (stará tabulka přejmenována na " + LEGACY_TABLE_DONE + ")", flush=True)
        return len(values)

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    # ---------------------------------------------------------------- nastavení

    def setting(self, key: str, default=None):
        if key in self._settings:
            return self._settings[key]
        return SETTING_DEFAULTS.get(key, default)

    def set_setting(self, key: str, value):
        value = "" if value is None else str(value)
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self.conn.commit()
        self._settings[key] = value

    def public_settings(self) -> dict:
        return {k: self.setting(k) for k in SETTING_DEFAULTS}

    @property
    def secret(self) -> str:
        return self.setting("secret_key", "")

    # ---------------------------------------------------------------- uživatelé

    def user_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get_user(self, user_id: int):
        with self.lock:
            r = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(r) if r else None

    def get_user_by_name(self, username: str):
        with self.lock:
            r = self.conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(r) if r else None

    def create_user(self, username: str, password_hash: str, must_change_pw: bool = False) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO users (username, password_hash, must_change_pw, created_at) VALUES (?,?,?,?)",
                (username, password_hash, 1 if must_change_pw else 0, now_iso()))
            self.conn.commit()
            return cur.lastrowid

    def update_password(self, user_id: int, password_hash: str):
        with self.lock:
            self.conn.execute(
                "UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
                (password_hash, user_id))
            self.conn.commit()

    # ---------------------------------------------------------------- API klíče

    def create_key(self, name, key_hash, prefix, role="client", allowed_providers=(),
                   allowed_models=()) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO api_keys (name, key_hash, prefix, role, allowed_providers,"
                " allowed_models, created_at) VALUES (?,?,?,?,?,?,?)",
                (name, key_hash, prefix, role, ",".join(allowed_providers),
                 ",".join(allowed_models), now_iso()))
            self.conn.commit()
            return cur.lastrowid

    def list_keys(self) -> list:
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT id, name, prefix, role, allowed_providers, allowed_models, created_at,"
                " last_used_at, disabled FROM api_keys ORDER BY id"))

    def update_key_models(self, key_id: int, allowed_models) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE api_keys SET allowed_models = ? WHERE id = ?",
                                    (",".join(allowed_models), key_id))
            self.conn.commit()
            return cur.rowcount > 0

    def update_key_limits(self, key_id: int, max_jobs=None, rate_per_min=None) -> bool:
        sets, args = [], []
        if max_jobs is not None:
            sets.append("max_jobs = ?")
            args.append(max(0, int(max_jobs)))
        if rate_per_min is not None:
            sets.append("rate_per_min = ?")
            args.append(max(0, int(rate_per_min)))
        if not sets:
            return self.get_key(key_id) is not None
        with self.lock:
            cur = self.conn.execute("UPDATE api_keys SET " + ", ".join(sets) + " WHERE id = ?",
                                    args + [key_id])
            self.conn.commit()
            return cur.rowcount > 0

    def get_key(self, key_id: int):
        with self.lock:
            r = self.conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(r) if r else None

    # -------------------------------------------------------------- úlohy

    def create_jobs(self, jobs: list) -> list:
        """jobs = [{batch_id, key_id, key_name, priority, provider, path, model, request_json,
        callback_url, not_before}] → seznam id."""
        ids = []
        with self.lock:
            for j in jobs:
                cur = self.conn.execute(
                    "INSERT INTO jobs (batch_id, key_id, key_name, status, priority, provider, path,"
                    " model, request_json, callback_url, not_before, created_at)"
                    " VALUES (?,?,?,'queued',?,?,?,?,?,?,?,?)",
                    (j["batch_id"], j.get("key_id"), j.get("key_name"), j.get("priority", 5),
                     j.get("provider", "ollama"), j["path"], j.get("model"), j["request_json"],
                     j.get("callback_url"), j.get("not_before"), now_iso()))
                ids.append(cur.lastrowid)
            self.conn.commit()
        return ids

    def get_job(self, job_id: int):
        with self.lock:
            r = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None

    def list_jobs(self, status=None, batch_id=None, key_id=None, limit=100, offset=0,
                  with_bodies=False):
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if batch_id:
            where.append("batch_id = ?")
            args.append(batch_id)
        if key_id is not None:
            where.append("key_id = ?")
            args.append(key_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        cols = "*" if with_bodies else JOB_LIGHT_COLS
        with self.lock:
            rows = _rows(self.conn.execute(
                "SELECT " + cols + " FROM jobs" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]))
            total = self.conn.execute("SELECT COUNT(*) FROM jobs" + clause, args).fetchone()[0]
        return rows, total

    def count_active_jobs(self, key_id) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE key_id IS ? AND status IN ('queued','running')",
                (key_id,)).fetchone()[0]

    def queued_jobs(self) -> list:
        """Úlohy připravené ke spuštění (bez těl), nejdřív podle priority, pak stáří."""
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT " + JOB_LIGHT_COLS + " FROM jobs WHERE status = 'queued'"
                " AND (not_before IS NULL OR not_before <= ?) ORDER BY priority, id", (now_iso(),)))

    def start_job(self, job_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1"
                " WHERE id = ? AND status = 'queued'", (now_iso(), job_id))
            self.conn.commit()
            return cur.rowcount > 0

    def finish_job(self, job_id: int, status: str, status_code=None, result_json=None, error=None,
                   request_id=None, result_path=None):
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, status_code = ?, result_json = ?,"
                " error = ?, request_id = ?, result_path = ? WHERE id = ?",
                (status, now_iso(), status_code, result_json, error, request_id, result_path, job_id))
            self.conn.commit()

    def requeue_job(self, job_id: int, error=None):
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL, error = ? WHERE id = ?",
                (error, job_id))
            self.conn.commit()

    def set_job_callback_status(self, job_id: int, status: str):
        with self.lock:
            self.conn.execute("UPDATE jobs SET callback_status = ? WHERE id = ?", (status, job_id))
            self.conn.commit()

    def cancel_job(self, job_id: int, key_id=None) -> str:
        """Vrátí 'cancelled', 'running' (běží — zruší ji pracovník), 'done' (už hotová) nebo 'missing'."""
        with self.lock:
            r = self.conn.execute("SELECT status, key_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if r is None or (key_id is not None and r["key_id"] != key_id):
                return "missing"
            if r["status"] == "queued":
                self.conn.execute("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                                  (now_iso(), job_id))
                self.conn.commit()
                return "cancelled"
            return r["status"] if r["status"] == "running" else "done"

    def requeue_running_jobs(self) -> int:
        """Po startu: úlohy, které běžely při pádu, zpátky do fronty."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL,"
                " error = 'restart proxy during run' WHERE status = 'running'")
            self.conn.commit()
            return cur.rowcount

    def jobs_summary(self) -> dict:
        with self.lock:
            by_status = {r[0]: r[1] for r in self.conn.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status")}
            queued_by_model = {r[0] or "?": r[1] for r in self.conn.execute(
                "SELECT model, COUNT(*) FROM jobs WHERE status = 'queued' GROUP BY model")}
            oldest = self.conn.execute(
                "SELECT MIN(created_at) FROM jobs WHERE status = 'queued'").fetchone()[0]
        return {"by_status": by_status, "queued_by_model": queued_by_model, "oldest_queued_at": oldest}

    def purge_jobs(self, days: int) -> int:
        if not days or days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        where = " WHERE status IN ('done','error','cancelled') AND finished_at < ?"
        with self.lock:
            files = [r[0] for r in self.conn.execute(
                "SELECT result_path FROM jobs" + where + " AND result_path IS NOT NULL", (cutoff,))]
            cur = self.conn.execute("DELETE FROM jobs" + where, (cutoff,))
            self.conn.commit()
        for path in files:   # soubor s výsledkem odchází spolu se záznamem
            try:
                os.remove(path)
            except OSError:
                pass
        return cur.rowcount

    def get_key_by_hash(self, key_hash: str):
        with self.lock:
            r = self.conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
        return dict(r) if r else None

    def touch_key(self, key_id: int):
        with self.lock:
            self.conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), key_id))
            self.conn.commit()

    def delete_key(self, key_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- poskytovatelé

    def list_providers(self) -> list:
        with self.lock:
            return _rows(self.conn.execute("SELECT * FROM providers ORDER BY slug"))

    def get_provider(self, slug: str):
        with self.lock:
            r = self.conn.execute("SELECT * FROM providers WHERE slug = ?", (slug,)).fetchone()
        return dict(r) if r else None

    def gpu_providers(self) -> list:
        """Zapnuté lokální GPU služby (typ gpu) — sdílejí kartu s Ollamou, jdou přes plánovač."""
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT * FROM providers WHERE kind = 'gpu' AND enabled = 1 ORDER BY slug"))

    def provider_for_model(self, model: str):
        """Slug GPU služby, která model obsluhuje (podle jejích vzorů), jinak None = Ollama."""
        if not model:
            return None
        for prov in self.gpu_providers():
            patterns = [p for p in (prov.get("models") or "").split(",") if p]
            if patterns and model_allowed(patterns, model):
                return prov["slug"]
        return None

    def save_provider(self, slug, name, kind, base_url, api_key=None, pricing=None,
                      inject_usage=True, enabled=True, models=()):
        """Založí nebo upraví poskytovatele. api_key=None znamená „klíč neměnit“."""
        if kind not in PROVIDER_KINDS:
            raise ValueError("neznámý typ poskytovatele: " + str(kind))
        pricing_json = json.dumps(pricing or {})
        models_csv = ",".join(models or ())
        with self.lock:
            existing = self.conn.execute("SELECT id, api_key FROM providers WHERE slug = ?", (slug,)).fetchone()
            if existing:
                key = existing["api_key"] if api_key is None else api_key
                self.conn.execute(
                    "UPDATE providers SET name=?, kind=?, base_url=?, api_key=?, pricing_json=?,"
                    " inject_usage=?, enabled=?, models=? WHERE slug=?",
                    (name, kind, base_url, key, pricing_json, 1 if inject_usage else 0,
                     1 if enabled else 0, models_csv, slug))
            else:
                self.conn.execute(
                    "INSERT INTO providers (slug, name, kind, base_url, api_key, pricing_json,"
                    " inject_usage, enabled, models, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (slug, name, kind, base_url, api_key or "", pricing_json,
                     1 if inject_usage else 0, 1 if enabled else 0, models_csv, now_iso()))
            self.conn.commit()

    def delete_provider(self, slug: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM providers WHERE slug = ?", (slug,))
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- log dotazů

    def log_request(self, row: dict):
        cols = list(row.keys())
        sql = ("INSERT INTO requests (" + ", ".join(cols) + ") VALUES ("
               + ", ".join("?" for _ in cols) + ")")
        with self.lock:
            try:
                cur = self.conn.execute(sql, [row[c] for c in cols])
                self.conn.commit()
                return cur.lastrowid
            except Exception as exc:  # logování nesmí shodit proxy
                print("log_request failed:", exc, flush=True)
                return None

    @staticmethod
    def _where(filters: dict):
        where, args = [], []
        if filters.get("model"):
            where.append("model LIKE ?")
            args.append("%" + filters["model"] + "%")
        if filters.get("provider"):
            where.append("provider = ?")
            args.append(filters["provider"])
        if filters.get("placement"):
            where.append("placement = ?")
            args.append(filters["placement"])
        if filters.get("key_name"):
            where.append("key_name LIKE ?")
            args.append("%" + filters["key_name"] + "%")
        if filters.get("user"):
            where.append("client_user LIKE ?")
            args.append("%" + filters["user"] + "%")
        if filters.get("status") is not None and filters.get("status") != "":
            if str(filters["status"]) == "error":
                where.append("(status >= 400 OR error IS NOT NULL)")
            else:
                where.append("status = ?")
                args.append(int(filters["status"]))
        since = parse_since(filters.get("since"))
        if since:
            where.append("ts >= ?")
            args.append(since)
        if filters.get("q"):
            where.append("(request_json LIKE ? OR response_text LIKE ? OR error LIKE ?)")
            args.extend(["%" + filters["q"] + "%"] * 3)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, args

    def query_requests(self, filters: dict, limit=100, offset=0, with_bodies=False):
        clause, args = self._where(filters)
        cols = "*" if with_bodies else LIGHT_COLS
        with self.lock:
            rows = _rows(self.conn.execute(
                "SELECT " + cols + " FROM requests" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]))
            total = self.conn.execute("SELECT COUNT(*) FROM requests" + clause, args).fetchone()[0]
        return rows, total

    def get_request(self, rid: int):
        with self.lock:
            r = self.conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        return dict(r) if r else None

    def stats(self, since=None) -> dict:
        since_iso = parse_since(since)
        clause = " WHERE ts >= ?" if since_iso else ""
        args = [since_iso] if since_iso else []
        agg = ("COUNT(*) n, COALESCE(SUM(prompt_tokens),0) prompt_tokens,"
               " COALESCE(SUM(completion_tokens),0) completion_tokens,"
               " ROUND(AVG(tokens_per_sec),1) tokens_per_sec, ROUND(AVG(wall_time_ms)) wall_time_ms,"
               " ROUND(COALESCE(SUM(cost_usd),0),4) cost_usd,"
               " SUM(CASE WHEN status >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) errors")
        with self.lock:
            total = dict(self.conn.execute("SELECT " + agg + " FROM requests" + clause, args).fetchone())
            groups = {}
            for col in ("provider", "model", "placement", "key_name", "client_user"):
                groups[col] = _rows(self.conn.execute(
                    "SELECT " + col + " AS key, " + agg + " FROM requests" + clause
                    + " GROUP BY " + col + " ORDER BY n DESC", args))
            # spotřeba aplikace (klíč) na jednotlivých modelech
            by_key_model = _rows(self.conn.execute(
                "SELECT key_name, provider, model, " + agg + " FROM requests" + clause
                + " GROUP BY key_name, provider, model"
                + " ORDER BY key_name IS NULL, key_name, prompt_tokens + completion_tokens DESC", args))
        return {
            "since": since_iso,
            "total": total,
            "by_provider": groups["provider"],
            "by_model": groups["model"],
            "by_placement": groups["placement"],
            "by_key": groups["key_name"],
            "by_user": groups["client_user"],
            "by_key_model": by_key_model,
        }

    def distinct(self, col: str) -> list:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT DISTINCT " + col + " FROM requests WHERE " + col + " IS NOT NULL ORDER BY 1")]

    def purge(self, days: int) -> int:
        if not days or days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.lock:
            cur = self.conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            self.conn.commit()
        return cur.rowcount


db = Database()
