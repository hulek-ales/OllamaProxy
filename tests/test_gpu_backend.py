"""GPU služba (poskytovatel typu gpu) přes HTTP: směrování podle modelu, uvolnění VRAM Ollamy
před TTS a naopak, řazení interaktivních dotazů za běžící syntézu, úloha s výsledkem v souboru."""

import os
import time

import pytest
from fastapi.testclient import TestClient

from ollamaproxy import config
from ollamaproxy.db import db
from ollamaproxy.jobs import limiter
from ollamaproxy.scheduler import sched


def wait_until(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def tts(admin, upstream):
    """Zaregistrovaná GPU služba `tts` (modely tts-cs), rychlý plánovač, čistý stav; po testu uklidit."""
    r = admin.post("/mgmt/v1/providers", json={"slug": "tts", "kind": "gpu", "base_url": "http://tts.test",
                                               "models": ["tts-cs"]})
    assert r.status_code == 201, r.text
    admin.put("/mgmt/v1/settings", json={"sched_hold_s": 0, "jobs_idle_s": 0, "gpu_evict_timeout_s": 5,
                                         "jobs_preempt_s": 0, "rate_limit_per_min": 0, "jobs_enabled": True})
    limiter.reset()
    upstream.loaded = {"gemma4:12b"}
    upstream.tts_loaded = set()
    upstream.tts_slow_s = 0.0
    sched.admitted = None
    sched.draining = False
    sched.evicting = None
    sched.in_flight.clear()
    sched.waiting.clear()
    sched.last_done = sched.last_interactive_done = 0.0
    sched.last_evict_error = None
    sched._ps_cache = (0.0, frozenset())
    yield r.json()
    admin.delete("/mgmt/v1/providers/tts")
    admin.put("/mgmt/v1/settings", json={"sched_hold_s": 10, "jobs_idle_s": 60, "gpu_evict_timeout_s": 60})
    upstream.loaded = {"gemma4:12b"}
    upstream.tts_loaded = set()
    upstream.tts_slow_s = 0.0
    sched.admitted = None
    sched.evicting = None
    sched.in_flight.clear()
    sched.waiting.clear()
    sched.last_done = sched.last_interactive_done = 0.0
    sched._ps_cache = (0.0, frozenset())


def calls(upstream, host, path):
    return [c for c in upstream.calls if c.url.host == host and c.url.path == path]


def test_gpu_provider_needs_models_and_shows_up(admin, tts, upstream):
    assert tts["kind"] == "gpu" and tts["models"] == ["tts-cs"]
    assert tts["client_base_url"].endswith("/v1") and "/providers/" not in tts["client_base_url"]
    r = admin.post("/mgmt/v1/providers", json={"slug": "tts2", "kind": "gpu", "base_url": "http://tts.test"})
    assert r.status_code == 422 and "models" in r.text
    # modely služby jsou v /mgmt/v1/models, ale ne v /api/tags (Open WebUI by je nabídlo jako chat)
    models = admin.get("/mgmt/v1/models").json()
    assert models["tts"]["ok"] and models["tts"]["models"] == ["tts-cs"]
    tags = admin.get("/api/tags").json()["models"]
    assert all(m["name"] != "tts-cs" for m in tags)
    assert admin.post("/mgmt/v1/providers/tts/test").json()["ok"] is True
    page = admin.get("/ui/providers").text
    assert "tts-cs" in page and "GPU-BACKEND" in page
    # úprava přes GUI vyžaduje modely
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = admin.post("/ui/providers", data={"csrf": csrf, "slug": "tts", "kind": "gpu", "base_url": "http://tts.test",
                                          "models": "", "enabled": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post("/ui/providers", data={"csrf": csrf, "slug": "tts", "kind": "gpu", "base_url": "http://tts.test",
                                          "models": "tts-cs, tts-*", "enabled": "1"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert db.get_provider("tts")["models"] == "tts-cs,tts-*"
    assert db.provider_for_model("tts-en") == "tts" and db.provider_for_model("gemma4:12b") is None


def test_route_by_model_evicts_ollama_and_back(admin, tts, upstream):
    chat = {"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}], "stream": False}
    assert admin.post("/api/chat", json=chat).status_code == 200
    assert sched.admitted == "gemma4:12b"
    n0 = len(upstream.calls)

    r = admin.post("/v1/audio/speech", json={"model": "tts-cs", "input": "Ahoj světe", "voice": "jirka"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/mpeg") and r.content.startswith(b"ID3")
    # nejdřív Ollama uvolnila VRAM (keep_alive 0), pak šla syntéza do kontejneru TTS
    seq = [(c.url.host, c.url.path) for c in upstream.calls[n0:]]
    assert ("ollama.test", "/api/generate") in seq and ("tts.test", "/v1/audio/speech") in seq
    assert seq.index(("ollama.test", "/api/generate")) < seq.index(("tts.test", "/v1/audio/speech"))
    assert upstream.loaded == set() and upstream.tts_loaded == {"tts-cs"}
    assert sched.admitted == "tts-cs" and sched.evicting is None and sched.in_flight == {}
    assert sched.snapshot()["backend"] == "tts" and sched.evictions == 1
    # log: poskytovatel tts, znaky vstupu místo tokenů, žádný text odpovědi
    row = admin.get("/mgmt/v1/requests?limit=1&bodies=1").json()["items"][0]
    assert row["provider"] == "tts" and row["model"] == "tts-cs" and row["endpoint"] == "/v1/audio/speech"
    assert row["status"] == 200 and row["prompt_tokens"] == len("Ahoj světe") and row["response_text"] is None
    assert row["key_name"] == "admin" and row["queue_ms"] is not None

    # zpátky k chatu: proxy řekne TTS, ať uvolní VRAM, pak pustí Ollamu
    n1 = len(upstream.calls)
    assert admin.post("/api/chat", json=chat).status_code == 200
    seq = [(c.url.host, c.url.path) for c in upstream.calls[n1:]]
    assert seq.index(("tts.test", "/api/unload")) < seq.index(("ollama.test", "/api/chat"))
    assert upstream.tts_loaded == set() and sched.admitted == "gemma4:12b" and sched.evictions == 2

    st = admin.get("/mgmt/v1/models/status").json()
    assert st["backends"]["tts"]["ok"] is True and st["backends"]["tts"]["loaded"] == []
    assert st["backend"] == "ollama" and st["evicting"] is None and st["last_evict_error"] is None
    assert "tts" in admin.get("/ui/settings").text


def test_interactive_chat_waits_for_running_tts_job(admin, tts, upstream):
    upstream.tts_slow_s = 0.8
    # úloha bez `provider`: proxy ji podle modelu pošle do TTS
    r = admin.post("/mgmt/v1/jobs", json={"path": "/v1/audio/speech", "body": {"model": "tts-cs", "input": "Dobrý den"}})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    assert wait_until(lambda: sched.in_flight.get("tts-cs") == 1, 10.0), sched.snapshot()
    assert admin.get("/mgmt/v1/jobs/" + str(jid)).json()["provider"] == "tts"
    # chat teď musí počkat, až syntéza doběhne a TTS uvolní VRAM
    t0 = time.time()
    chat = {"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}], "stream": False}
    assert admin.post("/api/chat", json=chat).status_code == 200
    assert time.time() - t0 >= 0.3
    row = admin.get("/mgmt/v1/requests?limit=1").json()["items"][0]
    assert row["model"] == "gemma4:12b" and row["queue_ms"] > 100
    assert upstream.tts_loaded == set() and sched.admitted == "gemma4:12b"

    job = admin.get("/mgmt/v1/jobs/" + str(jid)).json()
    assert job["status"] == "done" and job["result_url"] == "/mgmt/v1/jobs/" + str(jid) + "/result"
    assert job["result"]["content_type"] == "audio/mpeg" and job["result"]["bytes"] > 0
    assert job["result"]["file"].startswith(config.JOBS_DIR) and os.path.isfile(job["result"]["file"])
    r = admin.get(job["result_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg")
    assert r.content.startswith(b"ID3") and len(r.content) == job["result"]["bytes"]
    logrow = db.get_request(job["request_id"])
    assert logrow["provider"] == "tts" and logrow["prompt_tokens"] == len("Dobrý den") and logrow["job_id"] == jid
    page = admin.get("/ui/jobs/" + str(jid)).text
    assert job["result_url"] in page
    # úloha bez souboru žádný výsledek nemá
    other = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "body": {"model": "gemma4:12b", "messages": []}}).json()["id"]
    wait_until(lambda: admin.get("/mgmt/v1/jobs/" + str(other)).json()["status"] == "done")
    assert admin.get("/mgmt/v1/jobs/" + str(other) + "/result").status_code == 404


def test_keys_and_provider_path(admin, client, tts, upstream):
    only_tts = admin.post("/mgmt/v1/keys", json={"name": "podcast", "role": "client",
                                                 "allowed_models": ["tts-cs"]}).json()["key"]
    no_tts = admin.post("/mgmt/v1/keys", json={"name": "chat-only", "role": "client",
                                               "allowed_providers": ["openai"]}).json()["key"]
    fresh = TestClient(client.app)  # bez `with` → bez lifespanu, sdílí app.state
    speech = {"model": "tts-cs", "input": "test"}
    assert fresh.post("/v1/audio/speech", json=speech).status_code == 401
    h = {"Authorization": "Bearer " + only_tts}
    assert fresh.post("/v1/audio/speech", headers=h, json=speech).status_code == 200
    # explicitní cesta přes /providers/tts/… funguje taky a jde přes plánovač
    assert fresh.post("/providers/tts/v1/audio/speech", headers=h, json=speech).status_code == 200
    assert sched.admitted == "tts-cs"
    r = fresh.post("/api/chat", headers=h, json={"model": "gemma4:12b", "messages": []})
    assert r.status_code == 403 and "not allowed" in r.text
    h2 = {"Authorization": "Bearer " + no_tts}
    r = fresh.post("/v1/audio/speech", headers=h2, json=speech)
    assert r.status_code == 403 and "may not use provider 'tts'" in r.text
    # klíč omezený na tts vidí v /mgmt/v1/models jen svůj model
    assert fresh.get("/mgmt/v1/models", headers=h).json()["ollama"]["models"] == []
    assert fresh.get("/mgmt/v1/models", headers=h).json()["tts"]["models"] == ["tts-cs"]


def test_models_load_for_gpu_model(admin, tts, upstream):
    r = admin.post("/mgmt/v1/models/load", json={"model": "tts-cs", "wait_s": 5})
    assert r.status_code == 200 and r.json()["loaded"] is True and r.json()["status"] == "ready", r.text
    assert upstream.tts_loaded == {"tts-cs"} and sched.admitted == "tts-cs" and sched.in_flight == {}
    assert calls(upstream, "tts.test", "/api/load")
    # už je v paměti → ready bez dalšího volání
    n = len(calls(upstream, "tts.test", "/api/load"))
    assert admin.post("/mgmt/v1/models/load", json={"model": "tts-cs"}).json()["status"] == "ready"
    assert len(calls(upstream, "tts.test", "/api/load")) == n


def test_dead_backend_does_not_block_ollama(admin, tts, upstream):
    r = admin.post("/mgmt/v1/providers", json={"slug": "deadtts", "kind": "gpu", "base_url": "http://dead.test",
                                               "models": ["tts-dead"]})
    assert r.status_code == 201
    try:
        sched.admitted = "tts-dead"      # jako by kartu drželo něco, co mezitím umřelo
        chat = {"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}], "stream": False}
        t0 = time.time()
        assert admin.post("/api/chat", json=chat).status_code == 200
        assert time.time() - t0 < 5.0
        assert sched.admitted == "gemma4:12b" and sched.last_evict_error.startswith("deadtts: ")
        st = admin.get("/mgmt/v1/models/status").json()
        assert st["backends"]["deadtts"]["ok"] is False and st["last_evict_error"].startswith("deadtts")
        assert "deadtts" in admin.get("/ui/settings").text
    finally:
        admin.delete("/mgmt/v1/providers/deadtts")


def test_purge_removes_result_files(tmp_path):
    path = tmp_path / "1.mp3"
    path.write_bytes(b"ID3")
    ids = db.create_jobs([{"batch_id": "x", "path": "/v1/audio/speech", "model": "tts-cs",
                           "request_json": "{}", "provider": "tts"}])
    db.finish_job(ids[0], "done", status_code=200, result_json="{}", result_path=str(path))
    with db.lock:
        db.conn.execute("UPDATE jobs SET finished_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (ids[0],))
        db.conn.commit()
    assert db.purge_jobs(1) >= 1 and not path.exists()
