"""Komerční (a další) upstreamy: hlavičky s klíčem, výpis modelů, ceník."""

import json

import httpx

KIND_LABELS = {
    "openai": "OpenAI-kompatibilní (OpenAI, OpenRouter, Groq, Mistral, DeepSeek, …)",
    "anthropic": "Anthropic (Claude)",
    "google": "Google Gemini",
    "ollama": "Další Ollama (jiný server)",
}

DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "ollama": "http://ollama:11434",
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
    root = proxy_root.rstrip("/") + "/providers/" + slug
    if kind in ("openai",):
        return root + "/v1"
    return root


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
