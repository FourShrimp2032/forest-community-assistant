"""Live Groq smoke test of the customer scenarios. Run:  pytest -m live tests/live/test_live_groq.py -s

Requires GROQ_API_KEY. Uses the archive texts from tests/fixtures (no Drive key needed). Consumes
roughly 4 × ~5k tokens of the free-tier quota.
"""

import asyncio
import os
import re

import pytest

from forest_bot.config import Settings
from forest_bot.conversation import Conversation
from forest_bot.drive import DriveSync
from forest_bot.embeddings import Embedder
from forest_bot.library import Library
from forest_bot.llm import GroqClient
from forest_bot.qa import QAService
from forest_bot.retrieval import Retriever
from forest_bot.storage import Store

from ..conftest import KYIV, FakeDriveClient, load_archive_into

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set")]

SCENARIOS = [
    ("Скільки внесок під дубом і до якого числа?",
     [r"4\s*жолуд", r"10", r"2\s*лісов\w*\s*горіх", r"Кабан"], "Членські внески рукавички Під дубом"),
    ("Біля греблі мисливці! Як кричати?",
     [r"тричі|три\s+(корот|раз)", r"Сорок", r"гущавин"], "Як кричати, коли бачиш мисливців"),
    ("Де шукати гриби біля верби?",
     [r"опеньк", r"кор[іе]н"], "Чим поживитись поруч із рукавичкою Під вербою"),
    ("Коли утеплювати рукавичку на узліссі?",
     [r"листопад"], "Календар сезонів"),
]


@pytest.fixture
async def conversation(tmp_path):
    settings = Settings.from_env()
    store = Store(":memory:")
    embedder = Embedder(settings.embedding_model, settings.embedding_cache_dir, settings.embeddings_enabled)
    library = Library(store, tmp_path, embedder=embedder)
    drive = FakeDriveClient()
    load_archive_into(drive)
    await DriveSync(drive, store, library, "f").run()
    llm = GroqClient(settings.groq_api_key, reasoning_effort=settings.llm_reasoning_effort,
                     fallback_models=settings.llm_fallback_models)
    qa = QAService(llm, Retriever(store, embedder), model=settings.llm_model, router_model=settings.llm_router_model, tz=KYIV,
                   answer_reasoning_effort=settings.llm_answer_reasoning_effort)
    yield Conversation(store, library, qa, KYIV)
    await llm.close()


@pytest.mark.parametrize("question,facts,source", SCENARIOS)
async def test_customer_scenario(conversation, question, facts, source):
    await asyncio.sleep(15)   # stay under the free-tier 8K tokens/minute
    replies = await conversation.on_text(1, question)
    text = replies[-1].text
    print(f"\n=== {question}\n{text}")
    answer, _, sources = text.partition("<b>Джерела:</b>")
    for fact in facts:
        assert re.search(fact, answer, re.I), f"missing {fact!r}"
    assert source in sources
    if "греблі" in question:
        assert not re.search(r"не\s+кричи|утрима\w*\s+від\s+крику|пошепки", answer, re.I), "invented conflict"
    if "верби" in question:
        assert "Грибний путівник листяного лісу" in sources and "Грибний путівник біля річки" in sources
    if "узліссі" in question:
        assert "Як утеплити рукавичку" not in sources


async def test_follow_up_dialogue_answers_only_what_is_asked(conversation):
    """Multi-turn: narrow follow-ups, then a place switch that keeps the topic."""
    turns = {}
    for message in ("Скільки внесок під дубом?", "і до якого числа?", "а кому віддавати?", "а у соснах?"):
        await asyncio.sleep(15)
        replies = await conversation.on_text(7, message)
        turns[message] = replies[-1].text
        print(f"\n=== {message}\n{turns[message]}")

    deadline = turns["і до якого числа?"].partition("<b>Джерела:</b>")
    assert re.search(r"\b10\b", deadline[0])
    assert not re.search(r"жолуд|горіх|Кабан|поросят|пташенят", deadline[0], re.I), "repeated the whole answer"
    assert "Членські внески рукавички Під дубом" in deadline[2]

    recipient = turns["а кому віддавати?"].partition("<b>Джерела:</b>")
    assert re.search(r"Кабан", recipient[0])
    assert not re.search(r"10.{0,3}числ|горіх|поросят", recipient[0], re.I)

    pines = turns["а у соснах?"].partition("<b>Джерела:</b>")
    assert re.search(r"Синиц", pines[0])
    assert "Членські внески рукавички У соснах" in pines[2]
    assert "Під дубом" not in pines[2]


