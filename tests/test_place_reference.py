"""A region cited as the source of a proposed recommendation vs places actually asked about.

«Біля бобрової греблі хочу утеплити рукавичку мохом, як радять для хвойного лісу» asks about the dam mitten;
the coniferous forest is where the suggestion comes from. Genuine comparisons stay comparisons, and
specificity hints are only built between documents applying to the same place.
"""

from types import SimpleNamespace as N

import pytest

from forest_bot import geography as geo
from forest_bot.qa import _specificity_pairs

from .conftest import evidence_titles

DAM_SUGGESTION = "Біля бобрової греблі хочу утеплити рукавичку мохом, як радять для хвойного лісу. Підійде?"
GENUINE = "Порівняй утеплення у хвойному лісі й біля річки"
RIVER_INSULATION = "Як утеплити рукавичку біля річки"
CONIFER_INSULATION = "Як утеплити рукавичку в хвойному лісі"


@pytest.mark.parametrize("message, mittens, regions, reference, comparison", [
    (DAM_SUGGESTION, ["bilia_hrebli"], [], ["khvoinyi"], False),
    ("Біля бобрової греблі хочу утеплити рукавичку мохом за порадою для хвойного лісу.",
     ["bilia_hrebli"], [], ["khvoinyi"], False),
    ("Біля бобрової греблі можна утеплюватися мохом, як радять у хвойному лісі?",
     ["bilia_hrebli"], [], ["khvoinyi"], False),
    # genuine comparisons and coordinated places are unchanged
    (GENUINE, [], ["khvoinyi", "richka"], [], True),
    ("Порівняй утеплення біля бобрової греблі і в хвойному лісі", ["bilia_hrebli"], ["khvoinyi"], [], True),
    ("Що робити при пожежі біля бобрової греблі і в хвойному лісі?", ["bilia_hrebli"], ["khvoinyi"], [], True),
    # a region of the mitten itself only describes it
    ("Під вербою утеплюють, як радять біля річки?", ["pid_verboyu"], [], [], False),
])
def test_target_place_vs_region_cited_as_advice_source(message, mittens, regions, reference, comparison):
    d = geo.detect_locations(message)
    assert (d.mittens, d.regions, d.reference_regions, d.comparison) == (mittens, regions, reference, comparison)


def test_specificity_hints_only_between_documents_for_the_same_place():
    same = "Утеплюйте рукавичку мохом шаром у три лапи, коли настане жовтень."
    other = "Утеплюйте рукавичку очеретом шаром у три лапи, коли настане листопад."
    local_pines = N(text=same, levels={"u_sosnakh": 3})
    river = N(text=other, levels={"bilia_hrebli": 2})
    conifer = N(text=other, levels={"u_sosnakh": 2, "pid_yalynoyu": 2})
    conifer_dam = N(text=same, levels={"pid_yalynoyu": 2})
    assert _specificity_pairs([local_pines, river]) == []            # different places: never ranked
    assert _specificity_pairs([conifer_dam, river]) == []            # equal-level regions: never ranked
    assert [(i, j) for i, j, *_ in _specificity_pairs([local_pines, conifer])] == [(1, 2)]   # same place


