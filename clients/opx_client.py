"""Klient Ollama proxy pro agenty — jen standardní knihovna, stačí zkopírovat do projektu.

    from opx_client import OpxClient
    opx = OpxClient("http://ollama-proxy:11435", "opx_…")

    # interaktivně (čeká na odpověď, prochází plánovačem modelů)
    r = opx.chat("gemma4:12b", [{"role": "user", "content": "ahoj"}])
    print(r["message"]["content"])

    # odloženě (agent, kterému nevadí odpověď za 15 minut)
    jid = opx.submit("/api/chat", {"model": "gemma4:12b", "messages": [...]})
    job = opx.wait(jid)                 # bloknout, dokud není hotová (poll každých 5 s)
    print(job["result"]["message"]["content"])

    # dávka: jeden batch_id, jedno čekání
    batch = opx.submit_batch([{"path": "/api/chat", "body": {...}}, ...], priority=7)
    for job in opx.wait_batch(batch):
        ...

    # jen se zeptat, jestli je model nahraný (true/false), bez čekání
    opx.model_loaded("gemma4:12b")

    # GPU služba vedle Ollamy (TTS): proxy sama uvolní Ollamu a pak zase službu
    mp3 = opx.speak("tts-cs", "Dobré ráno…", voice="jirka")         # bytes, hned
    jid = opx.submit("/v1/audio/speech", {"model": "tts-cs", "input": "…"})   # odloženě
    opx.wait(jid); opx.download(jid, "/podcast/dnes.mp3")           # výsledek je soubor
"""

import json
import time
import urllib.error
import urllib.request


class OpxError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP " + str(status) + ": " + str(body)[:300])
        self.status = status
        self.body = body


class OpxClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 900.0):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------ HTTP

    def _raw(self, method: str, path: str, body=None, headers=None, timeout=None):
        """Vrátí (bytes, content-type); HTTP chyba → OpxError."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", "replace")
            raise OpxError(exc.code, parsed) from None

    def _call(self, method: str, path: str, body=None, headers=None, timeout=None):
        raw, _ = self._raw(method, path, body, headers, timeout)
        return json.loads(raw) if raw else None

    # ----------------------------------------------------- interaktivně

    def chat(self, model: str, messages: list, wait_s=None, **extra) -> dict:
        """Ollama /api/chat bez streamu. `wait_s` = max. čekání na GPU (None = bez limitu)."""
        body = {"model": model, "messages": messages, "stream": False, **extra}
        headers = {"X-Opx-Wait": str(wait_s)} if wait_s is not None else None
        return self._call("POST", "/api/chat", body, headers)

    def generate(self, model: str, prompt: str, wait_s=None, **extra) -> dict:
        body = {"model": model, "prompt": prompt, "stream": False, **extra}
        headers = {"X-Opx-Wait": str(wait_s)} if wait_s is not None else None
        return self._call("POST", "/api/generate", body, headers)

    def model_loaded(self, model: str, wait_s: float = 0, keep_alive=None) -> bool:
        """Požádá o nahrání modelu; True = je na GPU, pošli dotazy hned."""
        body = {"model": model, "wait_s": wait_s}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        return bool(self._call("POST", "/mgmt/v1/models/load", body)["loaded"])

    def status(self) -> dict:
        return self._call("GET", "/mgmt/v1/models/status")

    def speak(self, model: str, text: str, voice=None, wait_s=None, **extra) -> bytes:
        """Syntéza řeči přes GPU službu (docs/GPU-BACKEND.md). Vrátí audio bytes.
        Proxy před tím uvolní Ollamu z VRAM a chat mezitím čeká — jeden díl = jeden dotaz."""
        body = {"model": model, "input": text, **extra}
        if voice is not None:
            body["voice"] = voice
        headers = {"X-Opx-Wait": str(wait_s)} if wait_s is not None else None
        audio, _ = self._raw("POST", "/v1/audio/speech", body, headers)
        return audio

    # --------------------------------------------------------- úlohy

    def submit(self, path: str, body: dict, provider: str = "ollama", priority: int = 5,
               callback_url=None, not_before=None) -> int:
        payload = {"path": path, "body": body, "provider": provider, "priority": priority,
                   "callback_url": callback_url, "not_before": not_before}
        return self._call("POST", "/mgmt/v1/jobs", payload)["id"]

    def submit_batch(self, jobs: list, priority=None, callback_url=None, not_before=None) -> str:
        """jobs = [{"path": "/api/chat", "body": {...}, "provider"?: "ollama"}, ...] → batch_id"""
        payload = {"jobs": jobs, "priority": priority, "callback_url": callback_url,
                   "not_before": not_before}
        return self._call("POST", "/mgmt/v1/jobs", payload)["batch_id"]

    def job(self, job_id: int) -> dict:
        return self._call("GET", "/mgmt/v1/jobs/" + str(job_id))

    def batch(self, batch_id: str, bodies: bool = True) -> list:
        out = self._call("GET", "/mgmt/v1/jobs?batch=" + batch_id + "&limit=500&bodies="
                         + ("1" if bodies else "0"))
        return out["items"]

    def cancel(self, job_id: int) -> dict:
        return self._call("DELETE", "/mgmt/v1/jobs/" + str(job_id))

    def download(self, job_id: int, dest: str) -> str:
        """Binární výsledek úlohy (audio) do souboru `dest`. Vrátí content-type."""
        raw, content_type = self._raw("GET", "/mgmt/v1/jobs/" + str(job_id) + "/result")
        with open(dest, "wb") as f:
            f.write(raw)
        return content_type

    def wait(self, job_id: int, poll: float = 5.0, timeout: float = None) -> dict:
        """Čeká, dokud úloha není done/error/cancelled. Vrátí ji včetně `result`."""
        deadline = time.time() + timeout if timeout else None
        while True:
            job = self.job(job_id)
            if job["status"] in ("done", "error", "cancelled"):
                return job
            if deadline and time.time() > deadline:
                raise TimeoutError("job " + str(job_id) + " still " + job["status"])
            time.sleep(poll)

    def wait_batch(self, batch_id: str, poll: float = 5.0, timeout: float = None) -> list:
        deadline = time.time() + timeout if timeout else None
        while True:
            items = self.batch(batch_id)
            if items and all(j["status"] in ("done", "error", "cancelled") for j in items):
                return sorted(items, key=lambda j: j["id"])
            if deadline and time.time() > deadline:
                raise TimeoutError("batch " + batch_id + " not finished")
            time.sleep(poll)
