"""Requested scope and attribution through the pipeline (comparison retest, 18 Oct 2026, late evening).

What these offline tests can prove: that post-processing (citation repair, measure check, fallback) keeps
correct citations and neither inserts nor deletes unrelated facts for replayed outputs, and which lines the
prompt contains. They cannot prove that a live model will follow the prompt (e.g. omit unsolicited dates).
"""

import re
from types import SimpleNamespace as N

import pytest

from forest_bot.qa import _claims, _measure_issues, requested_aspects, verify_citations
from forest_bot.textnorm import tokenize

from .conftest import archive_texts, evidence_titles

CONIFER = "Як утеплити рукавичку в хвойному лісі"
RIVER = "Як утеплити рукавичку біля річки"
CALENDAR = "Календар сезонів"
TEXTS = {d["filename"].rsplit(".", 1)[0]: d["text"] for d in archive_texts()}


def doc(title, levels=None, text=None):
    return N(text=text if text is not None else TEXTS[title], document=N(title=title), levels=levels or {})


# ------------------------------------------------------------------ 1. months: normalised, not proof alone

def test_month_forms_share_one_token_and_other_words_are_untouched():
    assert len(set(tokenize("жовтень жовтня жовтні жовтнем"))) == 1
    assert len(set(tokenize("листопад листопада листопаді листопадом"))) == 1
    assert tokenize("березень")[0] != tokenize("береза")[0]                      # a month is not a tree


def test_october_insulation_claim_is_not_backed_by_a_calendar_mentioning_october_for_other_things():
    calendar = doc("Календар", text="Жовтень\nЖолуді збирають у жовтні.\nРукавички утеплюють у листопаді.")
    docs = [doc(CONIFER), calendar]
    assert verify_citations("У хвойному лісі рукавичку утеплюють у жовтні [2].", docs).endswith("[1].")
    # the real calendar too («Серпень, вересень, жовтень» and acorns in October)
    docs = [doc(CONIFER), doc(CALENDAR)]
    out = verify_citations("Хвойний ліс — жовтень (конкретна порада, пріоритет над листопадом із загального "
                           "календаря) [1].", docs)
    assert re.search(r"календаря\) \[1\]", out)                                  # the coniferous source kept


MATERIALS = "Порівняй, чим утеплюють рукавички у хвойному лісі та біля річки."
LOCAL_MEETINGS = doc("Збори рукавички У соснах", {"u_sosnakh": 3},
                     text="Збори рукавички «У соснах»\nЗбори мешканців відбуваються в жовтні біля входу.")


def test_more_specific_document_with_october_for_another_topic_never_supports_insulation_october():
    docs = [doc(CONIFER, {"khvoinyi": 2, "u_sosnakh": 2}), LOCAL_MEETINGS]
    # cited to the unrelated local document: replaced by the document that ties October to insulation
    assert verify_citations("Хвойний ліс — жовтень [2].", docs, question=MATERIALS) == "Хвойний ліс — жовтень [1]."
    # cited correctly: the more specific local document is not added
    assert verify_citations("Хвойний ліс — жовтень [1].", docs, question=MATERIALS) == "Хвойний ліс — жовтень [1]."


def test_without_evidence_linking_the_month_to_the_subject_nothing_is_reassigned():
    docs = [doc(CONIFER, {"khvoinyi": 2, "u_sosnakh": 2}), LOCAL_MEETINGS]
    for text in ("Хвойний ліс — жовтень [1].", "Хвойний ліс — жовтень [2]."):
        assert verify_citations(text, docs, question="А коли?") == text


# ------------------------------------------------------------------ 2. measure check per claim, subject, place

CON_DOC = doc(CONIFER, {"khvoinyi": 2})
RIV_DOC = doc(RIVER, {"richka": 2})


@pytest.mark.parametrize("sentence", [
    # contrast between places (the observed sentence)
    "- **Метод укладання:** у хвойному лісі зазначено конкретний шар «у три лапи» [1], у річковій частині метод "
    "не уточнюється, однак є вимога підняти рукавичку на коріння [2].",
    # subordinate clauses carry the place they belong to
    "У хвойному лісі, де мох легко знайти, кладуть шар у три лапи, а біля річки, де мох гниє, товщину не "
    "вказано [1][2].",
])
def test_a_contrast_with_another_place_is_not_a_denial_of_the_measure(sentence):
    assert _measure_issues([sentence], [CON_DOC, RIV_DOC]) == []


def test_an_unplaced_denial_in_a_multi_place_answer_is_not_attributed_to_one_place():
    sentence = "Шар у три лапи радять у хвойному лісі [1]; товщина іншого утеплення не вказана [2]."
    assert _measure_issues([sentence], [CON_DOC, RIV_DOC]) == []


def test_a_real_denial_of_the_measure_is_still_detected():
    single = [doc(CONIFER, {"u_sosnakh": 2})]
    sentence = ("Товщина утеплення не уточнюється в документах; лише зазначено, що слід класти мох і суху хвою "
                "шаром у три лапи [1].")
    assert [k for k, _, _ in _measure_issues([sentence], single)] == ["measure"]
    placed = "У хвойному лісі кладуть шар у три лапи, але товщину утеплення там не вказано [1]."
    assert [k for k, _, _ in _measure_issues([placed], [CON_DOC, RIV_DOC])] == ["measure"]


def test_decimal_commas_do_not_split_claims():
    parts = [t.strip() for t, _ in _claims("Дятел платить половину, тобто 1,5 шишки на місяць [1].")]
    assert any("1,5 шишки" in p for p in parts)


