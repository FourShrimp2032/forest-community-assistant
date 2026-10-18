"""Comparisons: citation relevance, unsupported affordability conclusions, exceptions and eligibility."""

from forest_bot.qa import (ANSWER_SYSTEM, COMPARISON_CORRECTION, render_answer, unsupported_affordability_claim,
                           verify_citations)
from forest_bot.retrieval import EvidenceDoc, EvidencePassage, QueryScope, RetrievalResult

from .conftest import KYIV, evidence_titles

FEES_DUB = "Членські внески рукавички Під дубом"
FEES_PINES = "Членські внески рукавички У соснах"
LOST = "Як не заблукати серед однакових сосен"
QUESTION = "Порівняй внески під дубом і у соснах: скільки та як часто треба платити? Де дешевше?"


def evidence(store, *titles):
    docs = {d.title: d for d in store.active_documents()}
    out = []
    for t in titles:
        d = docs[t]
        out.append(EvidenceDoc(d, [EvidencePassage(c, 1.0) for c in store.chunks_for(d.id)], {}, 1.0))
    return RetrievalResult(QueryScope("mittens", ["pid_dubom", "u_sosnakh"], comparison=True), out, {})


# ------------------------------------------------------------------ citation relevance

async def test_citation_of_unrelated_document_is_repointed_or_removed(archive, store):
    result = evidence(store, FEES_PINES, FEES_DUB, LOST)
    text = ("Внесок у соснах — 2 соснові шишки з дорослого звіра щотижня [1].\n"
            "Під дубом — 4 жолуді з дорослого звіра на місяць, до 10 числа [2].\n"
            "Обсяг: 2 соснові шишки щотижня проти 4 жолудів на місяць; одиниці різні [3].")
    fixed = verify_citations(text, result.docs)
    lines = fixed.split("\n")
    assert lines[0].endswith("[1].") and lines[1].endswith("[2].")      # correct citations untouched
    assert "[3]" not in lines[2]                                         # navigation tip no longer cited
    out = render_answer({"status": "answered", "answer": text}, result, KYIV)
    assert LOST not in out.html
    assert FEES_PINES in out.html and FEES_DUB in out.html


async def test_answer_without_citations_lists_only_supporting_documents(archive, store):
    result = evidence(store, FEES_PINES, FEES_DUB, LOST)
    out = render_answer({"status": "answered",
                         "answer": "У соснах платять 2 соснові шишки щотижня, а під дубом 4 жолуді на місяць."},
                        result, KYIV)
    assert LOST not in out.html and FEES_PINES in out.html


# ------------------------------------------------------------------ unsupported conclusions

def test_affordability_claim_detector():
    assert unsupported_affordability_claim("Тому в зимовий час «У соснах» вважається менш обтяжливим [1].")
    assert unsupported_affordability_claim("Під дубом дешевше.")
    assert not unsupported_affordability_claim(
        "Визначити, де дешевше, за цими документами неможливо: внески мають різні одиниці.")
    assert not unsupported_affordability_claim("Внесок — 4 жолуді на місяць.")


async def test_unsupported_comparative_conclusion_triggers_one_correction(conversation, archive):
    bad = ("У соснах — 2 соснові шишки щотижня [1]. Під дубом — 4 жолуді на місяць [2]. "
           "Тому взимку «У соснах» менш обтяжливо [1].")
    good = ("У соснах — 2 соснові шишки щотижня [1]. Під дубом — 4 жолуді на місяць [2]. Визначити, де "
            "дешевше, за цими документами неможливо: внески мають різні одиниці та періодичність [1][2].")
    calls = []

    def answer(messages):
        calls.append(messages)
        corrected = "робить висновок про те, де дешевше" in messages[-1]["content"]
        return {"status": "answered", "answer": good if corrected else bad}

    conversation.llm.route = {"intent": "question", "locations": ["pid_dubom", "u_sosnakh"], "comparison": True,
                              "search_query": "Членські внески: скільки й як часто платити",
                              "keywords": ["внесок", "щотижня", "на місяць"]}
    conversation.llm.answer_fn = answer
    replies = await conversation.on_text(400, QUESTION)
    html = replies[-1].text
    assert len(calls) == 2                                  # exactly one extra correction call
    assert "менш обтяжливо" not in html and "неможливо" in html
    assert "Тому взимку «У соснах» менш обтяжливо" in calls[1][-1]["content"]


