"""Deterministic answer repairs and latency guards found in the manual winter/summer/badger dialogue."""

import json
import time
from types import SimpleNamespace

import httpx

from forest_bot.dialogue import genitive
from forest_bot.llm import GroqClient, LLMRateLimited
from forest_bot.qa import apply_grounding_fallback, deterministic_repairs

PINES = ("Членські внески рукавички «У соснах»\n- Розмір внеску: 2 соснові шишки з дорослого звіра щотижня.\n"
         "- Термін: внески здають щонеділі.\n- Куди: Синиці в кошик на гілці біля входу.\n"
         "Зайчик може платити морквою з поля. Обмін такий: 1 морквина за 2 шишки.\nУзимку внески вдвічі менші.")
DOCS = [SimpleNamespace(text=PINES, document=SimpleNamespace(title="Членські внески рукавички У соснах"))]


def test_claims_about_other_documents_are_removed_without_a_model_call():
    text = ("Для дорослого борсука внесок 2 соснові шишки щотижня [1]. "
            "(Інші документи не містять інформації про розмір внесків.)")
    out = deterministic_repairs(text, DOCS)
    assert out == "Для дорослого борсука внесок 2 соснові шишки щотижня [1]."
    assert "в архіві" not in deterministic_repairs("Узимку — 1 шишка [1]. В архіві більше нічого немає.", DOCS)
    # a scoped statement about the provided documents is kept
    kept = "У наданих документах літнього зменшення не згадано [1]."
    assert deterministic_repairs(kept, DOCS) == kept


def test_recipient_form_is_restored_from_the_source():
    out = deterministic_repairs("Внески здаються щонеділі у синицю в кошик на гілці біля входу [1].", DOCS)
    assert out == "Внески здаються щонеділі Синиці в кошик на гілці біля входу [1]."
    correct = "Внески здають Синиці в кошик на гілці біля входу [1]."
    assert deterministic_repairs(correct, DOCS) == correct


def test_genitive_for_identities():
    assert genitive("дорослий борсук") == "дорослого борсука"
    assert genitive("доросла видра") == "дорослої видри"
    assert genitive("ведмідь") == "ведмедя" and genitive("синиця") == "синиці" and genitive("кріт") == "крота"


def test_exclusivity_fallback_quotes_the_rule_and_uses_the_known_identity():
    bad = "Ні, морквою може платити лише Зайчик [1]."
    out = apply_grounding_fallback(bad, DOCS, False, {"identity": "дорослий борсук", "season": "літо"})
    assert out == ("У документі цю можливість названо так: «Зайчик може платити морквою з поля». Для вас як "
                   "дорослого борсука її не зазначено, тому підтвердити її не можу.")
    generic = apply_grounding_fallback(bad, DOCS, False, {})
    assert "Для інших звірів її не зазначено" in generic and "для:" not in generic


async def test_correction_calls_never_wait_for_rate_limits():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(429, headers={"retry-after": "18"}, json={"error": {}})

    client = GroqClient("key", max_retries=3, max_retry_wait=30, fallback_models=["b", "c"])
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        await client.chat("a", [{"role": "user", "content": "x"}], wait=False)
    except LLMRateLimited:
        pass
    assert time.monotonic() - started < 1.0            # previously the last model was waited out (~29 s)
    assert calls == ["a", "b", "c"]                     # each model tried once


async def test_correction_overhead_when_all_models_are_limited(archive, store, library):
    """Answer OK with an issue; the correction cannot run -> deterministic fix, no waiting."""
    from forest_bot.conversation import Conversation
    from forest_bot.qa import QAService
    from forest_bot.retrieval import Retriever
    from .conftest import KYIV

    route = {"intent": "question", "locations": ["u_sosnakh"], "needs_location": False,
             "search_query": "Чи можна платити морквою", "keywords": ["морква", "внесок"]}

    def handler(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("You route"):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(route)}}]})
        if "Знайдено проблеми" in body["messages"][-1]["content"]:
            return httpx.Response(429, headers={"retry-after": "18"}, json={"error": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content":
                              "СТАТУС: answered\nВІДПОВІДЬ:\nНі, морквою може платити лише Зайчик [1]."}}]})

    client = GroqClient("key", max_retries=3, max_retry_wait=30, fallback_models=["fb"])
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    conv = Conversation(store, library, QAService(client, Retriever(store), model="main", router_model="router",
                                                 tz=KYIV), KYIV)
    store.save_state(7, {"context": {"identity": "дорослий борсук", "season": "літо"},
                         "scope": {"kind": "mittens", "mittens": ["u_sosnakh"], "regions": []}})
    started = time.monotonic()
    reply = (await conv.on_text(7, "А для мене?"))[-1].text
    assert time.monotonic() - started < 3
    assert "Для вас як дорослого борсука її не зазначено" in reply and "лише" not in reply.split("Джерела")[0]
