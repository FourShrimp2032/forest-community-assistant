"""Groq client behaviour with a mocked HTTP transport."""

import httpx

from forest_bot.llm import GroqClient, LLMRateLimited


def make_client(handler, **kw):
    client = GroqClient("key", max_retries=1, max_retry_wait=5, **kw)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def ok(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})


async def test_daily_quota_falls_back_to_next_model_and_remembers_block():
    calls = []

    def handler(request):
        model = __import__("json").loads(request.content)["model"]
        calls.append(model)
        if model == "big":
            return httpx.Response(429, headers={"retry-after": "1200"}, json={"error": {"message": "TPD"}})
        return ok("<think>hidden</think>Відповідь")

    client = make_client(handler, fallback_models=["small"])
    assert await client.chat("big", [{"role": "user", "content": "x"}]) == "Відповідь"
    assert await client.chat("big", [{"role": "user", "content": "x"}]) == "Відповідь"
    assert calls == ["big", "small", "small"]          # blocked model is not retried until reset


async def test_all_models_limited_raises_rate_limited():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "900"}, json={"error": {}})

    client = make_client(handler, fallback_models=["small"])
    try:
        await client.chat("big", [{"role": "user", "content": "x"}])
    except LLMRateLimited as exc:
        assert exc.retry_after == 900
    else:
        raise AssertionError("expected LLMRateLimited")


async def test_short_rate_limit_is_waited_out():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {}})
        return ok("так")

    client = make_client(handler)
    assert await client.chat("big", [{"role": "user", "content": "x"}]) == "так"


async def test_per_minute_limit_switches_to_available_model_instead_of_waiting():
    import time
    calls = []

    def handler(request):
        model = __import__("json").loads(request.content)["model"]
        calls.append(model)
        if model == "big":
            return httpx.Response(429, headers={"retry-after": "17"}, json={"error": {"message": "TPM"}})
        return ok("так")

    client = GroqClient("key", max_retries=3, max_retry_wait=30, fallback_models=["small"], fallback_wait=2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    assert await client.chat("big", [{"role": "user", "content": "x"}]) == "так"
    assert time.monotonic() - started < 1.0            # did not sleep 17 s
    assert calls == ["big", "small"]


async def test_last_available_model_still_waits_out_a_short_limit():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={}) if state["n"] == 1 else ok("так")

    client = GroqClient("key", max_retries=3, max_retry_wait=30, fallback_models=[], fallback_wait=2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await client.chat("only", [{"role": "user", "content": "x"}]) == "так"


async def test_timing_records_llm_calls_and_waits():
    from forest_bot import timing

    def handler(request):
        return ok("так")

    client = GroqClient("key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with timing.request() as t:
        await client.chat("m", [{"role": "user", "content": "секретний текст"}])
    summary = t.summary()
    assert "m:" in summary and "retries=0" in summary
    assert "секретний" not in summary          # no message content in timing logs
