"""Plánovač modelů pro lokální GPU — 12 GB VRAM znamená jeden velký model najednou.

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

Kartu nedrží jen Ollama. GPU služby (poskytovatel typu `gpu`: TTS, Whisper…)
jsou další *backend* se svými modely; `backend_of(model)` říká, kam model patří.
Modely různých backendů nikdy neběží vedle sebe: před přepnutím na model jiného
backendu proxy starému backendu řekne, ať uvolní VRAM (`evictor`), počká, až je
opravdu prázdná, a teprve pak pustí čekající dotazy. Po dobu uvolňování
(`evicting`) nejde na kartu nic. Když uvolnění selže nebo vyprší
`evict_timeout_s`, přepne se i tak a chyba zůstane v `last_evict_error` — mrtvá
služba nesmí zablokovat Ollamu natrvalo.

Plánovač se týká inference na hlavní Ollamě (OLLAMA_UPSTREAM) a GPU služeb.
Komerční poskytovatelé ani další Ollama servery přes /providers/ nečekají.
"""

import asyncio
import time


class Waiter:
    __slots__ = ("model", "since", "event", "interactive")

    def __init__(self, model: str, since: float, interactive: bool = True):
        self.model = model
        self.since = since
        self.interactive = interactive
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
        self.last_done = 0.0          # monotonic čas posledního dokončení (interaktivní i úlohy)
        self.last_interactive_done = 0.0
        self.interactive_running = 0  # kolik z in_flight jsou interaktivní dotazy (ne úlohy)
        self.switches = 0
        self.loaded_probe = None      # async () → iterable názvů modelů načtených (Ollama + GPU služby)
        self._ps_cache = (0.0, frozenset())
        # více backendů na jedné kartě: "ollama" (hlavní upstream) nebo slug GPU služby
        self.backend_of = lambda model: "ollama"
        self.evictor = None           # async (slug) → None: uvolní VRAM backendu a počká, až je prázdná
        self.evict_timeout_s = 60.0
        self.evicting = None          # model, na který se přepíná; starý backend zrovna uvolňuje VRAM
        self.evictions = 0
        self.last_evict_error = None
        self._evict_task = None

    # ------------------------------------------------------------ nastavení

    def refresh(self, db):
        """Načte parametry z tabulky settings."""
        self.enabled = db.setting("sched_enabled") == "1"
        self.hold_s = _num(db.setting("sched_hold_s"), 10.0)
        self.max_wait_s = _num(db.setting("sched_max_wait_s"), 90.0)
        self.evict_timeout_s = _num(db.setting("gpu_evict_timeout_s"), 60.0)

    # ---------------------------------------------------------------- stav

    async def loaded(self, force: bool = False) -> frozenset:
        """Modely načtené v paměti (Ollama /api/ps + GPU služby), cache 1 s."""
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
            "backend": self.backend_of(self.admitted) if self.admitted else None,
            "draining": self.draining,
            "evicting": self.evicting,
            "in_flight": dict(self.in_flight),
            "interactive_running": self.interactive_running,
            "interactive_waiting": sum(1 for w in self.waiting if w.interactive),
            "waiting": waiting,
            "oldest_wait_s": round(now - oldest, 1) if oldest is not None else 0.0,
            "idle_s": round(now - self.last_done, 1) if self.last_done else None,
            "interactive_idle_s": (round(now - self.last_interactive_done, 1)
                                   if self.last_interactive_done else None),
            "hold_s": self.hold_s,
            "max_wait_s": self.max_wait_s,
            "evict_timeout_s": self.evict_timeout_s,
            "switches": self.switches,
            "evictions": self.evictions,
            "last_evict_error": self.last_evict_error,
        }

    # ------------------------------------------------------- acquire/release

    def interactive_pending(self) -> bool:
        """Běží nebo čeká nějaký interaktivní dotaz (ne úloha z fronty)?"""
        return self.interactive_running > 0 or any(w.interactive for w in self.waiting)

    def oldest_interactive_wait_s(self) -> float:
        now = time.monotonic()
        waits = [now - w.since for w in self.waiting if w.interactive]
        return max(waits) if waits else 0.0

    async def acquire(self, model: str, timeout: float = None, since: float = None,
                      interactive: bool = True) -> float:
        """Počká, až smí dotaz na `model` na GPU, a započítá ho mezi běžící.
        Vrátí, kolik sekund čekal. Po vypršení `timeout` zvedne TimeoutError
        (dotaz pak započítaný není). `since` posune „stáří“ čekání — pro
        opakované dotazy z /models/load, aby nepřicházely o pořadí.
        `interactive=False` = úloha z fronty (jobs.py): má nižší prioritu a
        nedrží GPU po dokončení (hold platí jen po interaktivním dotazu)."""
        if not self.enabled or not model:
            self._start(model, interactive)
            return 0.0
        t0 = time.monotonic()
        if await self._can_run(model):
            self._start(model, interactive)
            return 0.0
        w = Waiter(model, since if since is not None else t0, interactive)
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
                self.release(model, interactive)
            raise
        finally:
            if w in self.waiting:
                self.waiting.remove(w)
        return time.monotonic() - t0

    def release(self, model: str, interactive: bool = True):
        """Dotaz doběhl. Když někdo čeká, hned zkusí přepnout."""
        if not model:
            return
        n = self.in_flight.get(model, 0) - 1
        if n > 0:
            self.in_flight[model] = n
        else:
            self.in_flight.pop(model, None)
        self.last_done = time.monotonic()
        if interactive:
            self.interactive_running = max(0, self.interactive_running - 1)
            self.last_interactive_done = self.last_done
        if self.waiting:
            try:
                asyncio.get_running_loop().create_task(self._maybe_switch())
            except RuntimeError:
                pass

    # ---------------------------------------------------------------- vnitřek

    def _start(self, model, interactive: bool = True):
        if model:
            self.in_flight[model] = self.in_flight.get(model, 0) + 1
            if interactive:
                self.interactive_running += 1

    async def _foreign_loaded(self, model: str):
        """Model jiného backendu, který je v paměti (např. z doby před startem proxy)."""
        mine = self.backend_of(model)
        for name in await self.loaded():
            if self.backend_of(name) != mine:
                return name
        return None

    async def _can_run(self, model: str) -> bool:
        if self.evicting:
            return False
        if self.admitted is None:
            foreign = await self._foreign_loaded(model)
            if foreign is not None:
                self.admitted = foreign    # kartu drží cizí backend → řádné přepnutí s uvolněním VRAM
                return False
            self.admitted = model
            return True
        if model == self.admitted:
            return not self.draining
        if self.draining:
            return False
        if self.backend_of(model) != self.backend_of(self.admitted):
            return False                   # jiný backend nikdy vedle sebe, nejdřív uvolnit VRAM
        loaded = await self.loaded()
        # oba se vejdou vedle sebe (Ollama je drží najednou) → není co přepínat
        return self.admitted in loaded and model in loaded

    async def _maybe_switch(self):
        if not self.waiting or self.evicting:
            return
        now = time.monotonic()
        # interaktivní dotazy mají přednost před úlohami z fronty
        pool = [w for w in self.waiting if w.interactive] or self.waiting
        oldest = min(pool, key=lambda w: w.since)
        starving = now - oldest.since > self.max_wait_s
        if sum(self.in_flight.values()) > 0:
            if starving:
                self.draining = True
            return
        if (not starving and self.admitted is not None
                and now - self.last_interactive_done < self.hold_s):
            # GPU drží model, který právě odpovídal člověku — chvíli počkat na další tah
            if self.admitted in await self.loaded():
                return
        self._begin_switch(oldest.model)

    def _begin_switch(self, model: str):
        """Přepnutí na `model`; na jiný backend až po uvolnění VRAM toho starého."""
        old = self.admitted
        if (old is not None and self.evictor is not None
                and self.backend_of(old) != self.backend_of(model)):
            self.evicting = model
            self._evict_task = asyncio.get_running_loop().create_task(self._evict_and_switch(old, model))
            return
        self._switch_to(model)

    async def _evict_and_switch(self, old: str, model: str):
        slug = self.backend_of(old)
        try:
            await asyncio.wait_for(self.evictor(slug), self.evict_timeout_s)
            self.last_evict_error = None
        except Exception as exc:
            self.last_evict_error = slug + ": " + (str(exc) or exc.__class__.__name__)
            print("[sched] backend '" + slug + "' neuvolnil VRAM: " + self.last_evict_error
                  + " — přepínám i tak", flush=True)
        finally:
            self.evicting = None
            self._ps_cache = (0.0, frozenset())   # obsah paměti se změnil
        self.evictions += 1
        self._switch_to(model)

    def _switch_to(self, model: str):
        self.admitted = model
        self.draining = False
        self.switches += 1
        released = [w for w in self.waiting if w.model == model]
        self.waiting = [w for w in self.waiting if w.model != model]
        # započítat hned tady, aby další _maybe_switch nepřepnul dřív, než se dotazy rozběhnou
        if released:
            self.in_flight[model] = self.in_flight.get(model, 0) + len(released)
            self.interactive_running += sum(1 for w in released if w.interactive)
        for w in released:
            w.event.set()


def _num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


sched = ModelScheduler()
