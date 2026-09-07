import json

from fastapi.testclient import TestClient

from ollamaproxy.db import db


def test_login_required_for_ui(client):
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/login")


def test_wrong_password(client):
    r = client.post("/ui/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 200 and "Špatné" in r.text


def test_default_password_banner(admin):
    r = admin.get("/ui")
    assert r.status_code == 200
    assert "výchozím heslem" in r.text


def test_ollama_passthrough_and_logging(admin, upstream):
    r = admin.get("/api/tags")
    assert r.status_code == 200 and r.json()["models"][0]["name"] == "gemma4:12b"

    r = admin.post("/api/chat", json={"model": "gemma4:12b", "messages": [{"role": "user", "content": "ahoj"}]})
    assert r.status_code == 200
    lines = [json.loads(l) for l in r.text.strip().split("\n")]
    assert lines[-1]["done"] is True

    rows, total = db.query_requests({"provider": "ollama"}, with_bodies=True)
    row = rows[0]
    assert row["model"] == "gemma4:12b"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (12, 2)
    assert row["tokens_per_sec"] == 2.0
    assert row["placement"] == "gpu" and row["vram_pct"] == 100.0
    assert row["response_text"] == "Ahoj!"
    assert row["concurrent"] == 1
    assert row["key_name"] == "admin"  # session je principal


def test_ollama_upstream_down_gives_502(admin, monkeypatch):
    import httpx

    def boom(request):
        raise httpx.ConnectError("refused")

    real = admin.app.state.client
    admin.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        r = admin.post("/api/generate", json={"model": "x", "prompt": "y"})
    finally:
        admin.app.state.client = real
    assert r.status_code == 502
    rows, _ = db.query_requests({"status": "error"})
    assert rows[0]["status"] == 502 and "unreachable" in rows[0]["error"]


def test_mgmt_requires_auth(client):
    # bez cookie: TestClient sdílí cookie jar, proto nový klient bez session
    fresh = TestClient(client.app)  # bez `with` → bez lifespanu, sdílí app.state
    if True:
        assert fresh.get("/mgmt/v1/requests").status_code == 401
        assert fresh.post("/providers/openai/v1/chat/completions", json={}).status_code == 404  # neexistuje ještě


def test_keys_and_roles(admin, client):
    r = admin.post("/mgmt/v1/keys", json={"name": "claude-debug", "role": "admin"})
    assert r.status_code == 201
    admin_key = r.json()["key"]
    assert admin_key.startswith("opx_")

    r = admin.post("/mgmt/v1/keys", json={"name": "app", "role": "client", "allowed_providers": ["openai"]})
    client_key = r.json()["key"]

    fresh = TestClient(client.app)  # bez `with` → bez lifespanu, sdílí app.state
    if True:
        h_admin = {"Authorization": "Bearer " + admin_key}
        h_client = {"Authorization": "Bearer " + client_key}
        assert fresh.get("/mgmt/v1/requests?since=24h", headers=h_admin).status_code == 200
        assert fresh.get("/mgmt/v1/stats", headers=h_admin).json()["total"]["n"] >= 1
        assert fresh.get("/mgmt/v1/keys", headers=h_admin).status_code == 200
        assert fresh.get("/mgmt/v1/keys", headers=h_client).status_code == 403
        assert fresh.get("/mgmt/v1/requests", headers=h_client).status_code == 200
        assert fresh.get("/mgmt/v1/requests", headers={"Authorization": "Bearer opx_bogus"}).status_code == 401
        # holé Ollama API bez klíče jde (výchozí), s naším klíčem se klíč do Ollamy nepřepošle
        assert fresh.get("/api/tags").status_code == 200
        r = fresh.get("/api/tags", headers=h_client)
        assert r.status_code == 200
        # vyžadovat klíč pro Ollamu
        assert fresh.put("/mgmt/v1/settings", json={"ollama_require_key": True}, headers=h_admin).status_code == 200
        assert fresh.get("/api/tags").status_code == 401
        assert fresh.get("/api/tags", headers=h_client).status_code == 200
        fresh.put("/mgmt/v1/settings", json={"ollama_require_key": False}, headers=h_admin)

    # klíče se v seznamu ukazují jen prefixem
    keys = admin.get("/mgmt/v1/keys").json()
    assert all("key_hash" not in k for k in keys)
    names = {k["name"]: k for k in keys}
    assert names["app"]["allowed_providers"] == "openai"



def test_commercial_provider_roundtrip(admin, client, upstream):
    r = admin.post("/mgmt/v1/providers", json={
        "slug": "openai", "name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.test",
        "api_key": "sk-real-secret", "pricing": {"gpt-4o-mini": {"in": 0.15, "out": 0.6}}})
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["api_key_masked"] == "sk-r…cret" and "sk-real-secret" not in r.text
    assert view["client_base_url"].endswith("/providers/openai/v1")

    r = admin.post("/mgmt/v1/providers", json={
        "slug": "anthropic", "kind": "anthropic", "base_url": "https://api.anthropic.test", "api_key": "ant-key"})
    assert r.status_code == 201

    key = admin.post("/mgmt/v1/keys", json={"name": "app2", "role": "client", "allowed_providers": ["openai"]}).json()["key"]

    fresh = TestClient(client.app)  # bez `with` → bez lifespanu, sdílí app.state
    if True:
        # bez klíče → 401
        assert fresh.post("/providers/openai/v1/chat/completions", json={"model": "gpt-4o-mini"}).status_code == 401
        h = {"Authorization": "Bearer " + key}
        # streamovaný chat: proxy doplní include_usage a nahradí Authorization skutečným klíčem
        r = fresh.post("/providers/openai/v1/chat/completions", headers=h,
                       json={"model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert "data: [DONE]" in r.text
        sent = upstream.calls[-1]
        assert sent.headers["authorization"] == "Bearer sk-real-secret"
        assert json.loads(sent.content)["stream_options"] == {"include_usage": True}
        # klíč omezený na openai nesmí na anthropic
        assert fresh.post("/providers/anthropic/v1/messages", headers=h, json={}).status_code == 403
        # non-stream
        r = fresh.post("/providers/openai/v1/chat/completions", headers=h,
                       json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        assert r.json()["choices"][0]["message"]["content"] == "Hi there"
        # neznámý poskytovatel
        assert fresh.get("/providers/nope/v1/models", headers=h).status_code == 404

    rows, _ = db.query_requests({"provider": "openai"}, with_bodies=True)
    assert len(rows) == 2
    for row in rows:
        assert row["model"] == "gpt-4o-mini-2024-07-18"
        assert (row["prompt_tokens"], row["completion_tokens"]) == (10, 5)
        assert row["key_name"] == "app2"
        assert row["placement"] is None
        assert abs(row["cost_usd"] - (10 * 0.15 + 5 * 0.6) / 1e6) < 1e-9
    assert rows[0]["response_text"] == "Hi there"
    assert rows[1]["response_text"] == "Hi"


def test_anthropic_stream_logged(admin, client):
    key = admin.post("/mgmt/v1/keys", json={"name": "claude-app", "role": "client"}).json()["key"]
    fresh = TestClient(client.app)  # bez `with` → bez lifespanu, sdílí app.state
    if True:
        r = fresh.post("/providers/anthropic/v1/messages", headers={"x-api-key": key},
                       json={"model": "claude-x", "stream": True, "max_tokens": 10,
                             "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "message_stop" in r.text
    rows, _ = db.query_requests({"provider": "anthropic"}, with_bodies=True)
    row = rows[0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 3)
    assert row["response_text"] == "Zdravim"
    assert row["key_name"] == "claude-app"


def test_models_endpoint(admin):
    out = admin.get("/mgmt/v1/models").json()
    assert out["ollama"]["models"] == ["gemma4:12b"]
    assert out["openai"]["models"] == ["gpt-4o-mini"]


def test_provider_update_keeps_key(admin):
    r = admin.put("/mgmt/v1/providers/openai", json={
        "slug": "openai", "name": "OpenAI2", "kind": "openai", "base_url": "https://api.openai.test"})
    assert r.status_code == 200 and r.json()["has_key"] is True and r.json()["name"] == "OpenAI2"
    r = admin.put("/mgmt/v1/providers/openai", json={
        "slug": "openai", "kind": "openai", "base_url": "https://api.openai.test", "api_key": ""})
    assert r.json()["has_key"] is False


def test_ui_pages_render(admin):
    for path in ("/ui", "/ui/providers", "/ui/keys", "/ui/settings", "/ui/r/1", "/ui/r/999999",
                 "/ui?provider=openai&since=24h&status=error"):
        r = admin.get(path)
        assert r.status_code == 200, path
    assert "gemma4:12b" in admin.get("/ui").text
    assert admin.get("/mgmt/openapi.json").status_code == 200


def test_ui_forms(admin):
    csrf = admin.get("/ui/keys").text.split('name="csrf" value="')[1].split('"')[0]
    r = admin.post("/ui/keys", data={"csrf": csrf, "name": "gui-key", "role": "client"})
    assert r.status_code == 200 and "opx_" in r.text
    r = admin.post("/ui/keys", data={"csrf": "bad", "name": "x", "role": "client"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    # poskytovatel přes GUI
    r = admin.post("/ui/providers", data={"csrf": csrf, "slug": "groq", "kind": "openai",
                                           "base_url": "https://api.groq.test", "api_key": "g", "enabled": "1",
                                           "inject_usage": "1", "pricing": ""}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert db.get_provider("groq")["api_key"] == "g"
    # změna hesla
    r = admin.post("/ui/settings/password", data={"csrf": csrf, "current": "admin123", "new": "noveheslo1",
                                                   "again": "noveheslo1"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert db.get_user_by_name("admin")["must_change_pw"] == 0
    assert "výchozím heslem" not in admin.get("/ui").text
    db.update_password(db.get_user_by_name("admin")["id"], __import__("ollamaproxy.auth").auth.hash_password("admin123"))


def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["placement"] == "gpu"


def test_retention_purge():
    db.log_request({"ts": "2000-01-01T00:00:00+00:00", "endpoint": "/api/chat", "model": "old", "status": 200})
    assert db.purge(30) >= 1
    assert db.purge(0) == 0
