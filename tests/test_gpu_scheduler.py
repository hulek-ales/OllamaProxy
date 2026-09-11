"""Plánovač s více backendy na jedné kartě: přepnutí na model jiného backendu až po
uvolnění VRAM (evictor), nikdy vedle sebe, mrtvý backend neblokuje navždy."""

import asyncio

from ollamaproxy.scheduler import ModelScheduler


def run(coro):
    return asyncio.run(coro)


def backend_of(model):
    return "tts" if model and model.startswith("tts") else "ollama"


def make(loaded=(), hold=0.0, evict_timeout=2.0, evictor="ok"):
    """`evictor`: "ok" = uvolní modely backendu ze `state['loaded']`, "fail" = zvedne výjimku,
    "hang" = nikdy neskončí, None = žádný (přepíná se rovnou)."""
    s = ModelScheduler()
    s.hold_s = hold
    s.max_wait_s = 90.0
    s.evict_timeout_s = evict_timeout
    s.backend_of = backend_of
    state = {"loaded": set(loaded), "evicted": []}

    async def probe():
        return state["loaded"]

    async def evict(slug):
        state["evicted"].append(slug)
        if evictor == "fail":
            raise RuntimeError("backend down")
        if evictor == "hang":
            await asyncio.sleep(3600)
        await asyncio.sleep(0.05)
        state["loaded"] = {m for m in state["loaded"] if backend_of(m) != slug}

    s.loaded_probe = probe
    s.evictor = evict if evictor else None
    return s, state


def test_switch_to_other_backend_waits_for_eviction():
    async def go():
        s, state = make(loaded={"a"})
        await s.acquire("a")
        s.release("a")
        t = asyncio.create_task(s.acquire("tts-cs"))
        await asyncio.sleep(0.02)
        # probíhá uvolňování VRAM Ollamy: nikdo nesmí na kartu, ani nový dotaz na starý model
        assert s.evicting == "tts-cs" and not t.done() and state["evicted"] == ["ollama"]
        old = asyncio.create_task(s.acquire("a"))
        await asyncio.sleep(0.02)
        assert not old.done()
        await asyncio.wait_for(t, 2.0)
        assert s.admitted == "tts-cs" and s.evicting is None and s.evictions == 1
        assert s.in_flight == {"tts-cs": 1} and state["loaded"] == set()
        assert s.snapshot()["backend"] == "tts"
        # zpátky na Ollamu: teď se uvolňuje TTS
        state["loaded"] = {"tts-cs"}
        s.release("tts-cs")
        await asyncio.wait_for(old, 2.0)
        assert s.admitted == "a" and state["evicted"] == ["ollama", "tts"] and s.evictions == 2
        assert state["loaded"] == set()
    run(go())


def test_models_of_different_backends_never_run_side_by_side():
    async def go():
        s, state = make(loaded={"a"})
        await s.acquire("a")
        state["loaded"] = {"a", "tts-cs"}           # i kdyby oba hlásily „načteno“
        s._ps_cache = (0.0, frozenset())
        t = asyncio.create_task(s.acquire("tts-cs"))
        await asyncio.sleep(0.05)
        assert not t.done() and s.snapshot()["waiting"] == {"tts-cs": 1}
        s.release("a")
        await asyncio.wait_for(t, 2.0)
        assert s.admitted == "tts-cs" and state["evicted"] == ["ollama"]
    run(go())


def test_same_backend_switch_does_not_evict():
    async def go():
        s, state = make(loaded={"a"})
        await s.acquire("a")
        t = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0.02)
        s.release("a")
        await asyncio.wait_for(t, 2.0)
        assert s.admitted == "b" and state["evicted"] == [] and s.evictions == 0
    run(go())


def test_evictor_failure_switches_anyway_and_keeps_error():
    async def go():
        s, state = make(loaded={"a"}, evictor="fail")
        await s.acquire("a")
        s.release("a")
        assert await asyncio.wait_for(s.acquire("tts-cs"), 2.0) < 1.0
        assert s.admitted == "tts-cs" and s.last_evict_error == "ollama: backend down"
        assert s.snapshot()["last_evict_error"] == "ollama: backend down"
    run(go())


def test_evictor_timeout_switches_anyway():
    async def go():
        s, state = make(loaded={"a"}, evictor="hang", evict_timeout=0.1)
        await s.acquire("a")
        s.release("a")
        await asyncio.wait_for(s.acquire("tts-cs"), 2.0)
        assert s.admitted == "tts-cs" and s.last_evict_error.startswith("ollama: ")
        assert "Timeout" in s.last_evict_error
    run(go())


def test_foreign_model_in_memory_is_adopted_and_evicted_first():
    """Po startu proxy nic nedrží (`admitted` None), ale v paměti TTS služby zůstal model."""
    async def go():
        s, state = make(loaded={"tts-cs"})
        waited = await asyncio.wait_for(s.acquire("a"), 2.0)
        assert waited > 0 and s.admitted == "a" and state["evicted"] == ["tts"]
        assert state["loaded"] == set() and s.in_flight == {"a": 1}
    run(go())


def test_without_evictor_switch_is_direct():
    async def go():
        s, state = make(loaded={"a"}, evictor=None)
        await s.acquire("a")
        s.release("a")
        assert await asyncio.wait_for(s.acquire("tts-cs"), 1.0) < 0.5
        assert s.admitted == "tts-cs" and state["evicted"] == [] and s.evicting is None
    run(go())


def test_timeout_while_evicting_leaves_clean_state():
    async def go():
        s, state = make(loaded={"a"}, evictor="hang", evict_timeout=0.3)
        await s.acquire("a")
        s.release("a")
        try:
            await s.acquire("tts-cs", timeout=0.05)
            assert False, "měl vypršet"
        except TimeoutError:
            pass
        assert s.waiting == [] and s.evicting == "tts-cs"     # uvolňování běží dál
        await asyncio.sleep(0.4)
        assert s.evicting is None and s.admitted == "tts-cs" and s.in_flight == {}
        # další dotaz na TTS jde rovnou, karta už je jeho
        assert await s.acquire("tts-cs") == 0.0
    run(go())


def test_snapshot_has_backend_fields():
    s, _ = make()
    snap = s.snapshot()
    for k in ("backend", "evicting", "evictions", "last_evict_error", "evict_timeout_s"):
        assert k in snap
