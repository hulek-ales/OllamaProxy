"""Migrace logu z původní proxy (tabulka `ollama_requests`) do `requests`."""
import os
import sqlite3
import tempfile

from ollamaproxy.db import LEGACY_TABLE_DONE, Database, legacy_ts

LEGACY_SCHEMA = """
CREATE TABLE ollama_requests (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, client_ip TEXT, endpoint TEXT, model TEXT,
    stream INTEGER, request_json TEXT, response_text TEXT, prompt_tokens INTEGER,
    completion_tokens INTEGER, total_duration_ms REAL, eval_duration_ms REAL,
    tokens_per_sec REAL, wall_time_ms REAL
)
"""


def _legacy_db():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO ollama_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("u1", "2026-09-01T10:00:00.123456", "172.24.1.5", "/api/chat", "gemma4:12b", 1,
          '{"model":"gemma4:12b"}', "Ahoj", 12, 2, 2000.0, 1000.0, 2.0, 2100.0),
         ("u2", "2026-09-02T11:30:15.000001", None, "/api/generate", "llama3", 0,
          None, None, None, None, 0.0, 0.0, None, 50.0)])
    conn.commit()
    conn.close()
    return path


def test_legacy_ts_normalised():
    assert legacy_ts("2026-09-01T10:00:00.123456") == "2026-09-01T10:00:00+00:00"
    assert legacy_ts("2026-09-01T12:00:00+02:00") == "2026-09-01T10:00:00+00:00"
    assert legacy_ts("nesmysl") == "nesmysl"
    assert legacy_ts(None) is None


def test_legacy_rows_migrated_once():
    path = _legacy_db()
    db = Database()
    db.init(path)
    rows, total = db.query_requests({}, with_bodies=True)
    assert total == 2
    newest, oldest = rows  # řazeno od nejnovějšího id
    assert oldest["ts"] == "2026-09-01T10:00:00+00:00"
    assert oldest["model"] == "gemma4:12b"
    assert oldest["client_ip"] == "172.24.1.5"
    assert oldest["provider"] == "ollama"
    assert oldest["response_text"] == "Ahoj"
    assert (oldest["prompt_tokens"], oldest["completion_tokens"]) == (12, 2)
    assert newest["endpoint"] == "/api/generate"
    assert newest["client_ip"] is None
    # stará tabulka zůstala, jen pod jiným jménem
    names = {r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ollama_requests" not in names and LEGACY_TABLE_DONE in names
    db.close()

    # druhý start: nic se nepřidá
    db2 = Database()
    db2.init(path)
    assert db2.migrate_legacy() == 0
    assert db2.query_requests({})[1] == 2
    db2.close()


def test_fresh_db_without_legacy_table():
    db = Database()
    db.init(os.path.join(tempfile.mkdtemp(), "new.db"))
    assert db.migrate_legacy() == 0
    db.close()