async def test_suggestion_from_another_region_is_answered_for_the_target_mitten_only(conversation, archive):
    # The router read the observed request as a comparison of four mittens; deterministic detection now
    # keeps the dam as the only target and marks the coniferous forest as a cited source.
    conversation.llm.route = {"intent": "question", "comparison": True, "needs_location": False,
                              "search_query": "утеплення мох", "keywords": ["утеплення", "мох"]}
    answer = ("Ні. Біля річки мох не годиться, бо біля води мох гниє; утеплюйте очеретом і підніміть рукавичку на "
              "коріння {" + RIVER_INSULATION + "}. Порада хвойного лісу утеплювати мохом до вашої рукавички не "
              "застосовується, бо вона не з хвойного лісу.")

    def answer_fn(messages):
        text = answer
        for n, t in enumerate(evidence_titles(messages[1]["content"]), start=1):
            text = text.replace("{" + t + "}", f"[{n}]")
        return {"status": "answered", "answer": text}

    conversation.llm.answer_fn = answer_fn
    html = (await conversation.on_text(1, DAM_SUGGESTION))[-1].text
    prompt = conversation.llm.answer_prompts[0]
    header = prompt.split("<document>")[0]
    assert header.startswith("Місце, про яке питають: рукавичка «Біля бобрової греблі» (Біля річки)\n")
    assert "порівняння між місцями" not in header
    assert "Користувач посилається на пораду для частини лісу «Хвойний ліс»" in header
    assert "Під ялиною" not in header and "У соснах" not in header
    titles = evidence_titles(prompt)
    assert RIVER_INSULATION in titles and CONIFER_INSULATION not in titles     # only applicable guidance
    body, _, sources = html.partition("<b>Джерела:</b>")
    assert "мох не годиться" in body and "не застосовується" in body and "пріоритет" not in body
    assert RIVER_INSULATION in sources
    assert conversation.store.get_state(1)["scope"]["mittens"] == ["bilia_hrebli"]   # follow-ups stay here


async def test_genuine_region_comparison_keeps_both_regions_without_priority_hints(conversation, archive):
    conversation.llm.route = {"intent": "question", "search_query": "утеплення", "keywords": ["утеплення"]}
    conversation.llm.answer_fn = lambda m: {"status": "answered", "answer": "Хвойний ліс: мох [1]. Біля річки: очерет [2]."}
    await conversation.on_text(2, GENUINE)
    prompt = conversation.llm.answer_prompts[0]
    header = prompt.split("<document>")[0]
    assert "Хвойний ліс" in header and "Біля річки" in header and "порівняння між місцями" in header
    titles = evidence_titles(prompt)
    assert RIVER_INSULATION in titles and CONIFER_INSULATION in titles
    i, j = titles.index(CONIFER_INSULATION) + 1, titles.index(RIVER_INSULATION) + 1
    assert f"[{i}] і [{j}]" not in header and f"[{j}] і [{i}]" not in header   # equal level: no ranking hint


# ------------------------------------------------------------------ month repair in multi-region answers

CALENDAR = "Календар сезонів"
COMPARE_Q = "Порівняй, чим утеплюють рукавички у хвойному лісі та біля річки."
COMPARE_ROUTE = {"intent": "question", "search_query": "утеплення рукавичок матеріал", "keywords": ["утеплення"]}


def _replay(template, corrected=None):
    from forest_bot.llm import LLMRateLimited

    def answer_fn(messages):
        if "Знайдено проблеми" in messages[-1]["content"]:
            if corrected is None:
                raise LLMRateLimited(30)
            text = corrected
        else:
            text = template
        for n, t in enumerate(evidence_titles(messages[1]["content"]), start=1):
            text = text.replace("{" + t + "}", f"[{n}]")
        return {"status": "answered", "answer": text}

    return answer_fn


async def _compare(conversation, template):
    conversation.llm.route = COMPARE_ROUTE
    conversation.llm.answer_fn = _replay(template)
    html = (await conversation.on_text(3, COMPARE_Q))[-1].text
    body, _, sources = html.partition("<b>Джерела:</b>")
    return body, sources


# Reconstructed from the retest log (qwen/qwen3.8-27b; correction rate-limited): the three November
# sentences below were flagged and each replaced by the coniferous «Утеплюйте рукавичку в жовтні».
OBSERVED_COMPARISON = (
    "**Хвойний ліс:** утеплюють мохом і сухою хвоєю шаром у три лапи, у жовтні {" + CONIFER_INSULATION + "}.\n"
    "**Біля річки:**\n"
    "- Біля річки спершу піднімають рукавичку на коріння, а потім утеплюють очеретом (мох біля води гниє), "
    "дотримуючись загального строку — у листопаді {" + RIVER_INSULATION + "}{" + CALENDAR + "}.\n"
    "- **Строк:** у місцевій пораді для частини «Біля річки» конкретний місяць не вказано, тому діє загальна "
    "порада для всього лісу — листопад {" + CALENDAR + "}.\n"
    "Загальний лісовий календар зазначає листопад як місяць утеплення {" + CALENDAR + "}.")


