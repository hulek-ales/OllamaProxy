"""Vyřazení lokální Ollamy (nastavení `ollama_enabled` = 0).

Až GPU dělá něco jiného a Ollama server je zrušený, nesmí se proxy tvářit, že je
rozbitá: stav hlásí „nepoužívá se“, na upstream se nic neposílá, katalog ji
nenabízí a úlohy pro ni se odmítnou hned. Poskytovatelé a GPU služby jedou dál.
"""

import time

import pytest

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


def ollama_calls(upstream):
    return [c for c in upstream.calls if c.url.host == "ollama.test"]


@pytest.fixture
def off(admin):
    """Lokální Ollama vypnutá; po testu zpátky zapnutá."""
    assert admin.put("/mgmt/v1/settings", json={"ollama_enabled": False}).status_code == 200
    assert db.ollama_on() is False
    yield
    admin.put("/mgmt/v1/settings", json={"ollama_enabled": True})
    assert db.ollama_on() is True


def test_status_page_calls_it_off_not_broken(admin, upstream, off):
    before = len(ollama_calls(upstream))
    page = admin.get("/ui/stav").text
    assert "nepoužívá se" in page
    assert "Lokální Ollama se nepoužívá" in page
    assert "neodpovídá na" not in page           # mlčící upstream už není chyba
    assert "mlčí" not in page
    assert len(ollama_calls(upstream)) == before  # a vůbec se nezkouší

    data = admin.get("/ui/stav.json").json()
    assert data["ollama"]["enabled"] is False
    assert data["ollama"]["placement"] == "off"
    assert data["ollama"]["error"] is None


def test_log_page_drops_the_red_banner(admin, off):
    log = admin.get("/ui").text
    assert "Ollama neodpovídá" not in log
    assert "počítá <b>na CPU</b>" not in log
    assert "nepoužívá se" in log                  # pruh to přiznává, ale mezi fakty


def test_catalog_no_longer_offers_the_local_ollama(admin, off):
    cat = admin.get("/mgmt/v1/models").json()
    assert "ollama" not in cat


def test_catalog_has_it_back_when_switched_on(admin):
    cat = admin.get("/mgmt/v1/models").json()
    assert "ollama" in cat and "gemma4:12b" in cat["ollama"]["models"]


def test_root_path_says_what_is_wrong_instead_of_timing_out(admin, upstream, off):
    before = len(ollama_calls(upstream))
    r = admin.post("/api/chat", json={"model": "gemma4:12b", "messages": []})
    assert r.status_code == 503
    assert "disabled" in r.json()["error"]
    assert len(ollama_calls(upstream)) == before   # na mrtvý upstream se nešlo


def test_model_lists_stay_valid_but_empty(admin, off):
    assert admin.get("/api/tags").json() == {"models": []}
    assert admin.get("/v1/models").json() == {"object": "list", "data": []}


def test_job_for_the_local_ollama_is_refused_right_away(admin, off):
    r = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "provider": "ollama",
                                          "body": {"model": "gemma4:12b", "messages": []}})
    assert r.status_code == 422
    assert "ollama_enabled" in r.json()["detail"]


def test_queued_job_fails_fast_when_the_switch_flips(admin, upstream):
    """Úloha založená ještě za živé Ollamy nesmí po vypnutí viset ve frontě."""
    admin.put("/mgmt/v1/settings", json={"jobs_enabled": False})
    limiter.reset()
    jid = admin.post("/mgmt/v1/jobs", json={"path": "/api/chat", "provider": "ollama",
                                            "body": {"model": "gemma4:12b", "messages": []}}).json()["id"]
    before = len(ollama_calls(upstream))
    admin.put("/mgmt/v1/settings", json={"ollama_enabled": False, "jobs_enabled": True})
    try:
        assert wait_until(lambda: admin.get("/mgmt/v1/jobs/" + str(jid)).json()["status"] == "error")
        assert "ollama_enabled" in admin.get("/mgmt/v1/jobs/" + str(jid)).json()["error"]
        assert len(ollama_calls(upstream)) == before
    finally:
        admin.put("/mgmt/v1/settings", json={"ollama_enabled": True})


def test_gpu_service_still_runs_without_the_local_ollama(admin, upstream, off):
    """Kvůli tomu to celé je: kartu dál obsluhuje TTS, jen už ji s nikým nesdílí."""
    assert admin.post("/mgmt/v1/providers", json={"slug": "tts2", "kind": "gpu",
                                                  "base_url": "http://tts.test",
                                                  "models": ["tts-cs"]}).status_code == 201
    sched.admitted = None
    sched.in_flight.clear()
    sched.waiting.clear()
    sched._ps_cache = (0.0, frozenset())
    before = len(ollama_calls(upstream))
    try:
        r = admin.post("/v1/audio/speech", json={"model": "tts-cs", "input": "ahoj", "voice": "x"})
        assert r.status_code == 200 and r.content.startswith(b"ID3")
        assert len(ollama_calls(upstream)) == before   # žádné uvolňování VRAM mrtvé Ollamy
    finally:
        admin.delete("/mgmt/v1/providers/tts2")
        sched.admitted = None
        sched.in_flight.clear()
        sched._ps_cache = (0.0, frozenset())
