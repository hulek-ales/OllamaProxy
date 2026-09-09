"""Fronta úloh: zadání, zpracování pracovníkem, přednost interaktivních dotazů, limity, callback."""

import time

import pytest
from fastapi.testclient import TestClient

from ollamaproxy.db import db
from ollamaproxy.jobs import RateLimiter, limiter, worker
from ollamaproxy.scheduler import sched


def wait_job(c, jid, headers=None, timeout=10.0, states=("done", "error", "cancelled")):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get("/mgmt/v1/jobs/" + str(jid), headers=headers).json()
        if j["status"] in states:
            return j
        time.sleep(0.1)
    raise AssertionError("job " + str(jid) + " stuck in " + j["status"])


@pytest.fixture
def fast(admin):
    """Krátké časy plánovače, čistý stav; po testu vrátit výchozí."""
    admin.put("/mgmt/v1/settings", json={"jobs_idle_s": 0, "sched_hold_s": 0, "jobs_preempt_s": 0,
                                         "rate_limit_per_min": 0, "jobs_enabled": True})
    sched.last_interactive_done = 0.0
    limiter.reset()
    yield
    admin.put("/mgmt/v1/settings", json={"jobs_idle_s": 60, "sched_hold_s": 10, "jobs_preempt_s": 0,
                                         "rate_limit_per_min": 0})
    limiter.reset()
    sched.last_interactive_done = 0.0


def test_rate_limiter_window():
    rl = RateLimiter()
    assert rl.hit(1, 0) is None and rl.hit(None, 5) is None
    assert rl.hit(1, 2) is None and rl.hit(1, 2) is None
    retry = rl.hit(1, 2)
    assert 1 <= retry <= 61
    assert rl.hit(2, 2) is None   # jiný klíč má vlastní okno


