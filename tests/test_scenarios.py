"""Customer-provided scenarios, end-to-end through the dialogue with the real archive texts.

The LLM is replaced by a fake that answers from the documents it receives, so these tests verify
what the application controls: which evidence is eligible and offered (with which priority level),
what is excluded, and that sources are rendered from stored records. Real LLM wording is covered by
the optional live test (tests/live/test_live_groq.py).
"""

import re

from .conftest import evidence_titles


def cite_answer(fact_pattern: str):
    """Fake answer: cite every evidence document whose text matches `fact_pattern`."""

    def answer(messages):
        prompt = messages[1]["content"]
        blocks = re.split(r"(?m)^(?=\[\d+\] «)", prompt.split("Документи:", 1)[1])
        cited = [int(re.match(r"\[(\d+)\]", b).group(1)) for b in blocks if b.startswith("[") and
                 re.search(fact_pattern, b)]
        if not cited:
            return {"status": "not_found", "answer": "Такої інформації в документах не знайдено."}
        return {"status": "answered", "answer": " ".join(f"Факт [{n}]." for n in cited), "used": cited}

    return answer


def route(search, keywords, **extra):
    return {"intent": "question", "locations": [], "needs_location": False, "search_query": search,
            "keywords": keywords, **extra}


async def ask(conversation, question, router, fact):
    conversation.llm.route = router
    conversation.llm.answer_fn = cite_answer(fact)
    replies = await conversation.on_text(100, question)
    prompts = conversation.llm.answer_prompts
    return replies[-1].text, prompts[-1] if prompts else ""


async def test_fee_under_oak(conversation, archive):
    html, prompt = await ask(conversation, "Скільки внесок під дубом і до якого числа?",
                             route("Скільки членський внесок і до якого числа?", ["внесок", "числа", "здавати"]),
                             r"4 жолуді з дорослого звіра")
    titles = evidence_titles(prompt)
    assert titles[0] == "Членські внески рукавички Під дубом"
    for fact in ("4 жолуді з дорослого звіра на місяць", "до 10 числа", "Кабанові в кошик біля входу",
                 "Замість 4 жолудів можна віддати 2 лісові горіхи"):
        assert fact in prompt
    assert "«Членські внески рукавички Під дубом» — Лісовий архів" in html
    assert "drive.google.com/file/d/" in html and "📍 рукавичка «Під дубом»" in html


async def test_hunters_near_dam(conversation, archive):
    html, prompt = await ask(conversation, "Біля греблі мисливці! Як кричати?",
                             route("Як кричати, коли бачиш мисливців?", ["мисливці", "кричати", "Сорока", "постріл"]),
                             r"Крикни тричі коротко")
    assert "Як кричати, коли бачиш мисливців" in evidence_titles(prompt)
    assert "рукавичка «Біля бобрової греблі»" in prompt.split("Документи:")[0]   # understood "біля греблі"
    assert "Крикни тричі коротко, як Сорока" in prompt and "Ховайся в гущавину" in prompt
    assert "«Як кричати, коли бачиш мисливців» — Лісовий архів" in html


async def test_mushrooms_near_willow(conversation, archive):
    html, prompt = await ask(conversation, "Де шукати гриби біля верби?",
                             route("Де шукати гриби?", ["гриби", "опеньки", "білі гриби", "підберезники"]),
                             r"[Оо]пеньки|[Гг]риби")
    titles = evidence_titles(prompt)
    assert titles[0] == "Чим поживитись поруч із рукавичкою Під вербою"          # local advice first
    assert "Опеньки ростуть просто на корінні верби" in prompt
    assert "Грибний путівник листяного лісу" in titles and "Грибний путівник біля річки" in titles
    assert "Грибний путівник хвойного лісу" not in titles
    local_block = prompt.split("[1] «Чим поживитись поруч із рукавичкою Під вербою»")[1].split("<document>")[0]
    assert "місцева порада рукавички «Під вербою»" in local_block
    assert "«Чим поживитись поруч із рукавичкою Під вербою» — Лісовий архів" in html


