"""Dialogue context (place, season, identity) across a whole conversation — offline, fake LLM."""

import re

from forest_bot import dialogue

from .conftest import evidence_titles

FEES_PINES = "Членські внески рукавички У соснах"
T1 = "Я зайчик з рукавички «У соснах». Скільки платити внесок узимку?"
T2 = "А я дорослий борсук, можу замість шишок платити морквою?"


def test_extract_and_merge_context():
    assert dialogue.extract(T1) == {"season": "зима", "season_scope": "question", "identity": "зайчик"}
    assert dialogue.extract(T2) == {"identity": "дорослий борсук"}
    assert dialogue.extract("Зараз зима, скільки платити?")["season_scope"] == "current"
    merged = dialogue.merge(dialogue.extract(T1), dialogue.extract(T2))
    assert merged == {"season": "зима", "season_scope": "question", "identity": "дорослий борсук"}
    assert dialogue.merge(merged, {"season": "літо"})["season"] == "літо"
    assert dialogue.extract("Скільки внесок під дубом?") == {}


def _cite(prompt: str, title: str) -> int:
    return evidence_titles(prompt).index(title) + 1


def _routes():
    return {
        T1: dict(locations=["u_sosnakh"], follow_up=False,
                 answer_focus="Скільки платити членський внесок у рукавичці «У соснах» узимку?",
                 search_query="Членський внесок: скільки платити", keywords=["внесок", "шишки", "узимку"],
                 context={"season": "зима", "identity": "зайчик"}),
        T2: dict(locations=[], follow_up=True,
                 answer_focus="Чи може дорослий борсук у рукавичці «У соснах» платити внесок морквою замість шишок?",
                 search_query="Членський внесок: заміна шишок морквою", keywords=["морква", "заміна", "шишки"],
                 context={"identity": "дорослий борсук"}),
    }


def _router(routes):
    def route(messages):
        message = messages[1]["content"].split("<message>")[1].split("</message>")[0]
        return {"intent": "question", "needs_location": False, **routes[message]}
    return route


GOOD_T1 = "Узимку внески вдвічі менші, тобто 1 соснова шишка з дорослого звіра щотижня [{n}]."
BAD_T2 = ("Зайчик може платити морквою з поля [{n}]. Для інших дорослих звірів, включно з борсуками, заміна "
          "не зазначена [{n}]. Тому, як дорослий борсук, ви повинні сплачувати 2 соснові шишки щотижня [{n}].")
GOOD_T2 = ("У документі заміна шишок морквою передбачена для Зайчика [{n}]. Для борсука її не зазначено, тому "
           "підтвердити таку можливість не можу. Узимку внески вдвічі менші, тобто 1 соснова шишка щотижня [{n}].")


async def _run(conversation, routes, t2_answers):
    """Two-turn dialogue; t2_answers = (first answer, answer after correction)."""
    prompts, corrections = [], []

    def answer(messages):
        prompt = messages[1]["content"]
        n = _cite(prompt, FEES_PINES)
        if "Знайдено проблеми" in messages[-1]["content"]:
            corrections.append(messages[-1]["content"])
            return {"status": "answered", "answer": t2_answers[1].format(n=n)}
        prompts.append(prompt)
        text = GOOD_T1 if len(prompts) == 1 else t2_answers[0]
        return {"status": "answered", "answer": text.format(n=n)}

    conversation.llm.route = _router(routes) if routes else None
    conversation.llm.answer_fn = answer
    await conversation.on_text(500, T1)
    replies = await conversation.on_text(500, T2)
    return replies[-1].text, prompts, corrections


async def test_full_winter_dialogue_keeps_place_season_and_updated_identity(conversation, archive, store):
    html, prompts, corrections = await _run(conversation, _routes(), (BAD_T2, GOOD_T2))
    t2 = prompts[1]
    # Place, season and the UPDATED identity reach answer generation…
    assert "рукавичка «У соснах»" in t2.split("Документи:")[0]
    context_line = next(line for line in t2.split("\n") if line.startswith("Обставини користувача"))
    assert "сезон — зима" in context_line and "користувач — дорослий борсук" in context_line
    assert "зайчик" not in context_line
    # …and the evidence keeps both conditions: the seasonal rule and the rule named for Зайчик.
    assert evidence_titles(t2)[0] == FEES_PINES
    assert "Узимку внески вдвічі менші" in t2 and "Зайчик може платити морквою з поля" in t2
    assert store.get_state(500)["context"] == {"season": "зима", "season_scope": "question",
                                               "identity": "дорослий борсук"}
    # The first answer derived an obligation from silence and ignored winter -> one correction naming both.
    assert len(corrections) == 1
    assert "висновок про обов'язок чи заборону" in corrections[0] and "сезон «зима»" in corrections[0]
    body = html.split("<b>Джерела:</b>")[0]
    assert "передбачена для Зайчика" in body and "Для борсука її не зазначено" in body
    assert "1 соснова шишка щотижня" in body
    assert not re.search(r"повинн|мусит|\bНі\b|лише", body)
    assert FEES_PINES in html


