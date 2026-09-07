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
    if pct >= 99:
        placement = "gpu"
    elif pct < 1:
        placement = "cpu"
    else:
        placement = "split"
    return {
        "placement": placement,
        "vram_pct": pct,
        "loaded_model": entry.get("name") or entry.get("model"),
    }