def test_job_roundtrip_and_log(admin, fast, upstream):
    r = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "body": {
        "model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}]}})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    job = wait_job(admin, jid)
    assert job["status"] == "done" and job["result"]["message"]["content"] == "Ahoj!"
    assert job["request"]["model"] == "gemma4:12b" and job["attempts"] == 1
    assert job["request_id"]
    row = db.get_request(job["request_id"])
    assert row["job_id"] == jid and row["prompt_tokens"] == 12 and row["key_name"] == "admin"
    sent = [c for c in upstream.calls if c.url.path == "/api/chat"][-1]
    assert b'"stream": false' in sent.content
    assert sched.in_flight == {} and sched.interactive_running == 0
    assert admin.get("/ui/jobs").status_code == 200
    assert "Ahoj!" in admin.get("/ui/jobs/" + str(jid)).text
    assert admin.get("/ui/jobs/999999").status_code == 200


def test_batch_and_key_scope(admin, client, fast):
    key = admin.post("/mgmt/v1/keys", json={"name": "agent-a", "role": "client",
                                            "allowed_models": ["gemma4"]}).json()["key"]
    c = TestClient(client.app)
    h = {"Authorization": "Bearer " + key}
    r = c.post("/mgmt/v1/jobs", headers=h, json={"jobs": [
        {"path": "/api/chat", "body": {"model": "gemma4:12b", "messages": [{"role": "user", "content": str(i)}]}}
        for i in range(3)], "priority": 7})
    assert r.status_code == 202, r.text
    batch = r.json()["batch_id"]
    ids = [j["id"] for j in r.json()["jobs"]]
    for jid in ids:
        assert wait_job(c, jid, headers=h)["status"] == "done"
    out = c.get("/mgmt/v1/jobs?batch=" + batch + "&bodies=1", headers=h).json()
    assert out["total"] == 3 and out["finished"] == 3
    assert all(j["result"]["message"]["content"] == "Ahoj!" and j["priority"] == 7 for j in out["items"])
    # klíč client vidí jen svoje úlohy; admin všechny
    assert c.get("/mgmt/v1/jobs", headers=h).json()["total"] == 3
    assert admin.get("/mgmt/v1/jobs").json()["total"] >= 4
    other = admin.get("/mgmt/v1/jobs?status=done").json()["items"][-1]["id"]
    assert other not in ids or c.get("/mgmt/v1/jobs/" + str(other), headers=h).status_code == 200
    mine_only = [j["id"] for j in admin.get("/mgmt/v1/jobs").json()["items"] if j["key_name"] == "admin"]
    assert c.get("/mgmt/v1/jobs/" + str(mine_only[0]), headers=h).status_code == 404
    # model mimo seznam klíče, špatná cesta, neznámý poskytovatel
    assert c.post("/mgmt/v1/jobs", headers=h, json={"path": "/api/chat", "body": {"model": "llama3:8b"}}).status_code == 403
    assert c.post("/mgmt/v1/jobs", headers=h, json={"path": "/api/pull", "body": {"model": "gemma4:12b"}}).status_code == 422
    assert c.post("/mgmt/v1/jobs", headers=h, json={"path": "/api/chat", "body": {"model": "gemma4:12b"},
                                                    "provider": "nope"}).status_code == 404
    assert c.post("/mgmt/v1/jobs", headers=h, json={"path": "/api/chat", "body": {"messages": []}}).status_code == 422


def test_limits_per_key(admin, client, fast):
    r = admin.post("/mgmt/v1/keys", json={"name": "agent-b", "role": "client", "max_jobs": 2, "rate_per_min": 3})
    key, kid = r.json()["key"], r.json()["id"]
    assert r.json()["max_jobs"] == 2
    c = TestClient(client.app)
    h = {"Authorization": "Bearer " + key}
    far = "2999-01-01T00:00:00+00:00"
    body = {"path": "/api/chat", "body": {"model": "gemma4:12b", "messages": []}, "not_before": far}
    a = c.post("/mgmt/v1/jobs", headers=h, json=body).json()["id"]
    b = c.post("/mgmt/v1/jobs", headers=h, json=body).json()["id"]
    r = c.post("/mgmt/v1/jobs", headers=h, json=body)
    assert r.status_code == 429 and "Retry-After" in r.headers and "too many queued" in r.text
    # zrušení uvolní místo; zrušená úloha zůstane vidět
    assert c.delete("/mgmt/v1/jobs/" + str(a), headers=h).json()["status"] == "cancelled"
    assert c.get("/mgmt/v1/jobs/" + str(a), headers=h).json()["status"] == "cancelled"
    assert c.post("/mgmt/v1/jobs", headers=h, json=body).status_code == 202
    assert c.delete("/mgmt/v1/jobs/" + str(b), headers=h).status_code == 200
    for j in c.get("/mgmt/v1/jobs?status=queued", headers=h).json()["items"]:
        c.delete("/mgmt/v1/jobs/" + str(j["id"]), headers=h)
    assert c.delete("/mgmt/v1/jobs/999999", headers=h).status_code == 404
    # limit za minutu: 3 zadání úloh už proběhla (limit sdílí úlohy i průchozí dotazy) → 429
    limiter.reset()
    chat = {"model": "gemma4:12b", "messages": [{"role": "user", "content": "x"}], "stream": False}
    assert c.post("/api/chat", headers=h, json=chat).status_code == 200
    assert c.post("/api/chat", headers=h, json=chat).status_code == 200
    assert c.post("/api/chat", headers=h, json=chat).status_code == 200
    r = c.post("/api/chat", headers=h, json=chat)
    assert r.status_code == 429 and "Retry-After" in r.headers
    rows, _ = db.query_requests({"status": "error", "key_name": "agent-b"})
    assert rows[0]["status"] == 429
    # změna limitů u existujícího klíče
    r = admin.put("/mgmt/v1/keys/" + str(kid), json={"rate_per_min": 0, "max_jobs": 0, "allowed_models": ["gemma4"]})
    assert r.json() == {"id": kid, "allowed_models": ["gemma4"], "max_jobs": 0, "rate_per_min": 0}
    assert admin.put("/mgmt/v1/keys/999999", json={"max_jobs": 1}).status_code == 404
    limiter.reset()
    assert c.post("/api/chat", headers=h, json=chat).status_code == 200


def test_interactive_has_priority_soft(admin, fast, upstream):
    """Běžící úloha se nechá doběhnout; nová úloha se nezačne, dokud interaktivní dotaz čeká."""
    jid = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "body": {
        "model": "slow", "messages": [{"role": "user", "content": "x"}]}}).json()["id"]
    wait_job(admin, jid, states=("running",), timeout=5)
    # GPU drží úloha na model "slow" → interaktivní dotaz na jiný model bez čekání dostane 503
    r = admin.post("/api/chat", headers={"X-Opx-Wait": "0"},
                   json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 503 and r.json()["scheduler"]["admitted"] == "slow"
    # s čekáním projde hned po doběhnutí úlohy (hold 0)
    t0 = time.time()
    r = admin.post("/api/chat", json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200 and time.time() - t0 < upstream.slow_s + 2
    job = wait_job(admin, jid)
    assert job["status"] == "done" and job["attempts"] == 1 and job["result"]["message"]["content"] == "pomalu"


def test_interactive_preempts_job_hard(admin, fast, upstream):
    admin.put("/mgmt/v1/settings", json={"jobs_preempt_s": 0.3})
    before = worker.preempted
    jid = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "body": {
        "model": "slow", "messages": [{"role": "user", "content": "x"}]}}).json()["id"]
    wait_job(admin, jid, states=("running",), timeout=5)
    t0 = time.time()
    r = admin.post("/api/chat", json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200
    assert time.time() - t0 < upstream.slow_s, "interaktivní dotaz měl úlohu přerušit, ne čekat na ni"
    assert worker.preempted == before + 1
    job = wait_job(admin, jid, timeout=15)
    assert job["status"] == "done" and job["attempts"] == 2
    assert sched.in_flight == {} and sched.interactive_running == 0


def test_cancel_running_job(admin, fast):
    jid = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "body": {
        "model": "slow", "messages": [{"role": "user", "content": "x"}]}}).json()["id"]
    wait_job(admin, jid, states=("running",), timeout=5)
    assert admin.delete("/mgmt/v1/jobs/" + str(jid)).json()["status"] == "cancelled"
    job = wait_job(admin, jid, timeout=5)
    assert job["status"] == "cancelled"
    time.sleep(0.3)
    assert worker.current is None and sched.in_flight == {}


