"""Stav hostitele a Ollamy v okamžiku příchodu dotazu — kvůli diagnostice výpadků."""

import threading

import httpx

_active = 0
_active_lock = threading.Lock()


def bump(delta: int) -> int:
    """Počítadlo souběžně běžících dotazů."""
    global _active
    with _active_lock:
        _active += delta
        return _active


def active() -> int:
    return _active


def host_snapshot() -> dict:
    """Load a paměť hostitele — kontejner čte /proc přímo."""
    out = {"load1": None, "load5": None, "mem_avail_pct": None}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = int(val.split()[0])
        if mem.get("MemTotal"):
            out["mem_avail_pct"] = round(100.0 * mem["MemAvailable"] / mem["MemTotal"], 1)
    except Exception:
        pass
    return out


def placement_of(pct) -> str:
    """Z podílu modelu ve VRAM udělá verdikt: gpu / split / cpu."""
    if pct is None:
        return "unknown"
    if pct >= 99:
        return "gpu"
    if pct < 1:
        return "cpu"
    return "split"


async def ollama_status(client: httpx.AsyncClient, upstream: str, light: bool = False) -> dict:
    """Živý stav Ollamy pro GUI: verze, co drží v paměti a jestli to je na kartě.

    `size_vram` z /api/ps je jediná pravda o tom, kde model počítá — Ollama po
    restartu bez viditelné karty naběhne dál, jen tiše na CPU, a pozná se to
    přesně tady (`size_vram` = 0)."""
    out = {"ok": False, "upstream": upstream, "error": None, "version": None,
           "models": [], "installed": None, "placement": "none", "vram_bytes": 0}
    try:
        resp = await client.get(upstream + "/api/ps", timeout=2.0)
        resp.raise_for_status()
        running = resp.json().get("models") or []
        out["ok"] = True
    except Exception as exc:
        out["error"] = str(exc) or exc.__class__.__name__
        return out

    worst = None
    for m in running:
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        pct = round(100.0 * vram / size, 1) if size else None
        details = m.get("details") or {}
        out["models"].append({
            "name": m.get("name") or m.get("model"),
            "size": size, "size_vram": vram, "vram_pct": pct,
            "placement": placement_of(pct), "expires_at": m.get("expires_at"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "context_length": m.get("context_length"),
        })
        out["vram_bytes"] += vram
        # verdikt za celou Ollamu = nejhorší z běžících modelů
        rank = {"cpu": 0, "split": 1, "gpu": 2, "unknown": 3}
        if worst is None or rank[placement_of(pct)] < rank[worst]:
            worst = placement_of(pct)
    if worst:
        out["placement"] = worst

    if light:                 # pruh nad logem chce jen /api/ps, ne tři dotazy
        return out
    try:
        resp = await client.get(upstream + "/api/version", timeout=2.0)
        out["version"] = (resp.json() or {}).get("version")
    except Exception:
        pass
    try:
        resp = await client.get(upstream + "/api/tags", timeout=3.0)
        out["installed"] = len((resp.json() or {}).get("models") or [])
    except Exception:
        pass
    return out


async def gpu_snapshot(client: httpx.AsyncClient, upstream: str, want_model: str = None) -> dict:
    """Kde je model nahraný — /api/ps řekne size vs size_vram."""
    blank = {"placement": "unknown", "vram_pct": None, "loaded_model": None}
    try:
        resp = await client.get(upstream + "/api/ps", timeout=2.0)
        models = resp.json().get("models") or []
    except Exception:
        return blank

    if not models:
        return {"placement": "none", "vram_pct": None, "loaded_model": None}

    entry = None
    if want_model:
        for m in models:
            if m.get("name") == want_model or m.get("model") == want_model:
                entry = m
                break
    if entry is None:
        entry = models[0]

    size = entry.get("size") or 0
    vram = entry.get("size_vram") or 0
    if not size:
        return blank
    pct = round(100.0 * vram / size, 1)
    return {
        "placement": placement_of(pct),
        "vram_pct": pct,
        "loaded_model": entry.get("name") or entry.get("model"),
    }


async def ps_models(client: httpx.AsyncClient, upstream: str) -> list:
    """Názvy modelů, které Ollama právě drží v paměti (/api/ps). Výjimka = Ollama neodpovídá."""
    resp = await client.get(upstream + "/api/ps", timeout=2.0)
    resp.raise_for_status()
    return [m.get("name") or m.get("model") for m in resp.json().get("models") or []
            if m.get("name") or m.get("model")]