async def test_stubborn_answer_is_fixed_deterministically(conversation, archive):
    html, _, corrections = await _run(conversation, _routes(), (BAD_T2, BAD_T2))
    body = html.split("<b>Джерела:</b>")[0]
    assert len(corrections) == 1
    assert "повинні" not in body                                   # obligation-from-silence removed
    assert "Зверніть увагу на сезон: «Узимку внески вдвічі менші." in body   # seasonal rule restored
    assert "Зайчик може платити морквою з поля" in body              # stated eligibility preserved


async def test_dialogue_context_survives_without_the_router(conversation, archive, store):
    """Router unavailable: deterministic extraction keeps winter + updated identity, place from history."""
    html, prompts, _ = await _run(conversation, None, (GOOD_T2, GOOD_T2))
    t2 = prompts[1]
    assert "сезон — зима" in t2 and "користувач — дорослий борсук" in t2
    assert evidence_titles(t2)[0] == FEES_PINES
    assert store.get_state(500)["scope"]["mittens"] == ["u_sosnakh"]


async def test_reset_clears_dialogue_context(conversation, archive, store):
    await _run(conversation, _routes(), (GOOD_T2, GOOD_T2))
    await conversation.on_command(500, "/reset")
    assert store.get_state(500)["context"] == {}


def test_derived_winter_value_is_allowed_but_unrelated_conversion_is_not(archive, store):
    from types import SimpleNamespace
    from forest_bot.qa import grounding_issues
    doc = next(d for d in store.active_documents() if d.title == FEES_PINES)
    ev = [SimpleNamespace(text=doc.text, document=doc)]
    kinds = lambda s: {k for k, _, _ in grounding_issues(s, ev, False, {"season": "зима"})}
    assert "number" not in kinds("Узимку внески вдвічі менші, тобто 1 соснова шишка щотижня [1].")
    assert "number" in kinds("Це 8 шишок на місяць [1].")
    assert "season" in kinds("Внесок — 2 соснові шишки щотижня [1].")          # winter dialogue, rule ignored
    assert "season" not in kinds("Узимку внески вдвічі менші [1].")


# ------------------------------------------------------------------ concise derived answers

async def test_concise_derived_winter_amount_is_valid_without_spelling_out_the_calculation(archive, store):
    from types import SimpleNamespace
    from forest_bot.qa import grounding_issues
    doc = next(d for d in store.active_documents() if d.title == FEES_PINES)
    ev = [SimpleNamespace(text=doc.text, document=doc)]
    winter = {"season": "зима"}
    assert grounding_issues("1 шишка щотижня [1].", ev, False, winter) == []
    assert grounding_issues("Узимку — 1 соснова шишка щотижня [1].", ev, False, winter) == []
    # The regular amount in a winter dialogue still ignores the rule; an unrelated value is still unsupported.
    assert {k for k, _, _ in grounding_issues("2 соснові шишки щотижня [1].", ev, False, winter)} == {"season"}
    assert "number" in {k for k, _, _ in grounding_issues("3 шишки щотижня [1].", ev, False, winter)}
    # Outside a winter dialogue the halved value is not supported by anything.
    assert "number" in {k for k, _, _ in grounding_issues("0.5 шишки [1].", ev, False, {})}


# ------------------------------------------------------------------ context updates beyond /reset

def _analyze(message, known):
    from forest_bot import geography as geo
    from forest_bot.qa import merge_analysis
    from forest_bot.retrieval import QueryScope
    history = [{"q": "Скільки внесок у соснах?", "resolved": "Скільки внесок у рукавичці «У соснах»?"}]
    return merge_analysis(message, geo.detect_locations(message), None, QueryScope("mittens", ["u_sosnakh"]),
                          history, known)


KNOWN = {"season": "зима", "season_scope": "current", "identity": "дорослий борсук"}   # «зараз зима»


def test_new_season_replaces_the_old_one():
    a = _analyze("А влітку?", KNOWN)
    assert a.context["season"] == "літо" and a.persist_context["season"] == "літо"
    assert a.context["identity"] == "дорослий борсук"


def test_question_about_another_animal_does_not_change_the_users_identity():
    a = _analyze("А для зайчика?", KNOWN)
    assert a.turn_subject == "зайчика"
    assert a.context["identity"] == "дорослий борсук" and a.persist_context == KNOWN


