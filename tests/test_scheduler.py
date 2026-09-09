"""Plánovač modelů — čistě asyncio, bez HTTP."""

import asyncio

from ollamaproxy.scheduler import ModelScheduler


def run(coro):
    return asyncio.run(coro)


def make(loaded=(), hold=0.0, max_wait=90.0):
    s = ModelScheduler()
    s.hold_s = hold
    s.max_wait_s = max_wait
    state = {"loaded": set(loaded)}

    async def probe():
        return state["loaded"]

    s.loaded_probe = probe
    return s, state


def test_first_model_runs_immediately():
    async def go():
        s, _ = make()
        assert await s.acquire("a") == 0.0
        assert s.admitted == "a" and s.in_flight == {"a": 1}
        s.release("a")
        assert s.in_flight == {}
    run(go())


def test_other_model_waits_until_in_flight_done_then_all_released_at_once():
    async def go():
        s, state = make(loaded={"a"})
        await s.acquire("a")
        t1 = asyncio.create_task(s.acquire("b"))
        t2 = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0.05)
        assert not t1.done() and not t2.done()
        assert s.snapshot()["waiting"] == {"b": 2}
        s.release("a")
        await asyncio.wait_for(asyncio.gather(t1, t2), 2.0)
        assert s.admitted == "b" and s.in_flight == {"b": 2} and s.waiting == []
        assert s.switches == 1
        s.release("b"); s.release("b")
    run(go())


def test_models_loaded_side_by_side_do_not_wait():
    async def go():
        s, state = make(loaded={"big", "embed"})
        await s.acquire("big")
        assert await s.acquire("embed") == 0.0    # oba jsou v /api/ps → žádné přepínání
        assert s.admitted == "big" and s.in_flight == {"big": 1, "embed": 1}
    run(go())


def test_hold_keeps_gpu_for_recent_model():
    async def go():
        s, state = make(loaded={"a"}, hold=0.3)
        await s.acquire("a")
        s.release("a")
        t = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0.1)
        assert not t.done()                        # hold ještě běží
        waited = await asyncio.wait_for(t, 3.0)
        assert 0.2 <= waited <= 2.0 and s.admitted == "b"
    run(go())


def test_no_hold_when_admitted_model_not_actually_loaded():
    async def go():
        s, state = make(loaded=set(), hold=5.0)
        await s.acquire("a")
        s.release("a")
        assert await asyncio.wait_for(s.acquire("b"), 1.0) < 0.5
    run(go())


def test_timeout_leaves_no_trace():
    async def go():
        s, _ = make(loaded={"a"})
        await s.acquire("a")
        try:
            await s.acquire("b", timeout=0)
            assert False, "měl vypršet"
        except TimeoutError:
            pass
        assert s.waiting == [] and s.in_flight == {"a": 1}
    run(go())


def test_starvation_forces_drain_of_current_model():
    async def go():
        s, _ = make(loaded={"a"}, max_wait=0.2)
        await s.acquire("a")
        tb = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0.35)
        await s._maybe_switch()
        assert s.draining is True
        # nový dotaz na "a" už do Ollamy nejde, řadí se za "b"
        ta = asyncio.create_task(s.acquire("a"))
        await asyncio.sleep(0.05)
        assert not ta.done() and s.snapshot()["waiting"] == {"b": 1, "a": 1}
        s.release("a")                             # poslední běžící "a" doběhl
        await asyncio.wait_for(tb, 2.0)
        assert s.admitted == "b" and not s.draining and not ta.done()
        s.release("b")
        await asyncio.wait_for(ta, 2.0)            # pak se vrátí k "a"
        assert s.admitted == "a"
    run(go())


def test_client_gone_between_release_and_resume_gives_slot_back():
    """Klient zavěsí přesně ve chvíli, kdy ho plánovač pustil, ale acquire ještě nevrátil."""
    async def go():
        s, _ = make(loaded={"a"})
        await s.acquire("a")
        t = asyncio.create_task(s.acquire("b"))
        await asyncio.sleep(0.05)
        s.in_flight.clear()
        await s._maybe_switch()                    # "b" puštěno a započítáno, t ještě nepokračoval
        assert s.in_flight == {"b": 1} and t.done() is False
        t.cancel()
        try:
            await t
            got_slot = True                        # wait_for zrušení spolkl → slot má volající
        except asyncio.CancelledError:
            got_slot = False                       # zrušeno uvnitř acquire → slot vrácen
        if got_slot:
            assert s.in_flight == {"b": 1}
            s.release("b")
        assert s.in_flight == {} and s.waiting == []
    run(go())


def test_timeout_while_waiting_removes_waiter_but_keeps_others():
    async def go():
        s, _ = make(loaded={"a"})
        await s.acquire("a")
        keep = asyncio.create_task(s.acquire("b"))
        try:
            await s.acquire("c", timeout=0.1)
            assert False
        except TimeoutError:
            pass
        assert s.snapshot()["waiting"] == {"b": 1}
        s.release("a")
        await asyncio.wait_for(keep, 2.0)
        assert s.admitted == "b"
    run(go())


def test_disabled_passes_everything():
    async def go():
        s, _ = make(loaded={"a"})
        s.enabled = False
        await s.acquire("a")
        assert await s.acquire("b") == 0.0
        assert s.admitted is None
    run(go())


def test_snapshot_shape():
    s, _ = make()
    snap = s.snapshot()
    for k in ("enabled", "admitted", "draining", "in_flight", "waiting", "oldest_wait_s",
              "hold_s", "max_wait_s", "switches"):
        assert k in snap
