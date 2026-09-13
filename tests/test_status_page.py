"""Stránka Stav: pozná se z ní, že Ollama spadla na CPU nebo že stojí fronta."""

from ollamaproxy.db import db


def test_status_page_shows_what_holds_the_card(admin, upstream):
    upstream.vram_ratio = 1.0
    page = admin.get("/ui/stav").text
    assert "Stav serveru" in page
    assert "gemma4:12b" in page
    assert "celý na GPU" in page
    assert "Q4_K_M" in page                      # kvantizace z /api/ps details
    assert 'http-equiv="refresh"' in page        # živý pohled se sám načítá


def test_cpu_fallback_is_called_out_on_both_pages(admin, upstream):
    """Ollama po restartu bez karty naběhne dál a tiše počítá na CPU."""
    upstream.vram_ratio = 0.0
    try:
        page = admin.get("/ui/stav").text
        assert "na CPU — Ollama nepočítá na kartě" in page
        assert "nejspíš nevidí kartu" in page

        log = admin.get("/ui").text             # a je to vidět i nad logem
        assert "počítá <b>na CPU</b>" in log

        data = admin.get("/ui/stav.json").json()
        assert data["ollama"]["placement"] == "cpu"
        assert data["ollama"]["models"][0]["vram_pct"] == 0.0
    finally:
        upstream.vram_ratio = 1.0


def test_split_placement_is_a_warning_not_an_error(admin, upstream):
    upstream.vram_ratio = 0.6
    try:
        page = admin.get("/ui/stav").text
        assert "vešel jen částí" in page
        assert "60" in page
    finally:
        upstream.vram_ratio = 1.0


def test_disabled_job_worker_is_the_first_thing_you_see(admin):
    """Nejčastější důvod, proč fronta „nejede“: vypnutý pracovník."""
    db.set_setting("jobs_enabled", "0")
    try:
        page = admin.get("/ui/stav").text
        assert "Fronta úloh je vypnutá" in page
        assert "jobs_enabled" in page
    finally:
        db.set_setting("jobs_enabled", "1")
    assert "Fronta úloh je vypnutá" not in admin.get("/ui/stav").text


def test_status_json_carries_scheduler_and_queue(admin):
    data = admin.get("/ui/stav.json").json()
    assert set(data) >= {"ollama", "sched", "jobs", "host", "backends", "active", "notes"}
    assert data["ollama"]["ok"] is True
    assert "admitted" in data["sched"] and "by_status" in data["jobs"]
