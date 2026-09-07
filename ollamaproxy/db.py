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
]

LIGHT_COLS = (
    "id, ts, endpoint, model, status, prompt_tokens, completion_tokens, "
    "total_duration_ms, eval_duration_ms, tokens_per_sec, wall_time_ms, "
    "placement, vram_pct, loaded_model, load1, load5, mem_avail_pct, concurrent, "
    "provider, key_name, client_ip, cost_usd, error"
)

SETTING_DEFAULTS = {
    "retention_days": "0",          # 0 = nemazat
    "log_bodies": config.LOG_BODIES_DEFAULT,
    "ollama_require_key": "0",      # 1 = i holé Ollama API chce proxy klíč
}

PROVIDER_KINDS = ("openai", "anthropic", "google", "ollama")

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
        for col, typ in EXTRA_COLUMNS:
            try:
                self.conn.execute("ALTER TABLE requests ADD COLUMN " + col + " " + typ)
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

    def create_key(self, name, key_hash, prefix, role="client", allowed_providers=()) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO api_keys (name, key_hash, prefix, role, allowed_providers, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, key_hash, prefix, role, ",".join(allowed_providers), now_iso()))
            self.conn.commit()
            return cur.lastrowid

    def list_keys(self) -> list:
        with self.lock:
            return _rows(self.conn.execute(
                "SELECT id, name, prefix, role, allowed_providers, created_at, last_used_at, disabled"
                " FROM api_keys ORDER BY id"))

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

    def save_provider(self, slug, name, kind, base_url, api_key=None, pricing=None,
                      inject_usage=True, enabled=True):
        """Založí nebo upraví poskytovatele. api_key=None znamená „klíč neměnit“."""
        if kind not in PROVIDER_KINDS:
            raise ValueError("neznámý typ poskytovatele: " + str(kind))
        pricing_json = json.dumps(pricing or {})
        with self.lock:
            existing = self.conn.execute("SELECT id, api_key FROM providers WHERE slug = ?", (slug,)).fetchone()
            if existing:
                key = existing["api_key"] if api_key is None else api_key
                self.conn.execute(
                    "UPDATE providers SET name=?, kind=?, base_url=?, api_key=?, pricing_json=?,"
                    " inject_usage=?, enabled=? WHERE slug=?",
                    (name, kind, base_url, key, pricing_json, 1 if inject_usage else 0,
                     1 if enabled else 0, slug))
            else:
                self.conn.execute(
                    "INSERT INTO providers (slug, name, kind, base_url, api_key, pricing_json,"
                    " inject_usage, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, name, kind, base_url, api_key or "", pricing_json,
                     1 if inject_usage else 0, 1 if enabled else 0, now_iso()))
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
                self.conn.execute(sql, [row[c] for c in cols])
                self.conn.commit()
            except Exception as exc:  # logování nesmí shodit proxy
                print("log_request failed:", exc, flush=True)

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
            for col in ("provider", "model", "placement", "key_name"):
                groups[col] = _rows(self.conn.execute(
                    "SELECT " + col + " AS key, " + agg + " FROM requests" + clause
                    + " GROUP BY " + col + " ORDER BY n DESC", args))
        return {
            "since": since_iso,
            "total": total,
            "by_provider": groups["provider"],
            "by_model": groups["model"],
            "by_placement": groups["placement"],
            "by_key": groups["key_name"],
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
