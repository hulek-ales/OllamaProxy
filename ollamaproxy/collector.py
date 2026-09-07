"""Sběr metrik ze streamované odpovědi.

Rozumí formátům:
  - Ollama NDJSON (/api/generate, /api/chat) — prompt_eval_count, eval_count, *_duration
  - OpenAI chat/completions a Responses API (SSE i JSON) — usage.prompt_tokens / input_tokens
  - Anthropic Messages (SSE i JSON) — message_start.usage, message_delta.usage
  - Google Gemini generateContent — usageMetadata
Cokoli nerozpoznaného se tiše ignoruje; proxy nikdy nesmí spadnout na parsování.
"""

import json


class Collector:
    def __init__(self, log_bodies: bool = True):
        self.log_bodies = log_bodies
        self.prompt_tokens = None
        self.completion_tokens = None
        self.total_ns = None
        self.eval_ns = None
        self.model = None
        self.error = None
        self.text_parts = []
        self._buf = b""

    # ------------------------------------------------------------ vstup

    def feed(self, chunk: bytes):
        self._buf += chunk
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            self._line(line.strip())

    def finish(self):
        rest = self._buf.strip()
        self._buf = b""
        if rest:
            self._line(rest)

    def _line(self, raw: bytes):
        if not raw:
            return
        if raw.startswith(b"event:"):
            return
        if raw.startswith(b"data:"):
            raw = raw[5:].strip()
            if raw in (b"[DONE]", b""):
                return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        self._absorb(obj)

    # ------------------------------------------------------------ rozbor

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        pt = usage.get("prompt_tokens", usage.get("input_tokens"))
        ct = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(pt, int):
            self.prompt_tokens = pt
        if isinstance(ct, int):
            self.completion_tokens = ct

    def _absorb(self, obj):
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("model"), str):
            self.model = obj["model"]

        # Ollama nativní
        if "prompt_eval_count" in obj:
            self.prompt_tokens = obj["prompt_eval_count"]
        if "eval_count" in obj:
            self.completion_tokens = obj["eval_count"]
        if "total_duration" in obj:
            self.total_ns = obj["total_duration"]
        if "eval_duration" in obj:
            self.eval_ns = obj["eval_duration"]

        # OpenAI + Anthropic usage (v kořeni, ve zprávě, v Responses API)
        self._usage(obj.get("usage"))
        msg = obj.get("message")
        if isinstance(msg, dict):
            self._usage(msg.get("usage"))
            if isinstance(msg.get("model"), str):
                self.model = msg["model"]
        resp = obj.get("response")
        if isinstance(resp, dict):
            self._usage(resp.get("usage"))
            if isinstance(resp.get("model"), str):
                self.model = resp["model"]

        # Gemini
        um = obj.get("usageMetadata")
        if isinstance(um, dict):
            if isinstance(um.get("promptTokenCount"), int):
                self.prompt_tokens = um["promptTokenCount"]
            if isinstance(um.get("candidatesTokenCount"), int):
                self.completion_tokens = um["candidatesTokenCount"]
        if isinstance(obj.get("modelVersion"), str):
            self.model = obj["modelVersion"]

        # chyby: Ollama {"error": "..."}, OpenAI/Anthropic {"error": {"message": ...}}
        err = obj.get("error")
        if isinstance(err, str):
            self.error = err
        elif isinstance(err, dict):
            self.error = err.get("message") or err.get("type") or json.dumps(err)[:500]

        if self.log_bodies:
            self._text(obj, msg, resp)

    def _text(self, obj, msg, resp):
        # Ollama chat / generate
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            self.text_parts.append(msg["content"])
        elif isinstance(resp, str):
            self.text_parts.append(resp)
        # OpenAI chat completions
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                self.text_parts.append(delta["content"])
            elif isinstance(choice.get("text"), str):
                self.text_parts.append(choice["text"])
        # OpenAI Responses API stream
        typ = obj.get("type")
        if typ == "response.output_text.delta" and isinstance(obj.get("delta"), str):
            self.text_parts.append(obj["delta"])
        # Anthropic stream
        if typ == "content_block_delta":
            d = obj.get("delta") or {}
            if isinstance(d, dict) and isinstance(d.get("text"), str):
                self.text_parts.append(d["text"])
        # Anthropic bez streamu
        if typ == "message" and isinstance(obj.get("content"), list):
            for block in obj["content"]:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    self.text_parts.append(block["text"])
        # Gemini
        for cand in obj.get("candidates") or []:
            content = cand.get("content") if isinstance(cand, dict) else None
            for part in (content or {}).get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    self.text_parts.append(part["text"])

    # ----------------------------------------------------------- výsledky

    def tokens_per_sec(self):
        if self.completion_tokens and self.eval_ns:
            return round(self.completion_tokens / (self.eval_ns / 1e9), 2)
        return None

    def text(self):
        return "".join(self.text_parts) if self.text_parts else None