async def test_insulating_forest_edge(conversation, archive):
    html, prompt = await ask(conversation, "Коли утеплювати рукавичку на узліссі?",
                             route("Коли утеплювати рукавичку?", ["утеплювати", "утеплення", "листопад"]),
                             r"утеплюють у листопаді")
    titles = evidence_titles(prompt)
    assert "Календар сезонів" in titles
    assert not any(t.startswith("Як утеплити рукавичку") for t in titles)       # no regional advice
    assert "Рукавички утеплюють у листопаді" in prompt
    assert "не належить до жодної частини лісу" in prompt
    assert "«Календар сезонів» — Лісовий архів" in html


async def test_forest_wide_comparison_with_units_and_sources(conversation, archive):
    html, prompt = await ask(conversation, "Порівняй членські внески в усіх рукавичках",
                             route("Порівняти членські внески", ["внесок", "членські внески"], whole_forest=True,
                                   comparison=True),
                             r"[Вв]несок|внеску")
    titles = evidence_titles(prompt)
    assert sum(t.startswith("Членські внески рукавички") for t in titles) == 8
    assert "порівняння між місцями" in prompt
    system = conversation.llm.calls[-1][1][0]["content"]
    assert "не перераховуй жолуді, горіхи" in system and "для яких місць даних немає" in system


async def test_unknown_topic_is_not_found(conversation, archive):
    html, prompt = await ask(conversation, "Скільки коштує квиток на потяг під дубом?",
                             route("Скільки коштує квиток на потяг?", ["квиток", "потяг"]),
                             r"квиток на потяг")
    assert ("не знайдено" in html or "не знайшлося" in html) and "Джерела" not in html


async def test_multi_turn_follow_ups_narrow_answers_and_location_switch(conversation, archive, store):
    """«Скільки внесок під дубом?» → «і до якого числа?» → «а кому віддавати?» → «а у соснах?»"""
    routes = {
        "Скільки внесок під дубом?": dict(
            follow_up=False, answer_focus="Скільки членський внесок у рукавичці «Під дубом»?",
            search_query="Скільки членський внесок?", keywords=["внесок", "членські внески"]),
        "і до якого числа?": dict(
            follow_up=True, answer_focus="До якого числа здавати членський внесок у рукавичці «Під дубом»?",
            search_query="Членський внесок: до якого числа здавати", keywords=["внесок", "числа", "здавати"]),
        "а кому віддавати?": dict(
            follow_up=True, answer_focus="Кому віддавати членський внесок у рукавичці «Під дубом»?",
            search_query="Членський внесок: кому віддавати", keywords=["внесок", "здають", "приймає"]),
        "а у соснах?": dict(
            follow_up=True, answer_focus="Кому віддавати членський внесок у рукавичці «У соснах»?",
            search_query="Членський внесок: кому віддавати", keywords=["внесок", "здають", "приймає"]),
    }
    seen_focus = []

    def router(messages):
        message = messages[1]["content"].split("<message>")[1].split("</message>")[0]
        return {"intent": "question", "locations": [], "needs_location": False, **routes[message]}

    def answer(messages):
        prompt = messages[1]["content"]
        seen_focus.append(prompt)
        return {"status": "answered", "answer": "Відповідь [1].", "used": [1]}

    conversation.llm.route = router
    conversation.llm.answer_fn = answer
    replies = {}
    for message in routes:
        replies[message] = (await conversation.on_text(200, message))[-1].text

    first, deadline, recipient, pines = seen_focus

    # Turn 1: a new question -> no follow-up mode.
    assert "УТОЧНЮВАЛЬНЕ" not in first
    # Turns 2-3: follow-up mode with the narrow request; the place «Під дубом» comes from the conversation.
    assert "УТОЧНЮВАЛЬНЕ ПИТАННЯ" in deadline and "«До якого числа здавати членський внесок у рукавичці «Під дубом»?»" in deadline
    assert "«Кому віддавати членський внесок у рукавичці «Під дубом»?»" in recipient
    for prompt in (deadline, recipient):
        assert evidence_titles(prompt)[0] == "Членські внески рукавички Під дубом"   # grounded in active documents
        assert "до 10 числа" in prompt and "Кабанові" in prompt
    # Turn 4: the place switches to «У соснах», the topic (who receives the fee) is preserved.
    assert "«Кому віддавати членський внесок у рукавичці «У соснах»?»" in pines
    titles = evidence_titles(pines)
    assert titles[0] == "Членські внески рукавички У соснах"
    assert not any("Під дубом" in t for t in titles)
    assert "Синиці в кошик" in pines
    # The conversation remembers resolved questions, not just the raw short messages.
    history_block = pines.split("Попередня розмова:")[1].split("Документи:")[0]
    assert "(тобто: Кому віддавати членський внесок у рукавичці «Під дубом»?)" in history_block
    assert store.get_state(200)["scope"]["mittens"] == ["u_sosnakh"]
    # Every answer keeps its source attribution.
    assert "«Членські внески рукавички Під дубом» — Лісовий архів" in replies["і до якого числа?"]
    assert "«Членські внески рукавички У соснах» — Лісовий архів" in replies["а у соснах?"]


