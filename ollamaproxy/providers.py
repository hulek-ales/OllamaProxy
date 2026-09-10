"""Komerční (a další) upstreamy: hlavičky s klíčem, výpis modelů, ceník.

Typ `gpu` je lokální služba (TTS, Whisper…), která sdílí grafiku s hlavní Ollamou.
Dotazy na její modely jdou přes plánovač modelů a proxy jí před přepnutím řekne,
ať uvolní VRAM. Služba musí umět (viz docs/GPU-BACKEND.md):

    GET  /v1/models    {"data": [{"id": "tts-cs"}]}          seznam modelů
    GET  /api/ps       {"models": [{"name": "tts-cs", …}]}    co má v paměti (jako Ollama)
    POST /api/unload   {"model"?: "tts-cs"}                   uvolnit VRAM (bez modelu = vše)
    POST /api/load     {"model": "tts-cs"}                    nepovinné; jinak nahraje první dotaz
"""

import json

import httpx

KIND_LABELS = {
    "openai": "OpenAI-kompatibilní (OpenAI, OpenRouter, Groq, Mistral, DeepSeek, …)",
    "anthropic": "Anthropic (Claude)",
    "google": "Google Gemini",
    "ollama": "Další Ollama (jiný server)",
    "gpu": "Lokální GPU služba (TTS, Whisper…) — sdílí kartu s Ollamou, jde přes plánovač",
}

DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "ollama": "http://ollama:11434",
    "gpu": "http://tts:8000",
}

ANTHROPIC_VERSION = "2023-06-01"


def auth_headers(kind: str, api_key: str, incoming: dict) -> dict:
    """Doplní hlavičky pro upstream. `incoming` už nesmí obsahovat klientovu autentizaci."""
    headers = dict(incoming)
    if kind == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
    elif kind == "google":
        if api_key:
            headers["x-goog-api-key"] = api_key
    else:  # openai, ollama
        if api_key:
            headers["authorization"] = "Bearer " + api_key
    return headers


def client_base_url(proxy_root: str, slug: str, kind: str) -> str:
    """Co má aplikace nastavit jako base_url, aby šla přes proxy."""
    if kind == "gpu":
        # směruje se podle modelu: stejná adresa jako Ollama, proxy pozná model služby
        return proxy_root.rstrip("/") + "/v1"
    root = proxy_root.rstrip("/") + "/providers/" + slug
    if kind in ("openai",):
        return root + "/v1"
    return root


def provider_models(row: dict) -> list:
    """Vzory modelů GPU služby ze sloupce `models`."""
    return [p for p in (row.get("models") or "").split(",") if p]


# ------------------------------------------------ kontrakt GPU služby

async def backend_ps(client: httpx.AsyncClient, base_url: str, api_key: str) -> list:
    """Modely, které GPU služba drží v paměti (GET /api/ps, stejný tvar jako Ollama)."""
    r = await client.get(base_url.rstrip("/") + "/api/ps", headers=auth_headers("gpu", api_key, {}),
                         timeout=5.0)
    r.raise_for_status()
    return [m.get("name") or m.get("model") for m in r.json().get("models") or []
            if m.get("name") or m.get("model")]


async def backend_unload(client: httpx.AsyncClient, base_url: str, api_key: str, model: str = None):
    """Řekne službě, ať uvolní VRAM (POST /api/unload). Vrátí se, až je hotovo."""
    body = {"model": model} if model else {}
    r = await client.post(base_url.rstrip("/") + "/api/unload", json=body,
                          headers=auth_headers("gpu", api_key, {}), timeout=120.0)
    r.raise_for_status()


async def backend_load(client: httpx.AsyncClient, base_url: str, api_key: str, model: str) -> bool:
    """POST /api/load — nepovinné; 404 = služba model nahraje sama při prvním dotazu."""
    r = await client.post(base_url.rstrip("/") + "/api/load", json={"model": model},
                          headers=auth_headers("gpu", api_key, {}), timeout=600.0)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


async def fetch_models(client: httpx.AsyncClient, kind: str, base_url: str, api_key: str) -> list:
    base = base_url.rstrip("/")
    headers = auth_headers(kind, api_key, {})
    if kind == "ollama":
        r = await client.get(base + "/api/tags", headers=headers, timeout=15.0)
        r.raise_for_status()
        return sorted(m.get("name") or m.get("model") for m in r.json().get("models") or [])
    if kind == "google":
        r = await client.get(base + "/v1beta/models", headers=headers, timeout=15.0)
        r.raise_for_status()
        return sorted((m.get("name") or "").replace("models/", "") for m in r.json().get("models") or [])
    r = await client.get(base + "/v1/models", headers=headers, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else data
    return sorted(m.get("id") for m in items or [] if isinstance(m, dict) and m.get("id"))


def parse_pricing(raw) -> dict:
    """Ceník: {"model": {"in": USD za 1M vstupních, "out": USD za 1M výstupních}}."""
    if not raw:
        return {}
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("ceník musí být objekt {model: {in, out}}")
    out = {}
    for model, price in data.items():
        if not isinstance(price, dict):
            raise ValueError("cena u " + str(model) + " musí být objekt {in, out}")
        out[str(model)] = {"in": float(price.get("in", 0) or 0), "out": float(price.get("out", 0) or 0)}
    return out


def cost_usd(pricing: dict, model, prompt_tokens, completion_tokens):
    """Nejdelší shodný prefix názvu modelu (gpt-4o-mini-2024-07-18 → gpt-4o-mini)."""
    if not pricing or not model:
        return None
    best = None
    for key in pricing:
        if model == key or model.startswith(key):
            if best is None or len(key) > len(best):
                best = key
    if best is None:
        return None
    p = pricing[best]
    return round(((prompt_tokens or 0) * p["in"] + (completion_tokens or 0) * p["out"]) / 1e6, 8)


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "…" + key[-4:]