async def test_no_correction_call_when_conclusion_is_supported(conversation, archive):
    calls = []

    def answer(messages):
        calls.append(messages)
        return {"status": "answered",
                "answer": "Визначити, де дешевше, неможливо: різні одиниці [1][2]."}

    conversation.llm.route = {"intent": "question", "comparison": True, "search_query": "внески",
                              "keywords": ["внесок"]}
    conversation.llm.answer_fn = answer
    await conversation.on_text(401, QUESTION)
    assert len(calls) == 1


# ------------------------------------------------------------------ exceptions and eligibility

async def test_comparison_evidence_contains_exemptions_and_eligibility_conditions(conversation, archive):
    conversation.llm.route = {"intent": "question", "comparison": True,
                              "search_query": "Членські внески: скільки й як часто платити",
                              "keywords": ["внесок", "щотижня", "на місяць"]}
    await conversation.on_text(402, QUESTION)
    prompt = conversation.llm.answer_prompts[-1]
    assert {FEES_DUB, FEES_PINES} <= set(evidence_titles(prompt))
    assert "Пташенята й поросята до року не платять" in prompt          # exemption under the oak
    assert "Зайчик може платити морквою з поля" in prompt                # eligibility: only Зайчик
    assert "Узимку внески вдвічі менші" in prompt                       # seasonal reduction in the pines


def test_answer_rules_cover_exemptions_eligibility_and_unit_conversions():
    assert "лише Зайчик" not in ANSWER_SYSTEM          # an example must not suggest exclusivity
    for rule in ("«У документі не згадано» ≠ «немає»", "Звільнення — теж пільга", "«Зайчик може платити морквою»",
                 "НЕ означає, що іншим це заборонено",
                 "Не перераховуй періодичність", "спільну одиницю або явний еквівалент",
                 "Посилання [n] став лише на документ, у якому є саме це твердження"):
        assert rule in ANSWER_SYSTEM
    assert "неможливо" in COMPARISON_CORRECTION


async def test_conclusion_is_removed_if_correction_still_claims_cheaper(conversation, archive):
    stubborn = ("У соснах — 2 соснові шишки щотижня [1]. Під дубом — 4 жолуді на місяць [2].\n"
                "Тому взимку «У соснах» менш обтяжливо [1].")
    conversation.llm.route = {"intent": "question", "comparison": True, "search_query": "внески",
                              "keywords": ["внесок"]}
    conversation.llm.answer_fn = lambda messages: {"status": "answered", "answer": stubborn}
    html = (await conversation.on_text(403, QUESTION))[-1].text
    assert "менш обтяжливо" not in html
    assert "Визначити, де дешевше, за цими документами неможливо" in html
    assert "2 соснові шишки щотижня" in html                     # the factual part is kept


def test_guard_does_not_duplicate_an_existing_correct_conclusion():
    from forest_bot.qa import remove_unsupported_affordability
    text = ("Визначити, де дешевше, неможливо: різні одиниці [1][2].\n"
            "Але взимку у соснах вигідніше [1].")
    out = remove_unsupported_affordability(text)
    assert "вигідніше" not in out and out.count("неможливо") == 1


# ------------------------------------------------------------------ general grounding check

def _docs(store, *titles):
    return evidence(store, *titles).docs


async def test_grounding_detects_absence_relation_and_dropped_condition(archive, store):
    from forest_bot.qa import grounding_issues
    docs = _docs(store, FEES_DUB, FEES_PINES)
    kinds = lambda s: {k for k, _, _ in grounding_issues(s, docs, True)}
    assert "absence" in kinds("У «Під дубом» зимового зменшення немає.")
    assert "absence" in kinds("Пільги під дубом не передбачені.")
    assert "relation" in kinds("Внески кладуть у кошик, який належить Кабанові.")
    assert "qualifier" in kinds("Підсумок: пташенята й поросята не платять.")
    assert "affordability" in kinds("Тому взимку «У соснах» менш обтяжливо.")


