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
