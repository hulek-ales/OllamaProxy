"""Povolené modely u klíče + plánovač přes HTTP (X-Opx-Wait, /models/load, /models/status)."""

import json

import pytest
from fastapi.testclient import TestClient

from ollamaproxy.auth import model_allowed, parse_model_patterns
from ollamaproxy.db import db
from ollamaproxy.scheduler import sched


@pytest.fixture
def clean_sched():
    """Plánovač je globální — po testu vrátit do klidu (nic neběží, nic nečeká)."""
    yield sched
    sched.admitted = None
    sched.draining = False
    sched.in_flight.clear()
    sched.waiting.clear()
    sched.last_done = 0.0


def test_pattern_matching():
    assert model_allowed([], "cokoli")
    assert not model_allowed(["gemma4:12b"], None)
    assert model_allowed(["gemma4:12b"], "gemma4:12b")
    assert not model_allowed(["gemma4:12b"], "gemma4:latest")
    assert model_allowed(["gemma4"], "gemma4:12b") and model_allowed(["gemma4"], "gemma4:latest")
    assert not model_allowed(["gemma4"], "gemma4-pro:1b")
    assert model_allowed(["qwen3*"], "qwen3:8b") and model_allowed(["*-mini"], "gpt-4o-mini")
    assert not model_allowed(["gemma4*"], "gpt-4o")
    assert parse_model_patterns("gemma4:12b, nomic-embed-text qwen3*\n") == ["gemma4:12b", "nomic-embed-text", "qwen3*"]
    assert parse_model_patterns(["a", " a ", "", "b"]) == ["a", "b"]