async def test_grounding_accepts_faithful_sentences(archive, store):
    from forest_bot.qa import grounding_issues
    docs = _docs(store, FEES_DUB, FEES_PINES)
    for sentence in ("Внески віддають Кабанові в кошик біля входу [2].",
                     "Пташенята й поросята до року не платять [2].",
                     "Про зимове зменшення внеску в документі «Під дубом» не згадано.",
                     "Узимку внески вдвічі менші [1].",
                     "Зайчик може платити морквою з поля: 1 морквина за 2 шишки [1].",
                     "Визначити, де дешевше, за цими документами неможливо [1][2]."):
        assert grounding_issues(sentence, docs, True) == [], sentence


async def test_grounding_issues_trigger_one_correction_with_the_exact_sentences(conversation, archive):
    bad = ("Під дубом — 4 жолуді на місяць [2]. Зимового зменшення під дубом немає [2]. "
           "Внески кладуть у кошик, що належить Кабанові [2].")
    good = ("Під дубом — 4 жолуді на місяць [2]. Про зимове зменшення в документі не згадано. "
            "Внески віддають Кабанові в кошик біля входу [2].")
    calls = []

    def answer(messages):
        calls.append(messages)
        return {"status": "answered", "answer": good if "Знайдено проблеми" in messages[-1]["content"] else bad}

    conversation.llm.route = {"intent": "question", "comparison": True, "search_query": "внески",
                              "keywords": ["внесок"]}
    conversation.llm.answer_fn = answer
    html = (await conversation.on_text(404, QUESTION))[-1].text
    assert len(calls) == 2
    correction = calls[1][-1]["content"]
    assert "Зимового зменшення під дубом немає" in correction and "належить" in correction
    assert "немає" not in html.split("<b>Джерела:</b>")[0] and "належить" not in html


async def test_fallback_removes_sentences_still_failing_after_correction(archive, store):
    from forest_bot.qa import apply_grounding_fallback
    docs = _docs(store, FEES_DUB, FEES_PINES)
    text = ("Під дубом — 4 жолуді з дорослого звіра на місяць [1].\n"
            "Пташенята й поросята до року не платять [1].\n"
            "Зимового зменшення під дубом немає.\n"
            "Внески кладуть у кошик, що належить Кабанові.\n"
            "Підсумок: пташенята й поросята не платять.")
    out = apply_grounding_fallback(text, docs, comparison=True)
    assert "4 жолуді з дорослого звіра" in out and "до року не платять" in out     # faithful facts kept
    assert "немає" not in out and "належить" not in out
    assert "Підсумок" not in out          # redundant restatement without «до року» dropped


async def test_qualifier_restatement_is_kept_when_it_is_the_only_statement(archive, store):
    from forest_bot.qa import apply_grounding_fallback
    docs = _docs(store, FEES_DUB)
    text = "Пташенята й поросята не платять [1]."
    assert apply_grounding_fallback(text, docs, comparison=False) == text   # nothing to fall back to: kept, logged


async def test_grounding_detects_possessive_ownership_dropped_subject_and_unsupported_numbers(archive, store):
    from forest_bot.qa import grounding_issues
    docs = _docs(store, FEES_DUB, FEES_PINES)
    kinds = lambda s: {k for k, _, _ in grounding_issues(s, docs, True)}
    assert "relation" in kinds("Місце здачі: у дубі кошик Кабанова.")
    assert "eligibility" in kinds("Заміна: 1 морквина за 2 шишки.")
    assert "eligibility" not in kinds("Зайчик може платити морквою: 1 морквина за 2 шишки [1].")
    assert "number" in kinds("У соснах внесок щотижня (8 шишок на місяць).")
    assert "number" not in kinds("Внесок — 4 жолуді на місяць, до 10 числа [2].")


def test_parser_accepts_marker_without_colon():
    from forest_bot.qa import parse_answer_text
    out = parse_answer_text("НОТАТКИ:\n[1] так\nСТАТУС: answered\n**ВІДПОВІДЬ**\nУ листопаді [1].")
    assert out["answer"] == "У листопаді [1]."