async def test_follow_up_without_router_uses_previous_question_for_retrieval(conversation, archive):
    conversation.llm.route = None          # router unavailable / empty -> deterministic fallback
    await conversation.on_text(201, "Скільки внесок під дубом?")
    await conversation.on_text(201, "і до якого числа?")
    prompt = conversation.llm.answer_prompts[-1]
    assert "УТОЧНЮВАЛЬНЕ ПИТАННЯ" in prompt
    assert evidence_titles(prompt)[0] == "Членські внески рукавички Під дубом"


import pytest


@pytest.mark.parametrize("question,focus,search,expected_focus,fact", [
    ("До якого числа?", "До якого числа здавати членський внесок у рукавичці «Під дубом»?",
     "Членський внесок: до якого числа здавати", "До якого числа здавати членський внесок у рукавичці «У соснах»?",
     "щонеділі"),
    ("Кому віддавати?", "Кому віддавати членський внесок у рукавичці «Під дубом»?",
     "Членський внесок: кому віддавати, куди здавати", "Кому віддавати членський внесок у рукавичці «У соснах»?",
     "Синиці в кошик"),
    ("Скільки платити?", "Скільки платити членський внесок у рукавичці «Під дубом»?",
     "Членський внесок: скільки платити, розмір", "Скільки платити членський внесок у рукавичці «У соснах»?",
     "2 соснові шишки"),
])
async def test_place_switch_keeps_latest_requested_detail(conversation, archive, question, focus, search,
                                                          expected_focus, fact):
    """«…?» → «А у соснах?» must ask the SAME latest detail for «У соснах» (retrieval and answer)."""
    routes = {
        "Скільки внесок під дубом?": dict(follow_up=False, answer_focus="Скільки членський внесок у рукавичці «Під дубом»?",
                                          search_query="Членський внесок", keywords=["внесок"]),
        question: dict(follow_up=True, answer_focus=focus, search_query=search, keywords=["внесок"]),
        # A misleading router resolution (as observed live): the deterministic rule must override it.
        "А у соснах?": dict(follow_up=True, answer_focus="Коли здавати членський внесок у рукавичці «У соснах»?",
                            search_query="Членський внесок строк", keywords=["щонеділі"]),
    }

    def router(messages):
        message = messages[1]["content"].split("<message>")[1].split("</message>")[0]
        return {"intent": "question", "locations": [], "needs_location": False, **routes[message]}

    queries = []
    retrieve = conversation.qa.retriever.retrieve

    def spy(query, scope, revision=None):
        queries.append((query, scope.to_state()))
        return retrieve(query, scope, revision)

    conversation.qa.retriever.retrieve = spy
    conversation.llm.route = router
    for message in ("Скільки внесок під дубом?", question, "А у соснах?"):
        await conversation.on_text(300, message)

    prompt = conversation.llm.answer_prompts[-1]
    assert f"Поточний запит з урахуванням контексту: «{expected_focus}»" in prompt
    last_query, last_scope = queries[-1]
    assert last_query == queries[-2][0]                    # same retrieval query as the latest request …
    assert last_scope["mittens"] == ["u_sosnakh"]          # … for the new place
    assert evidence_titles(prompt)[0] == "Членські внески рукавички У соснах"
    assert fact in prompt                                   # the requested detail is in the evidence
    assert "НЕ підставляй замість неї іншу доступну інформацію" in prompt     # follow-up rules sent with it
    first_prompt = conversation.llm.answer_prompts[-3]
    assert "Правила для УТОЧНЮВАЛЬНОГО питання" not in first_prompt          # but not with the new question
