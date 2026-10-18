"""Graceful behaviour when Groq is rate-limited or failing (real GroqClient, mocked HTTP)."""

import json
import time

import httpx
import pytest

from forest_bot import messages as msg
from forest_bot.conversation import Conversation
from forest_bot.llm import GroqClient
from forest_bot.qa import QAService
from forest_bot.retrieval import Retriever

from .conftest import KYIV

QUESTION = "Скільки внесок під дубом і до якого числа?"
ROUTE = {"intent": "question", "locations": ["pid_dubom"], "needs_location": False,
         "search_query": "Членський внесок", "keywords": ["внесок", "числа"]}
GOOD = "СТАТУС: answered\nВІДПОВІДЬ:\n4 жолуді з дорослого звіра на місяць, до 10 числа [1]."
WITH_ISSUE = "СТАТУС: answered\nВІДПОВІДЬ:\n4 жолуді на місяць [1]. Пільг під дубом немає [1]."


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})


def limited(seconds):
    return httpx.Response(429, headers={"retry-after": str(seconds)}, json={"error": {"message": "rate limit"}})


def make(store, library, handler, budget=45.0, max_retries=2):
    calls = []

    def recording(request):
        body = json.loads(request.content)
        is_router = body["messages"][0]["content"].startswith("You route")
        is_correction = "Знайдено проблеми" in body["messages"][-1]["content"]
        kind = "router" if is_router else "correction" if is_correction else "answer"
        calls.append((body["model"], kind))
        return handler(body["model"], kind)

    client = GroqClient("key", max_retries=max_retries, max_retry_wait=30, fallback_models=["fallback"],
                        fallback_wait=2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    qa = QAService(client, Retriever(store), model="main", router_model="router", tz=KYIV)
    return Conversation(store, library, qa, KYIV, request_budget_seconds=budget), calls


async def test_every_model_rate_limited_gives_short_unavailability_message(archive, store, library):
    conv, calls = make(store, library, lambda model, kind: limited(900))
    started = time.monotonic()
    reply = (await conv.on_text(1, QUESTION))[-1].text
    assert reply == msg.RATE_LIMITED                               # short Ukrainian message …
    assert "не знайшлося" not in reply and "не знайдено" not in reply    # … never "not found in documents"
    assert time.monotonic() - started < 2                          # long limits are not waited out
    assert len(calls) <= 2                                         # each model tried once, no retry storm


async def test_answer_models_limited_after_successful_routing(archive, store, library):
    def handler(model, kind):
        return ok(json.dumps(ROUTE)) if kind == "router" else limited(900)

    conv, calls = make(store, library, handler)
    reply = (await conv.on_text(1, QUESTION))[-1].text
    assert reply == msg.RATE_LIMITED
    assert [k for _, k in calls].count("answer") <= 2


async def test_short_repeated_limits_are_bounded_by_the_request_budget(archive, store, library):
    """Retry-after 1 s forever on every model: must stop within the request budget."""
    conv, calls = make(store, library, lambda model, kind: limited(1), budget=8, max_retries=10)
    started = time.monotonic()
    reply = (await conv.on_text(1, QUESTION))[-1].text
    elapsed = time.monotonic() - started
    assert reply == msg.RATE_LIMITED
    assert elapsed < 9, f"took {elapsed:.1f}s"
    assert len(calls) < 15


async def test_rate_limited_correction_keeps_the_first_answer_with_deterministic_fix(archive, store, library):
    def handler(model, kind):
        if kind == "router":
            return ok(json.dumps(ROUTE))
        if kind == "answer":
            return ok(WITH_ISSUE)
        return limited(900)                                        # the correction call cannot run

    conv, calls = make(store, library, handler)
    reply = (await conv.on_text(1, QUESTION))[-1].text
    assert reply != msg.RATE_LIMITED and "4 жолуді на місяць" in reply
    assert "Пільг під дубом немає" not in reply                     # removed by the deterministic fallback
    assert "Членські внески рукавички Під дубом" in reply


@pytest.mark.parametrize("status", [500, 503])
async def test_server_errors_are_reported_as_technical_problem_not_missing_information(archive, store, library,
                                                                                       status):
    def handler(model, kind):
        return ok(json.dumps(ROUTE)) if kind == "router" else httpx.Response(status, text="upstream error")

    conv, calls = make(store, library, handler, budget=10, max_retries=1)
    reply = (await conv.on_text(1, QUESTION))[-1].text
    assert reply == msg.THINKING_ERROR
    assert "не знайшлося" not in reply


async def test_router_failure_other_than_rate_limit_falls_back_to_deterministic_routing(archive, store, library):
    def handler(model, kind):
        if kind == "router":
            return httpx.Response(400, text="bad request")
        return ok(GOOD)

    conv, calls = make(store, library, handler)
    reply = (await conv.on_text(1, QUESTION))[-1].text
    assert "4 жолуді з дорослого звіра на місяць" in reply and "Джерела" in reply