# ------------------------------------------------------------------ 3. requested aspects

@pytest.mark.parametrize("question, aspects", [
    ("Порівняй, чим утеплюють рукавички у хвойному лісі та біля річки.", ["material"]),
    ("Коли, чим і яким шаром утеплювати рукавичку «У соснах»?", ["time", "material", "thickness"]),
    ("Порівняй, коли, чим і яким шаром утеплюють у хвойному лісі й біля річки",
     ["time", "material", "thickness"]),
    ("Порівняй внески під дубом і у соснах: скільки та як часто треба платити?", ["amount", "frequency"]),
    ("Що робити, коли бачиш мисливців?", []),                 # conjunction «коли», not a time question
])
def test_every_explicitly_requested_aspect_is_detected(question, aspects):
    assert [c for c, _ in requested_aspects(question)] == aspects


MATERIALS_Q = "Порівняй, чим утеплюють рукавички у хвойному лісі та біля річки."
ROUTE = {"intent": "question", "search_query": "утеплення рукавичок матеріал", "keywords": ["утеплення"]}


def _replay(conversation, template):
    def answer_fn(messages):
        text = template
        for n, t in enumerate(evidence_titles(messages[1]["content"]), start=1):
            text = text.replace("{" + t + "}", f"[{n}]")
        return {"status": "answered", "answer": text}

    conversation.llm.route = ROUTE
    conversation.llm.answer_fn = answer_fn


async def test_prompt_names_requested_aspects_and_overlap_accurately(conversation, archive):
    _replay(conversation, "Хвойний ліс: мох [1].")
    await conversation.on_text(1, MATERIALS_Q)
    header = conversation.llm.answer_prompts[0].split("<document>")[0]
    assert "Запитані аспекти: чим (матеріали) і потрібна для цього підготовка." in header
    assert "Не додавай того, про що не питали (строк (коли)" in header
    # regional advice applies to member mittens; overlap and local advice may add conditions or conflicts
    assert "Поради частини лісу «Біля річки» діють для всіх її рукавичок" in header
    assert "«Під вербою» належить також до «Листяний ліс»" in header
    assert "можуть додавати умови або розходитися з порадою частини лісу" in header

    conversation.llm.calls.clear()
    await conversation.on_text(2, "Порівняй, коли, чим і яким шаром утеплюють у хвойному лісі й біля річки")
    header = conversation.llm.answer_prompts[0].split("<document>")[0]
    assert "Запитані аспекти: строк (коли); чим (матеріали) і потрібна для цього підготовка; товщина шару." in header
    assert "Не додавай того, про що не питали (строк" not in header


# ------------------------------------------------------------------ end-to-end replay of the retest trace

# Reconstructed from the log: gpt-oss-120b answered; the measure check flagged the «Метод укладання» item
# (a contrast) and a qwen correction followed; citation repair moved the October clause from the coniferous
# document to the calendar; the fallback inserted the coniferous measure as a quote.
OBSERVED = (
    "**Хвойний ліс:** мох і суха хвоя, шаром у три лапи {" + CONIFER + "}.\n"
    "**Біля річки:** рукавичку піднімають на коріння й утеплюють очеретом, бо мох біля води гниє {" + RIVER + "}.\n"
    "- **Метод укладання:** у хвойному лісі зазначено конкретний шар «у три лапи» {" + CONIFER + "}, у річковій "
    "частині метод не уточнюється, однак є вимога підняти рукавичку на коріння {" + RIVER + "}.\n"
    "- **Час виконання:** хвойний ліс — жовтень (конкретна порада, пріоритет над листопадом із загального "
    "календаря) {" + CONIFER + "}.")


def _sources(html):
    return dict(re.findall(r"\[(\d+)\] «([^»]+)»", html.partition("<b>Джерела:</b>")[2]))


async def test_post_processing_keeps_attribution_and_inserts_nothing(conversation, archive):
    _replay(conversation, OBSERVED)
    html = (await conversation.on_text(3, MATERIALS_Q))[-1].text
    body = html.partition("<b>Джерела:</b>")[0]
    sources = _sources(html)
    assert "У документі сказано" not in body                                       # no inserted quote
    assert len(conversation.llm.answer_prompts) == 1                               # nothing flagged, no correction
    october = re.search(r"календаря\) ((?:\[\d+\])+)", body).group(1)
    assert CONIFER in [sources[n] for n in re.findall(r"\d+", october)]            # October stays with [coniferous]
    assert "мох і суха хвоя, шаром у три лапи" in body
    assert "піднімають на коріння й утеплюють очеретом, бо мох біля води гниє" in body
    # Not deleted by post-processing. This only shows there is no false measure alarm; it does not verify the
    # statement, which is inaccurate — the river document does give preparation and insulation steps.
    assert "у річковій частині метод не уточнюється" in body


async def test_expected_materials_answer_has_exactly_the_two_insulation_sources(conversation, archive):
    expected = ("**Хвойний ліс:** мохом і сухою хвоєю, шаром у три лапи {" + CONIFER + "}.\n"
                "**Біля річки:** рукавичку піднімають на коріння й утеплюють очеретом; мох біля води гниє {"
                + RIVER + "}.")
    _replay(conversation, expected)
    html = (await conversation.on_text(4, MATERIALS_Q))[-1].text
    assert sorted(_sources(html).values()) == sorted([CONIFER, RIVER])
    assert "У документі сказано" not in html and len(conversation.llm.answer_prompts) == 1