@pytest.mark.parametrize("question,expected,forbidden", [
    ("До якого числа?", r"щонеділ|недil|неділ", r"Синиц|шишк"),
    ("Кому віддавати?", r"Синиц", r"щонеділ|шишк"),
    ("Скільки платити?", r"2\s*соснов\w*\s*шишк|2\s*шишк", r"Синиц|щонеділ"),
])
async def test_place_switch_preserves_latest_intent(conversation, question, expected, forbidden):
    for message in ("Скільки внесок під дубом?", question, "А у соснах?"):
        await asyncio.sleep(12)
        replies = await conversation.on_text(9, message)
    text = replies[-1].text
    print(f"\n=== {question} → А у соснах?\n{text}")
    answer, _, sources = text.partition("<b>Джерела:</b>")
    assert re.search(expected, answer, re.I), f"latest intent lost: expected {expected!r}"
    assert not re.search(forbidden, answer, re.I), "substituted or repeated another detail"
    assert "Членські внески рукавички У соснах" in sources


async def test_comparison_is_grounded_and_draws_no_unsupported_conclusion(conversation):
    from forest_bot.qa import unsupported_affordability_claim
    await asyncio.sleep(15)
    replies = await conversation.on_text(11, "Порівняй внески під дубом і у соснах: скільки та як часто треба "
                                             "платити? Де дешевше?")
    text = replies[-1].text
    print(f"\n=== comparison\n{text}")
    answer, _, sources = text.partition("<b>Джерела:</b>")
    assert unsupported_affordability_claim(answer) is None, "unsupported 'cheaper' conclusion"
    assert re.search(r"неможлив|не можна", answer)
    assert not re.search(r"пільг\w*\s+(не\s+передбачен|немає|відсутн)", answer, re.I), "exemption called absent"
    assert not re.search(r"(зимов\w*|зменшенн\w*)[^.\n]{0,40}\b(немає|не\s+існує|не\s+передбачен\w*|відсутн\w*)",
                         answer, re.I), "absence stated as fact"
    assert not re.search(r"належ|власн", answer, re.I), "invented ownership"
    if re.search(r"поросят|пташенят", answer, re.I):
        for m in re.finditer(r"[^.\n]*(?:поросят|пташенят)[^.\n]*", answer, re.I):
            assert "до року" in m.group(0), f"eligibility condition dropped: {m.group(0)!r}"
    if re.search(r"морк", answer, re.I):
        assert re.search(r"Зайч", answer), "carrot substitute presented as universal"
    assert not re.search(r"≈|приблизно\s+\d|\b8\s+шиш", answer), "unrequested unit/period conversion"
    assert "Членські внески рукавички Під дубом" in sources and "Членські внески рукавички У соснах" in sources
    assert "Як не заблукати" not in sources


async def test_named_eligible_subject_does_not_become_a_prohibition(conversation):
    await asyncio.sleep(15)
    replies = await conversation.on_text(12, "Я дорослий борсук у соснах, можу замість шишок платити морквою?")
    text = replies[-1].text
    print(f"\n=== badger\n{text}")
    answer = text.partition("<b>Джерела:</b>")[0]
    assert not re.match(r"\s*Ні\b", answer), "answered «Ні» without an explicit prohibition"
    assert not re.search(r"\b(лише|тільки|виключно)\b[^.\n]{0,40}за[йя]ч|заборон", answer, re.I), "invented exclusivity"
    assert re.search(r"Зайч", answer, re.I) and re.search(r"не\s+(зазначен|згадан|вказан)", answer, re.I)
