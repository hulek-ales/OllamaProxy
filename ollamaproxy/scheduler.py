"""Plánovač modelů pro lokální Ollamu — 12 GB VRAM znamená jeden velký model najednou.

Když přijde dotaz na model, který v Ollamě není načtený, a zároveň běží dotazy
na jiný model, Ollama by ho stejně nechala čekat a pak model přehodila. Tady se
to dělá řízeně:

  * dotazy na model, který GPU právě drží (`admitted`), jdou rovnou;
  * dotazy na model, který je podle /api/ps načtený vedle něj (malý model se
    vejde), jdou taky rovnou;
  * ostatní čekají. Až nic neběží (a uplyne `hold_s` od posledního dokončení),
    přepne se na model, na který se čeká nejdéle, a pustí se **všechny** jeho
    čekající dotazy najednou — Ollama je odbaví bez dalšího reloadu;
  * čeká-li někdo déle než `max_wait_s`, nové dotazy na aktuální model se
    zařadí do fronty (`draining`), aby GPU vůbec někdy uvolnily.

Plánovač se týká jen inference na hlavní Ollamě (OLLAMA_UPSTREAM). Komerční
poskytovatelé ani další Ollama servery přes /providers/ nečekají.
"""

import asyncio
import time


class Waiter:
    __slots__ = ("model", "since", "event")

    def __init__(self, model: str, since: float):
        self.model = model
        self.since = since
        self.event = asyncio.Event()


class ModelScheduler:
    def __init__(self):
        self.enabled = True
        self.hold_s = 10.0
        self.max_wait_s = 90.0
        self.admitted = None          # model, kterému teď patří GPU
        self.draining = False         # někdo hladoví → nové dotazy na admitted čekají
        self.in_flight = {}           # model → počet běžících dotazů
        self.waiting = []             # Waiter v pořadí příchodu
        self.last_done = 0.0          # monotonic čas posledního dokončení
        self.switches = 0
        self.loaded_probe = None      # async () → iterable názvů modelů načtených v Ollamě
        self._ps_cache = (0.0, frozenset())

    # ------------------------------------------------------------ nastavení

    def refresh(self, db):
        """Načte parametry z tabulky settings."""
        self.enabled = db.setting("sched_enabled") == "1"
        self.hold_s = _num(db.setting("sched_hold_s"), 10.0)
        self.max_wait_s = _num(db.setting("sched_max_wait_s"), 90.0)

    # ---------------------------------------------------------------- stav

    async def loaded(self, force: bool = False) -> frozenset:
        """Modely načtené v Ollamě (/api/ps), cache 1 s."""
        ts, cached = self._ps_cache
        now = time.monotonic()
        if not force and now - ts < 1.0:
            return cached
        try:
            names = frozenset(await self.loaded_probe()) if self.loaded_probe else frozenset()
        except Exception:
            names = frozenset()
        self._ps_cache = (now, names)
        return names

    def snapshot(self) -> dict:
        now = time.monotonic()
        waiting = {}
        oldest = None
        for w in self.waiting:
            waiting[w.model] = waiting.get(w.model, 0) + 1
            oldest = w.since if oldest is None or w.since < oldest else oldest
        return {
            "enabled": self.enabled,
            "admitted": self.admitted,
            "draining": self.draining,
            "in_flight": dict(self.in_flight),
            "waiting": waiting,
            "oldest_wait_s": round(now - oldest, 1) if oldest is not None else 0.0,
            "idle_s": round(now - self.last_done, 1) if self.last_done else None,
            "hold_s": self.hold_s,
            "max_wait_s": self.max_wait_s,
            "switches": self.switches,
        }

    # ------------------------------------------------------- acquire/release

    async def acquire(self, model: str, timeout: float = None, since: float = None) -> float:
        """Počká, až smí dotaz na `model` do Ollamy, a započítá ho mezi běžící.
        Vrátí, kolik sekund čekal. Po vypršení `timeout` zvedne TimeoutError
        (dotaz pak započítaný není). `since` posune „stáří“ čekání — pro
        opakované dotazy z /models/load, aby nepřicházely o pořadí."""
        if not self.enabled or not model:
            self._start(model)
            return 0.0
        t0 = time.monotonic()
        if await self._can_run(model):
            self._start(model)
            return 0.0
        w = Waiter(model, since if since is not None else t0)
        self.waiting.append(w)
        try:
            while True:
                await self._maybe_switch()
                if w.event.is_set():
                    break
                remaining = None if timeout is None else timeout - (time.monotonic() - t0)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("model '" + model + "' is not loaded")
                tick = 1.0 if remaining is None else min(1.0, remaining)
                try:
                    await asyncio.wait_for(w.event.wait(), tick)
                except asyncio.TimeoutError:
                    pass
        except BaseException:
            if w.event.is_set():   # už jsme byli pušteni a započítáni → vrátit
                self.release(model)
            raise
        finally:
            if w in self.waiting:
                self.waiting.remove(w)
        return time.monotonic() - t0

    def release(self, model: str):
        """Dotaz doběhl. Když někdo čeká, hned zkusí přepnout."""
        if not model:
            return
        n = self.in_flight.get(model, 0) - 1
        if n > 0:
            self.in_flight[model] = n
        else:
            self.in_flight.pop(model, None)
        self.last_done = time.monotonic()
        if self.waiting:
            try:
                asyncio.get_running_loop().create_task(self._maybe_switch())
            except RuntimeError:
                pass

    # ---------------------------------------------------------------- vnitřek

    def _start(self, model):
        if model:
            self.in_flight[model] = self.in_flight.get(model, 0) + 1

    async def _can_run(self, model: str) -> bool:
        if self.admitted is None:
            self.admitted = model
            return True
        if model == self.admitted:
            return not self.draining
        if self.draining:
            return False
        loaded = await self.loaded()
        # oba se vejdou vedle sebe (Ollama je drží najednou) → není co přepínat
        return self.admitted in loaded and model in loaded

    async def _maybe_switch(self):
        if not self.waiting:
            return
        now = time.monotonic()
        oldest = min(self.waiting, key=lambda w: w.since)
        starving = now - oldest.since > self.max_wait_s
        if sum(self.in_flight.values()) > 0:
            if starving:
                self.draining = True
            return
        if not starving and self.admitted is not None and now - self.last_done < self.hold_s:
            # GPU drží model, který právě odpovídal — chvíli počkat, jestli nepřijde další tah
            if self.admitted in await self.loaded():
                return
        self._switch_to(oldest.model)

    def _switch_to(self, model: str):
        self.admitted = model
        self.draining = False
        self.switches += 1
        released = [w for w in self.waiting if w.model == model]
        self.waiting = [w for w in self.waiting if w.model != model]
        # započítat hned tady, aby další _maybe_switch nepřepnul dřív, než se dotazy rozběhnou
        self.in_flight[model] = self.in_flight.get(model, 0) + len(released)
        for w in released:
            w.event.set()


def _num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


sched = ModelScheduler()