def test_key_with_allowed_models(admin, client, upstream, clean_sched):
    sched.hold_s = 0
    r = admin.post("/mgmt/v1/keys", json={"name": "kucharka", "role": "client",
                                          "allowed_models": ["gemma4", "nomic-embed-text"]})
    assert r.status_code == 201 and r.json()["allowed_models"] == ["gemma4", "nomic-embed-text"]
    key = r.json()["key"]
    kid = r.json()["id"]
    assert [k for k in admin.get("/mgmt/v1/keys").json() if k["id"] == kid][0]["allowed_models"] == "gemma4,nomic-embed-text"

    fresh = TestClient(client.app)
    h = {"Authorization": "Bearer " + key}
    # povolený model projde
    r = fresh.post("/api/chat", headers=h, json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}]})
    assert r.status_code == 200
    # jiný lokální model → 403 a záznam v logu
    before = db.query_requests({"status": "error"})[1]
    r = fresh.post("/api/chat", headers=h, json={"model": "llama3:8b", "messages": []})
    assert r.status_code == 403 and "not allowed" in r.json()["error"]
    rows, total = db.query_requests({"status": "error"})
    assert total == before + 1 and rows[0]["status"] == 403 and rows[0]["model"] == "llama3:8b" \
        and rows[0]["key_name"] == "kucharka"
    assert upstream.calls[-1].url.path != "/api/chat" or json.loads(upstream.calls[-1].content)["model"] != "llama3:8b"
    # komerční poskytovatel: klíč ho má sice povolený (allowed_providers prázdné), ale model ne
    r = fresh.post("/providers/openai/v1/chat/completions", headers=h,
                   json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    # Gemini má model v cestě
    admin.post("/mgmt/v1/providers", json={"slug": "gemini", "kind": "google",
                                           "base_url": "https://gemini.test", "api_key": "g"})
    r = fresh.post("/providers/gemini/v1beta/models/gemini-2.5-flash:generateContent", headers=h, json={})
    assert r.status_code == 403
    # seznamy modelů se ořežou
    tags = fresh.get("/api/tags", headers=h).json()
    assert [m["name"] for m in tags["models"]] == ["gemma4:12b", "nomic-embed-text:latest"]
    v1 = fresh.get("/v1/models", headers=h).json()
    assert [m["id"] for m in v1["data"]] == ["gemma4:12b"]
    assert fresh.get("/providers/openai/v1/models", headers=h).json()["data"] == []
    out = fresh.get("/mgmt/v1/models", headers=h).json()
    assert out["ollama"]["models"] == ["gemma4:12b", "nomic-embed-text:latest"] and out["openai"]["models"] == []
    # bez klíče (holé Ollama API) se nic neořezává
    assert len(fresh.get("/api/tags").json()["models"]) == 3
    # /models/load respektuje seznam
    assert fresh.post("/mgmt/v1/models/load", headers=h, json={"model": "llama3:8b"}).status_code == 403

    # změna seznamu přes API: prázdný = všechno
    r = admin.put("/mgmt/v1/keys/" + str(kid) + "/models", json={"allowed_models": []})
    assert r.status_code == 200 and r.json()["allowed_models"] == []
    assert fresh.post("/api/chat", headers=h, json={"model": "llama3:8b", "messages": []}).status_code == 200
    assert admin.put("/mgmt/v1/keys/999999/models", json={"allowed_models": ["x"]}).status_code == 404


def test_ui_key_models(admin):
    csrf = admin.get("/ui/keys").text.split('name="csrf" value="')[1].split('"')[0]
    r = admin.post("/ui/keys", data={"csrf": csrf, "name": "gui-omezeny", "role": "client",
                                     "allowed_models": "gemma4:12b, qwen3*"})
    assert r.status_code == 200 and "opx_" in r.text
    row = [k for k in db.list_keys() if k["name"] == "gui-omezeny"][0]
    assert row["allowed_models"] == "gemma4:12b,qwen3*"
    r = admin.post("/ui/keys/" + str(row["id"]) + "/models", data={"csrf": csrf, "allowed_models": ""},
                   follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert [k for k in db.list_keys() if k["id"] == row["id"]][0]["allowed_models"] == ""
    assert "Povolené modely" in admin.get("/ui/keys").text


def test_sched_settings_roundtrip(admin):
    r = admin.put("/mgmt/v1/settings", json={"sched_hold_s": 3, "sched_max_wait_s": 45, "sched_enabled": True})
    assert r.status_code == 200
    assert r.json()["sched_hold_s"] == "3.0" and r.json()["sched_max_wait_s"] == "45.0"
    assert sched.hold_s == 3.0 and sched.max_wait_s == 45.0 and sched.enabled
    csrf = admin.get("/ui/settings").text.split('name="csrf" value="')[1].split('"')[0]
    r = admin.post("/ui/settings", data={"csrf": csrf, "retention_days": "0", "log_bodies": "1",
                                         "sched_enabled": "1", "sched_hold_s": "10", "sched_max_wait_s": "90"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert sched.hold_s == 10.0 and sched.max_wait_s == 90.0
    page = admin.get("/ui/settings").text
    assert "Plánovač modelů" in page and "Stav plánovače" in page


def test_no_wait_header_gives_503_when_gpu_busy(admin, clean_sched):
    sched.admitted = "llama3:8b"
    sched.in_flight["llama3:8b"] = 1        # simulace: jiný model zrovna odpovídá
    r = admin.post("/api/chat", headers={"X-Opx-Wait": "0"},
                   json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}]})
    assert r.status_code == 503 and "Retry-After" in r.headers
    assert r.json()["scheduler"]["admitted"] == "llama3:8b"
    rows, _ = db.query_requests({"status": "error"})
    assert rows[0]["status"] == 503 and rows[0]["model"] == "gemma4:12b"
    assert sched.waiting == [] and sched.in_flight == {"llama3:8b": 1}
    # bez hlavičky by čekal; po uvolnění projde a zapíše se queue_ms
    sched.in_flight.clear()
    sched.hold_s = 0
    r = admin.post("/api/chat", json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}]})
    assert r.status_code == 200 and sched.admitted == "gemma4:12b"
    rows, _ = db.query_requests({"model": "gemma4:12b"})
    assert rows[0]["queue_ms"] is not None and rows[0]["status"] == 200
    assert sched.in_flight == {}
    assert admin.get("/ui/r/" + str(rows[0]["id"])).status_code == 200


def test_models_status_and_load(admin, clean_sched):
    st = admin.get("/mgmt/v1/models/status").json()
    assert st["loaded"] == ["gemma4:12b"] and "waiting" in st
    # model už v paměti → ready bez volání Ollamy
    r = admin.post("/mgmt/v1/models/load", json={"model": "gemma4:12b"})
    assert r.status_code == 200 and r.json()["loaded"] is True and r.json()["status"] == "ready"
    assert sched.in_flight == {}
    # jiný model, GPU volné (hold 0) → proxy ho nechá Ollamou nahrát a vrátí ready
    sched.hold_s = 0
    r = admin.post("/mgmt/v1/models/load", json={"model": "llama3:8b", "wait_s": 5, "keep_alive": "30m"})
    assert r.json()["loaded"] is True and r.json()["status"] == "ready" and sched.admitted == "llama3:8b"
    assert sched.in_flight == {}
    # GPU drží jiný model, který zrovna odpovídá → queued, nic se nenahrává
    sched.admitted = "llama3:8b"
    sched.in_flight["llama3:8b"] = 1
    r = admin.post("/mgmt/v1/models/load", json={"model": "qwen3:8b", "wait_s": 0})
    assert r.json() == {**r.json(), "loaded": False, "status": "queued", "admitted": "llama3:8b"}
    assert sched.waiting == []
    assert admin.get("/mgmt/v1/health").json()["scheduler"]["admitted"] == "llama3:8b"
    assert "scheduler" in admin.get("/healthz").json()
