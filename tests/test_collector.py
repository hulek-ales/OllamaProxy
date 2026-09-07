from ollamaproxy.collector import Collector
from ollamaproxy.providers import cost_usd, parse_pricing


def feed_all(data: bytes, chunk=7):
    c = Collector(log_bodies=True)
    for i in range(0, len(data), chunk):
        c.feed(data[i:i + chunk])
    c.finish()
    return c


def test_ollama_ndjson():
    c = feed_all(b'{"model":"m","message":{"content":"A"},"done":false}\n'
                 b'{"model":"m","message":{"content":"B"},"done":true,"prompt_eval_count":3,'
                 b'"eval_count":2,"eval_duration":1000000000,"total_duration":1500000000}\n')
    assert (c.prompt_tokens, c.completion_tokens) == (3, 2)
    assert c.tokens_per_sec() == 2.0
    assert c.text() == "AB"
    assert c.model == "m"


def test_openai_sse_with_usage():
    c = feed_all(b'data: {"choices":[{"delta":{"content":"x"}}],"model":"gpt"}\n\n'
                 b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n'
                 b'data: [DONE]\n\n')
    assert (c.prompt_tokens, c.completion_tokens) == (11, 4)
    assert c.text() == "x"
    assert c.tokens_per_sec() is None


def test_anthropic_sse():
    c = feed_all(b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude","usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
                 b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
                 b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n')
    assert (c.prompt_tokens, c.completion_tokens) == (5, 9)
    assert c.model == "claude"
    assert c.text() == "hi"


def test_anthropic_json():
    c = feed_all(b'{"type":"message","model":"claude","content":[{"type":"text","text":"ok"}],'
                 b'"usage":{"input_tokens":2,"output_tokens":1}}')
    assert (c.prompt_tokens, c.completion_tokens) == (2, 1)
    assert c.text() == "ok"


def test_gemini_json():
    c = feed_all(b'{"candidates":[{"content":{"parts":[{"text":"g"}]}}],'
                 b'"usageMetadata":{"promptTokenCount":8,"candidatesTokenCount":3},"modelVersion":"gemini-2.0"}')
    assert (c.prompt_tokens, c.completion_tokens) == (8, 3)
    assert c.model == "gemini-2.0"
    assert c.text() == "g"


def test_error_shapes():
    assert feed_all(b'{"error":"model not found"}').error == "model not found"
    assert feed_all(b'{"error":{"message":"bad key","type":"auth"}}').error == "bad key"


def test_garbage_is_ignored():
    c = feed_all(b"<html>oops</html>\nnot json\n")
    assert c.prompt_tokens is None and c.text() is None


def test_pricing_prefix_match():
    pricing = parse_pricing('{"gpt-4o-mini": {"in": 0.15, "out": 0.6}, "gpt-4o": {"in": 2.5, "out": 10}}')
    assert cost_usd(pricing, "gpt-4o-mini-2024-07-18", 1_000_000, 1_000_000) == 0.75
    assert cost_usd(pricing, "gpt-4o-2024-08-06", 1_000_000, 0) == 2.5
    assert cost_usd(pricing, "unknown", 5, 5) is None