def test_grounding_check_has_no_false_positives_on_the_archive_itself():
    """Every source sentence, checked against its own document, is faithful by definition."""
    from types import SimpleNamespace
    from forest_bot.qa import _sentences_of, grounding_issues
    from .conftest import archive_texts
    flagged = []
    for d in archive_texts():
        ev = [SimpleNamespace(text=d["text"], document=SimpleNamespace(title=d["filename"]))]
        for s in _sentences_of(d["text"]):
            if len(s) >= 15:
                flagged += [(k, s) for k, _, _ in grounding_issues(s, ev, comparison=True)]
    # The only flag: the exchange rate restated without its subject — misleading when quoted alone.
    assert flagged == [("eligibility", "Обмін такий: 1 морквина за 2 шишки.")]


# ------------------------------------------------------------------ eligibility vs. exclusivity

BADGER = "Я дорослий борсук у соснах, можу замість шишок платити морквою?"
EXPECTED_NEUTRAL = ("У документі така заміна передбачена для Зайчика [1]. Для борсука її не зазначено, тому "
                    "підтвердити можливість заміни не можу.")


def _single(text, title="Внески"):
    from types import SimpleNamespace
    return [SimpleNamespace(text=text, document=SimpleNamespace(title=title))]


def test_naming_an_eligible_subject_is_not_exclusivity():
    from forest_bot.qa import grounding_issues
    rule = _single("Внески\nЗайчик може платити морквою з поля. Обмін такий: 1 морквина за 2 шишки.")
    kinds = lambda s: [k for k, _, _ in grounding_issues(s, rule, False)]
    assert kinds("Ні, заміна шишок на моркву дозволена лише зайчику [1].") == ["exclusivity"]
    assert kinds("Ні, дорослий борсук не може платити морквою [1].") == ["exclusivity"]
    assert kinds("Морквою може платити тільки Зайчик [1].") == ["exclusivity"]
    assert kinds(EXPECTED_NEUTRAL) == []
    assert kinds("Зайчик може платити морквою з поля: 1 морквина за 2 шишки [1].") == []


def test_explicit_exclusivity_in_the_source_is_respected():
    from forest_bot.qa import grounding_issues
    rule = _single("Внески\nЛише Зайчик може платити морквою з поля. Іншим звірам така заміна заборонена.")
    assert grounding_issues("Ні, морквою може платити лише Зайчик [1].", rule, False) == []


def test_exclusivity_fallback_replaces_sentence_with_neutral_statement():
    from forest_bot.qa import apply_grounding_fallback
    rule = _single("Внески\nЗайчик може платити морквою з поля.")
    out = apply_grounding_fallback("Ні, заміна дозволена лише зайчику [1].", rule, False)
    assert "лише" not in out and not out.startswith("Ні")
    assert "названо так: «Зайчик може платити морквою з поля»" in out and "Для інших звірів її не зазначено" in out


async def test_badger_question_gets_one_correction_towards_neutral_answer(conversation, archive):
    bad = "Ні, заміна шишок на моркву дозволена лише Зайчику [1]."
    calls = []

    def answer(messages):
        calls.append(messages)
        return {"status": "answered",
                "answer": EXPECTED_NEUTRAL if "Знайдено проблеми" in messages[-1]["content"] else bad}

    conversation.llm.route = {"intent": "question", "locations": ["u_sosnakh"], "needs_location": False,
                              "search_query": "Чи можна платити внесок морквою замість шишок?",
                              "keywords": ["морква", "шишки", "заміна", "внесок"]}
    conversation.llm.answer_fn = answer
    html = (await conversation.on_text(405, BADGER))[-1].text
    assert len(calls) == 2 and "вигадана заборона" in calls[1][-1]["content"]
    body = html.split("<b>Джерела:</b>")[0]
    assert "лише" not in body and not body.lstrip().startswith("Ні")
    assert "передбачена для Зайчика" in body and "Членські внески рукавички У соснах" in html