def test_router_cannot_turn_a_question_about_another_animal_into_identity():
    from forest_bot import geography as geo
    from forest_bot.qa import merge_analysis
    from forest_bot.retrieval import QueryScope
    raw = {"intent": "question", "context": {"identity": "зайчик"}}
    a = merge_analysis("А для зайчика?", geo.detect_locations("А для зайчика?"), raw,
                       QueryScope("mittens", ["u_sosnakh"]), [{"q": "x"}], KNOWN)
    assert a.persist_context["identity"] == "дорослий борсук"


def test_hypothetical_circumstances_apply_to_this_answer_only():
    for message, key, value in (("Уявімо, що зараз літо. Скільки платити?", "season", "літо"),
                                ("Якщо я зайчик, чим можу платити?", "identity", "зайчик"),
                                ("А якби я був зайчиком?", None, None)):
        a = _analyze(message, KNOWN)
        assert a.hypothetical, message
        assert a.persist_context == KNOWN, message           # profile unchanged
        if key:
            assert a.context[key] == value, message            # … but this answer uses the hypothesis


def test_real_identity_change_is_remembered():
    a = _analyze("Я тепер дорослий зайчик, як мені платити?", KNOWN)
    assert not a.hypothetical and a.persist_context["identity"] == "дорослий зайчик"


async def test_context_updates_through_the_conversation(conversation, archive, store):
    conversation.llm.route = None                  # deterministic path
    for message in (T1, T2):
        await conversation.on_text(600, message)
    assert store.get_state(600)["context"]["season"] == "зима"          # follow-ups keep the question's season
    await conversation.on_text(600, "А для зайчика?")
    assert store.get_state(600)["context"]["identity"] == "дорослий борсук"
    assert "Поточне питання стосується іншого звіра — «зайчика»" in conversation.llm.answer_prompts[-1]
    await conversation.on_text(600, "Уявімо, що зараз літо. Скільки платити?")
    assert "сезон — літо" in conversation.llm.answer_prompts[-1]
    assert "гіпотетичне чи умовне питання" in conversation.llm.answer_prompts[-1]
    assert "season" not in store.get_state(600)["context"]              # hypothesis not stored; old topic ended
    await conversation.on_text(600, "А влітку?")
    assert store.get_state(600)["context"]["season"] == "літо"
    assert store.get_state(600)["context"]["identity"] == "дорослий борсук"


# ------------------------------------------------------------------ season scope (question vs. current)

def test_season_asked_about_ends_with_a_new_question_but_current_season_persists():
    asked = {"season": "зима", "season_scope": "question"}
    follow = _analyze("А хто приймає біля верби?", asked)
    assert follow.follow_up and follow.persist_context.get("season") == "зима"   # same thread keeps it
    new = _analyze("Де запасний ліхтар під вербою?", asked)
    assert "season" not in new.context and "season" not in new.persist_context  # new topic drops it
    current = {"season": "зима", "season_scope": "current"}
    assert _analyze("Де запасний ліхтар під вербою?", current).context["season"] == "зима"


async def test_winter_question_does_not_leak_into_an_unrelated_question(conversation, archive, store):
    conversation.llm.route = None
    await conversation.on_text(700, "Хто приймає внески біля бобрової греблі взимку?")
    assert store.get_state(700)["context"]["season"] == "зима"
    await conversation.on_text(700, "Де шукати гриби біля верби?")
    assert "Обставини користувача" not in conversation.llm.answer_prompts[-1]
    assert "season" not in store.get_state(700)["context"]


def test_season_remark_is_removed_when_no_document_relates_to_the_season():
    from types import SimpleNamespace
    from forest_bot.qa import deterministic_repairs
    lamp = [SimpleNamespace(text="Порада для рукавички «Під вербою».\nЗапасний ліхтар зберігається в жовтій "
                                 "скрині на ґанку.", document=None)]
    text = "Під вербою запасний ліхтар зберігається в жовтій скрині на ґанку [1]. Сезон — зима: це не впливає."
    out = deterministic_repairs(text, lamp, {"season": "зима", "season_scope": "current"})
    assert out == "Під вербою запасний ліхтар зберігається в жовтій скрині на ґанку [1]."
    pines = [SimpleNamespace(text="Узимку внески вдвічі менші.", document=None)]
    kept = "Узимку внески вдвічі менші [1]."
    assert deterministic_repairs(kept, pines, {"season": "зима"}) == kept


def test_priority_rule_forbids_calling_specific_advice_main():
    from forest_bot.qa import ANSWER_SYSTEM
    assert "відповідай лише конкретнішою" in ANSWER_SYSTEM
    assert "для цього місця вона не діє" in ANSWER_SYSTEM and "«основною» чи «головною»" in ANSWER_SYSTEM
    assert "коротко скажи, що вона має пріоритет" not in ANSWER_SYSTEM