def ensure_openai(admin):
    admin.post("/mgmt/v1/providers", json={
        "slug": "openai", "kind": "openai", "base_url": "https://api.openai.test",
        "api_key": "sk-test", "pricing": {"gpt-4o-mini": {"in": 0.15, "out": 0.6}}})
    admin.put("/mgmt/v1/providers/openai", json={
        "slug": "openai", "kind": "openai", "base_url": "https://api.openai.test", "api_key": "sk-test",
        "pricing": {"gpt-4o-mini": {"in": 0.15, "out": 0.6}}})


def test_callback_and_commercial_job(admin, fast, upstream):
    ensure_openai(admin)
    r = admin.post("/mgmt/v1/jobs", json={
        "path": "/v1/chat/completions", "provider": "openai", "priority": 1,
        "body": {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        "callback_url": "http://callback.test/hook"})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    job = wait_job(admin, jid)
    assert job["status"] == "done" and job["result"]["choices"][0]["message"]["content"] == "Hi there"
    end = time.time() + 5
    while time.time() < end and not upstream.callbacks:
        time.sleep(0.1)
    assert upstream.callbacks[-1]["id"] == jid and upstream.callbacks[-1]["result"]["model"].startswith("gpt-4o-mini")
    assert admin.get("/mgmt/v1/jobs/" + str(jid)).json()["callback_status"] == "HTTP 200"
    row = db.get_request(job["request_id"])
    assert row["provider"] == "openai" and row["cost_usd"] is not None and row["job_id"] == jid


def test_job_upstream_error(admin, fast, upstream):
    ensure_openai(admin)
    jid = admin.post("/mgmt/v1/jobs", json={"path": "/v1/embeddings", "provider": "openai",
                                            "body": {"model": "gpt-4o-mini", "input": "x"}}).json()["id"]
    job = wait_job(admin, jid)
    assert job["status"] == "error" and job["status_code"] == 404 and "unknown fake path" in job["error"]


def test_restart_requeues_running_and_purge():
    ids = db.create_jobs([{"batch_id": "x", "path": "/api/chat", "model": "m", "request_json": "{}"}])
    db.start_job(ids[0])
    assert db.get_job(ids[0])["status"] == "running"
    assert db.requeue_running_jobs() == 1 and db.get_job(ids[0])["status"] == "queued"
    assert db.cancel_job(ids[0]) == "cancelled"
    db.conn.execute("UPDATE jobs SET finished_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (ids[0],))
    db.conn.commit()
    assert db.purge_jobs(30) >= 1 and db.purge_jobs(0) == 0
    s = db.jobs_summary()
    assert "by_status" in s and "queued_by_model" in s


def test_status_endpoints_include_jobs(admin):
    assert "jobs" in admin.get("/mgmt/v1/models/status").json()
    assert admin.get("/mgmt/v1/health").json()["jobs"]["enabled"] is True
    page = admin.get("/ui/settings").text
    assert "Fronta úloh" in page
