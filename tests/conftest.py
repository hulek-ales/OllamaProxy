import asyncio
import json
import os
import tempfile

import httpx
import pytest

# konfigurace se čte při importu — nastavit dřív, než se app naimportuje
_tmp = tempfile.mkdtemp()
os.environ["OLLAMA_LOG_DB"] = os.path.join(_tmp, "test.db")
os.environ["OLLAMA_UPSTREAM"] = "http://ollama.test"
os.environ["ADMIN_PASSWORD"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from ollamaproxy.main import app  # noqa: E402


def ndjson(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


def sse(*objs):
    return "".join("data: " + json.dumps(o) + "\n\n" for o in objs).encode() + b"data: [DONE]\n\n"


class Upstream:
    """Falešný upstream — zaznamenává requesty a vrací připravené odpovědi."""

    def __init__(self):
        self.calls = []
        self.callbacks = []
        self.slow_s = 1.5   # model "slow" odpovídá tak dlouho (test přerušení úloh)
        self.loaded = {"gemma4:12b"}   # co Ollama hlásí v /api/ps; keep_alive 0 model vyhodí
        self.tts_loaded = set()        # totéž u falešné GPU služby tts.test
        self.tts_slow_s = 0.0          # syntéza trvá tak dlouho (test řazení za TTS)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "callback.test":
            self.callbacks.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if host == "tts.test":
            return await self.tts(path, request)
        if host == "chatterbox.test":
            return await self.chatterbox(path, request)
        if host == "ollama.test" and path == "/api/chat":
            body = json.loads(request.content)
            if body.get("model") == "slow":
                await asyncio.sleep(self.slow_s)
                return httpx.Response(200, json={
                    "model": "slow", "message": {"role": "assistant", "content": "pomalu"}, "done": True,
                    "prompt_eval_count": 3, "eval_count": 1})
            if not body.get("stream", True):
                return httpx.Response(200, json={
                    "model": body.get("model"), "message": {"role": "assistant", "content": "Ahoj!"},
                    "done": True, "prompt_eval_count": 12, "eval_count": 2,
                    "total_duration": 2_000_000_000, "eval_duration": 1_000_000_000})
        if host == "ollama.test":
            if path == "/api/ps":
                return httpx.Response(200, json={"models": [
                    {"name": m, "size": 1000, "size_vram": 1000} for m in sorted(self.loaded)]})
            if path in ("/api/generate", "/api/embed") and json.loads(request.content).get("keep_alive") == 0:
                self.loaded.discard(json.loads(request.content).get("model"))
                return httpx.Response(200, json={"done": True})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}, {"name": "llama3:8b"},
                                                            {"name": "nomic-embed-text:latest"}]})
            if path == "/v1/models":
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "gemma4:12b"}, {"id": "llama3:8b"}]})
            if path == "/api/generate":
                body = json.loads(request.content)
                return httpx.Response(200, json={"model": body.get("model"), "response": "", "done": True})
            if path == "/api/chat":
                return httpx.Response(200, content=ndjson(
                    {"model": "gemma4:12b", "message": {"role": "assistant", "content": "Ahoj"}, "done": False},
                    {"model": "gemma4:12b", "message": {"role": "assistant", "content": "!"}, "done": True,
                     "prompt_eval_count": 12, "eval_count": 2, "total_duration": 2_000_000_000,
                     "eval_duration": 1_000_000_000}),
                    headers={"content-type": "application/x-ndjson"})
            return httpx.Response(200, text="Ollama is running")
        if host == "api.openai.test":
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
            if path == "/v1/chat/completions":
                body = json.loads(request.content)
                if body.get("stream"):
                    return httpx.Response(200, content=sse(
                        {"model": "gpt-4o-mini-2024-07-18", "choices": [{"delta": {"content": "Hi"}}]},
                        {"model": "gpt-4o-mini-2024-07-18", "choices": [],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
                        headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={
                    "model": "gpt-4o-mini-2024-07-18",
                    "choices": [{"message": {"role": "assistant", "content": "Hi there"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if host == "api.anthropic.test":
            if path == "/v1/messages":
                return httpx.Response(200, content=(
                    b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-x",'
                    b'"usage":{"input_tokens":7,"output_tokens":1}}}\n\n'
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Zdrav"}}\n\n'
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"im"}}\n\n'
                    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'),
                    headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": "unknown fake path " + path})

    async def tts(self, path: str, request: httpx.Request) -> httpx.Response:
        """Falešná GPU služba podle docs/GPU-BACKEND.md."""
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "tts-cs"}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": m} for m in sorted(self.tts_loaded)]})
        if path == "/api/unload":
            model = json.loads(request.content or b"{}").get("model")
            if model:
                self.tts_loaded.discard(model)
            else:
                self.tts_loaded.clear()
            return httpx.Response(200, json={"ok": True})
        if path == "/api/load":
            self.tts_loaded.add(json.loads(request.content)["model"])
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/audio/speech":
            body = json.loads(request.content)
            self.tts_loaded.add(body["model"])
            if self.tts_slow_s:
                await asyncio.sleep(self.tts_slow_s)
            audio = b"ID3" + b"\x00" * max(1, len(body.get("input", "")) * 10)
            return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"error": "unknown tts path " + path})

    async def chatterbox(self, path: str, request: httpx.Request) -> httpx.Response:
        """Falešný Chatterbox-TTS-Server: jeden model, žádné /api/ps ani /v1/models,
        stav v /api/model-info, /api/unload bez těla."""
        if path == "/api/model-info":
            return httpx.Response(200, json={"loaded": bool(self.tts_loaded), "type": "multilingual",
                                             "device": "cuda"})
        if path == "/api/unload":
            self.tts_loaded.clear()
            return httpx.Response(200, json={"status": "unloaded"})
        if path == "/v1/audio/voices":
            return httpx.Response(200, json={"voices": [{"id": "jirka.wav"}]})
        if path == "/v1/audio/speech":
            body = json.loads(request.content)
            self.tts_loaded.add("chatterbox")
            if self.tts_slow_s:
                await asyncio.sleep(self.tts_slow_s)
            return httpx.Response(200, content=b"RIFF" + b"\x00" * 64,
                                  headers={"content-type": "audio/wav"})
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture(scope="session")
def upstream():
    return Upstream()


@pytest.fixture(scope="session")
def client(upstream):
    with TestClient(app) as c:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
        yield c


@pytest.fixture(scope="session")
def admin(client):
    """Přihlášený TestClient (session cookie)."""
    r = client.post("/ui/login", data={"username": "admin", "password": "admin123"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "opx_session" in r.cookies
    return client
