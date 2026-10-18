"""Groq chat completions (OpenAI-compatible HTTP API) with retries and rate-limit handling."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time

import httpx

from . import timing
from .scope import parse_json_object

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMRateLimited(LLMError):
    """Groq rate limit that cannot be waited out within the configured limit."""

    def __init__(self, retry_after: float | None):
        super().__init__(f"rate limited (retry after {retry_after})")
        self.retry_after = retry_after


class GroqClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.groq.com/openai/v1",
                 timeout: float = 60.0, max_retries: int = 3, max_retry_wait: float = 30.0,
                 reasoning_effort: str | None = "low", max_concurrency: int = 2,
                 fallback_models: list[str] | None = None, fallback_wait: float = 2.0):
        self.api_key = api_key
        self.fallback_wait = fallback_wait   # max wait on a rate-limited model while another one is available
        self.fallback_models = list(fallback_models or [])
        self._blocked_until: dict[str, float] = {}   # model -> monotonic time its rate limit resets
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(timeout=timeout)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._no_reasoning_param: set[str] = set()

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        resp = await self._client.get(f"{self.base_url}/models", headers=self._headers())
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", [])]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def chat(self, model: str, messages: list[dict], *, wait: bool = True, **kwargs) -> str:
        """Chat with `model`; if it is rate-limited for longer than we can wait (e.g. the free-tier daily
        token quota), use the configured fallback models, each of which has its own quota."""
        candidates = [model] + [m for m in self.fallback_models if m != model]
        available = [m for m in candidates if self._blocked_until.get(m, 0) <= time.monotonic()]
        last: LLMRateLimited | None = None
        for i, candidate in enumerate(available):
            if timing.remaining() < 3:
                break                                      # request time budget exhausted
            # Waiting out a rate limit only makes sense when no other model is left to try.
            max_wait = self.max_retry_wait if i == len(available) - 1 else self.fallback_wait
            if not wait:
                max_wait = 0.0                             # optional call: never wait out a rate limit
            try:
                text = await self._chat_one(candidate, messages, max_wait=max_wait, **kwargs)
                if candidate != model:
                    log.warning("Answered with fallback model %s (primary %s is rate-limited)", candidate, model)
                return text
            except LLMRateLimited as exc:
                last = exc
                self._blocked_until[candidate] = time.monotonic() + (exc.retry_after or 60)
                log.warning("Model %s rate-limited for %ss; trying the next fallback", candidate, exc.retry_after)
        raise last or LLMRateLimited(None)

    async def _chat_one(self, model: str, messages: list[dict], *, json_mode: bool = False,
                        max_tokens: int = 1024, temperature: float = 0.2, reasoning_effort: str | None = None,
                        max_wait: float | None = None) -> str:
        max_wait = self.max_retry_wait if max_wait is None else max_wait
        payload: dict = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
                         "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = reasoning_effort or self.reasoning_effort
        if effort and model not in self._no_reasoning_param and "gpt-oss" in model:
            payload["reasoning_effort"] = effort
            payload["include_reasoning"] = False
        if "qwen" in model and model not in self._no_reasoning_param:
            payload["reasoning_format"] = "hidden"

        attempt = 0
        async with self._semaphore:
            while True:
                attempt += 1
                started = time.monotonic()
                try:
                    resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload,
                                                   headers=self._headers())
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    timing.record_llm_call(model, time.monotonic() - started, "network-error")
                    if attempt > self.max_retries:
                        raise LLMError(f"network error: {exc}") from exc
                    delay = min(2 ** attempt, 10) + random.random()
                    if delay > timing.remaining() - 5:
                        raise LLMError(f"network error: {exc}") from exc
                    timing.record_wait(delay)
                    await asyncio.sleep(delay)
                    continue
                timing.record_llm_call(model, time.monotonic() - started, str(resp.status_code))

                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage", {})
                    log.info("LLM %s ok in %.1fs (prompt=%s, completion=%s tokens)", model,
                             time.monotonic() - started, usage.get("prompt_tokens"), usage.get("completion_tokens"))
                    content = data["choices"][0]["message"].get("content") or ""
                    return re.sub(r"(?s)<think>.*?</think>", "", content).strip()

                body = resp.text[:500]
                if resp.status_code == 400 and "reasoning" in body and any(
                        k in payload for k in ("reasoning_effort", "reasoning_format")):
                    log.warning("Model %s rejected reasoning parameters; retrying without them", model)
                    self._no_reasoning_param.add(model)
                    for k in ("reasoning_effort", "include_reasoning", "reasoning_format"):
                        payload.pop(k, None)
                    continue
                if resp.status_code == 400 and json_mode and "json" in body.lower():
                    # json_validate_failed etc.: fall back to free text, parsed leniently by callers.
                    log.warning("JSON mode failed for %s: %s; retrying without JSON mode", model, body)
                    payload.pop("response_format", None)
                    json_mode = False
                    if attempt > self.max_retries:
                        raise LLMError(f"HTTP 400: {body}")
                    continue
                if resp.status_code == 429:
                    retry_after = _retry_after(resp)
                    log.warning("Groq rate limit for %s (retry-after=%s): %s", model, retry_after, body)
                    if attempt > self.max_retries or (retry_after or 2 ** attempt) > max_wait:
                        raise LLMRateLimited(retry_after)
                    delay = (retry_after or 2 ** attempt) + random.random()
                    if delay > timing.remaining() - 5:
                        raise LLMRateLimited(retry_after)   # would exceed the request's time budget
                    timing.record_wait(delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 500:
                    if attempt > self.max_retries:
                        raise LLMError(f"HTTP {resp.status_code}: {body}")
                    delay = min(2 ** attempt, 10) + random.random()
                    if delay > timing.remaining() - 5:
                        raise LLMError(f"HTTP {resp.status_code}: {body}")
                    timing.record_wait(delay)
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"HTTP {resp.status_code}: {body}")

    async def chat_json(self, model: str, messages: list[dict], **kwargs) -> dict | None:
        text = await self.chat(model, messages, json_mode=True, **kwargs)
        result = parse_json_object(text)
        if result is None:
            result = salvage_json_fields(text)
            log.warning("LLM returned non-JSON output (%s): %r", "salvaged" if result else "unusable", text[:300])
        return result


def salvage_json_fields(text: str) -> dict | None:
    """Recover string/list fields from malformed or truncated JSON such as
    '{"status": "answered", "answer": "- Опеньки ростуть… [1]' (cut off by the token limit)."""
    import json
    import re

    if not text:
        return None
    out: dict = {}
    for key in ("status", "answer", "intent", "search_query"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)("?)', text, re.S)
        if m:
            try:
                out[key] = json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                out[key] = m.group(1).replace('\\n', "\n").replace('\\"', '"')
    m = re.search(r'"used"\s*:\s*\[([\d,\s]*)\]', text)
    if m:
        out["used"] = [int(x) for x in re.findall(r"\d+", m.group(1))]
    return out or None


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    return None