async def test_coniferous_month_is_never_moved_into_the_riverside_section(conversation, archive):
    body, sources = await _compare(conversation, OBSERVED_COMPARISON)
    river_part = body.split("Біля річки:")[1]
    assert "Утеплюйте рукавичку в жовтні" not in body and "У документі сказано" not in body
    assert "жовтні" not in river_part
    assert "мохом і сухою хвоєю шаром у три лапи" in body
    assert "піднімають рукавичку на коріння" in river_part and "очеретом" in river_part
    assert CONIFER_INSULATION in sources and RIVER_INSULATION in sources


async def test_focused_materials_comparison_is_delivered_unchanged(conversation, archive):
    expected = ("**Хвойний ліс:** мохом і сухою хвоєю, шаром у три лапи {" + CONIFER_INSULATION + "}.\n"
                "**Біля річки:** рукавичку піднімають на коріння й утеплюють очеретом; мох біля води гниє {"
                + RIVER_INSULATION + "}.")
    body, sources = await _compare(conversation, expected)
    assert "мохом і сухою хвоєю, шаром у три лапи" in body
    assert "рукавичку піднімають на коріння й утеплюють очеретом; мох біля води гниє" in body
    assert "жовтні" not in body and "листопаді" not in body
    assert CONIFER_INSULATION in sources and RIVER_INSULATION in sources and CALENDAR not in sources
    assert len(conversation.llm.answer_prompts) == 1


async def test_two_mittens_with_different_valid_months_are_not_repaired(conversation, archive):
    # «У соснах» (coniferous: October) vs «Біля бобрової греблі» (river only; no local or river month, so the
    # forest-wide calendar's November applies). Mitten-level, unambiguous — unlike a whole riverside region,
    # whose «Під вербою» also follows the deciduous rule (after leaf fall, before the first snow).
    conversation.llm.route = {"intent": "question", "search_query": "утеплення коли", "keywords": ["утеплення"]}
    conversation.llm.answer_fn = _replay(
        "У соснах рукавичку утеплюють у жовтні {" + CONIFER_INSULATION + "}. Біля бобрової греблі місцевого строку "
        "немає, тож діє календар для всього лісу: рукавички утеплюють у листопаді {" + CALENDAR + "}.")
    html = (await conversation.on_text(5, "Порівняй, коли утеплюють рукавичку у соснах і біля бобрової греблі."))[-1].text
    body = html.partition("<b>Джерела:</b>")[0]
    assert "У соснах рукавичку утеплюють у жовтні" in body
    assert "Біля бобрової греблі місцевого строку немає, тож діє календар для всього лісу" in body
    assert "листопаді" in body.split("Біля бобрової греблі")[1]
    assert len(conversation.llm.answer_prompts) == 1                 # no issue, no correction call


async def test_single_place_month_repair_still_works_and_never_repeats_the_quote(conversation, archive):
    conversation.llm.route = {"intent": "question", "locations": ["u_sosnakh"], "needs_location": False,
                              "search_query": "утеплення коли чим", "keywords": ["утеплення"]}
    conversation.llm.answer_fn = _replay(
        "Рукавички утеплюють у листопаді {" + CALENDAR + "}. Отже, утеплюйте рукавичку в листопаді {" + CALENDAR
        + "}. Утеплюють мохом і сухою хвоєю шаром у три лапи {" + CONIFER_INSULATION + "}.")
    html = (await conversation.on_text(4, "Коли й чим утеплювати рукавичку «У соснах»?"))[-1].text
    body = html.partition("<b>Джерела:</b>")[0]
    assert "листопаді" not in body
    assert body.count("Утеплюйте рукавичку в жовтні") == 1
    assert "мохом і сухою хвоєю шаром у три лапи" in body
