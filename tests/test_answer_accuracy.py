"""Answer accuracy regressions from manual tests (18 Oct 2026), run end to end on the real archive.

Each test drives the whole answer path (real retrieval, real prompt, deterministic checks, correction /
fallback, rendering) with a fake model that returns the kind of output observed in production, and checks
the FINAL answer and its sources, not only the prompt.
"""

import re

from forest_bot.llm import LLMRateLimited
from forest_bot.qa import grounding_issues

from .conftest import evidence_titles

WILLOW_CHARTER = "Статут ОСМД Під вербою"
WILLOW_FEES = "Членські внески рукавички Під вербою"
CONIFER_INSULATION = "Як утеплити рукавичку в хвойному лісі"
CALENDAR = "Календар сезонів"
DAM_PATHS = "Стежки до сусідів від рукавички Біля бобрової греблі"
FORDS = "Броди і коли бобри відкривають греблю"


def route(code, search, keywords):
    return {"intent": "question", "locations": [code], "needs_location": False, "search_query": search,
            "keywords": keywords}


def model(template, corrected=None):
    """Fake answer model: fills «{Title}» citations from the prompt's evidence numbering. The correction
    call returns `corrected`, or is rate-limited (all models busy) when `corrected` is None."""
    calls = []

    def answer(messages):
        calls.append(messages)
        titles = evidence_titles(messages[1]["content"])
        is_correction = "Знайдено проблеми" in messages[-1]["content"]
        if is_correction and corrected is None:
            raise LLMRateLimited(30)
        text = corrected if is_correction else template
        for n, t in enumerate(titles, start=1):
            text = text.replace("{" + t + "}", f"[{n}]")
        return {"status": "answered", "answer": text}

    return answer, calls


def split(html):
    body, _, sources = html.partition("<b>Джерела:</b>")
    return body, sources


async def ask(conversation, question, r, template, corrected=None):
    conversation.llm.route = r
    conversation.llm.answer_fn, calls = model(template, corrected)
    html = (await conversation.on_text(1, question))[-1].text
    prompt = conversation.llm.answer_prompts[0]
    return html, calls, prompt


# ------------------------------------------------------------------ multi-part question

WILLOW_Q = "Хто під вербою голова ОСМД, хто скарбник і кому віддавати внески взимку?"
WILLOW_ROUTE = route("pid_verboyu", "голова ОСМД, скарбник, внески взимку", ["голова", "скарбник", "внески"])


async def test_multi_part_answer_keeps_head_treasurer_and_winter_recipient(conversation, archive):
    # Observed (qwen fallback): the correct first sentence was removed as a "fabricated relation"
    # («Головою» looked like a possessive adjective) after the correction call was rate-limited.
    observed = (f"Головою ОСМД рукавички «Під вербою» є Качка, а скарбником — Жабка {{{WILLOW_CHARTER}}}. "
                f"Узимку, поки Жабка спить у мулі, внески приймає Качка {{{WILLOW_FEES}}}.")
    html, calls, prompt = await ask(conversation, WILLOW_Q, WILLOW_ROUTE, observed)
    body, sources = split(html)
    assert "Голова ОСМД — Качка" in prompt and "Скарбник ОСМД — Жабка" in prompt     # facts were retrieved
    assert "Головою ОСМД рукавички «Під вербою» є Качка, а скарбником — Жабка" in body
    assert "Узимку, поки Жабка спить у мулі, внески приймає Качка" in body
    assert WILLOW_CHARTER in sources and WILLOW_FEES in sources
    assert len(calls) == 1                                    # nothing to correct: no extra model call


def test_role_nouns_at_sentence_start_are_not_possessive_ownership(archive, store):
    from forest_bot.retrieval import EvidenceDoc
    docs = [EvidenceDoc(d, [], {}, 1.0) for d in store.active_documents() if d.title in (WILLOW_CHARTER,)]
    for sentence in ("Головою ОСМД є Качка [1].", "Товщина шару — три лапи [1].", "Скарбником є Жабка [1]."):
        assert [k for k, _, _ in grounding_issues(sentence, docs, False)] == [], sentence


# ------------------------------------------------------------------ measurement meaning, primary source

PINES_Q = "Коли й чим утеплювати рукавичку «У соснах» і якої товщини має бути утеплення?"
PINES_ROUTE = route("u_sosnakh", "утеплення рукавички коли чим товщина шар", ["утеплення", "товщина", "шар"])
EXPECTED_PINES = (f"У жовтні, мохом і сухою хвоєю, шаром завтовшки у три лапи {{{CONIFER_INSULATION}}}.")


async def test_correct_insulation_answer_is_kept_with_its_primary_source(conversation, archive):
    html, calls, prompt = await ask(conversation, PINES_Q, PINES_ROUTE, EXPECTED_PINES)
    body, sources = split(html)
    assert "шаром завтовшки у три лапи" in body and "жовтні" in body
    assert CONIFER_INSULATION in sources and CALENDAR not in sources
    assert len(calls) == 1


async def test_altered_measure_and_denied_thickness_are_corrected_to_the_source_measure(conversation, archive):
    # Observed (gpt-oss-20b fallback): «шаром у три лапи» became «три шари», thickness was declared
    # unspecified, and the only sentence citing the insulation document was later removed.
    observed = (f"Утеплюйте рукавичку в жовтні {{{CONIFER_INSULATION}}}. Утеплюють мохом і сухою хвоєю "
                f"{{{CONIFER_INSULATION}}}. Кладіть утеплення у три шари {{{CONIFER_INSULATION}}}. "
                f"Товщина утеплення не уточнюється в документах; лише зазначено, що слід класти мох і суху хвою "
                f"шаром у три лапи {{{CONIFER_INSULATION}}}.")
    html, calls, _ = await ask(conversation, PINES_Q, PINES_ROUTE, observed)        # correction rate-limited
    body, sources = split(html)
    assert len(calls) == 2                                                     # one correction attempt
    assert "три шари" not in body and "не уточнюється" not in body
    assert "шаром у три лапи" in body and "жовтні" in body and "мохом і сухою хвоєю" in body
    assert CONIFER_INSULATION in sources


async def test_broader_month_is_replaced_by_the_more_specific_source(conversation, archive):
    observed = (f"Рукавички утеплюють у листопаді {{{CALENDAR}}}. Утеплюють мохом і сухою хвоєю шаром у три лапи "
                f"{{{CONIFER_INSULATION}}}.")
    html, calls, prompt = await ask(conversation, PINES_Q, PINES_ROUTE, observed)
    body, sources = split(html)
    titles = evidence_titles(prompt)
    i, j = titles.index(CONIFER_INSULATION) + 1, titles.index(CALENDAR) + 1
    assert f"[{i}] і [{j}] говорять про той самий предмет" in prompt                # conflict made explicit
    assert "листопаді" not in body and "Утеплюйте рукавичку в жовтні" in body
    assert CONIFER_INSULATION in sources


# ------------------------------------------------------------------ specificity explanation

DAM_Q = "Я біля бобрової греблі. Гребля відкрита й вода вже спала. Можна перейти вбрід?"
DAM_ROUTE = route("bilia_hrebli", "перейти вбрід гребля відкрита вода спала", ["вбрід", "брід", "гребля"])
GOOD_DAM = (f"Ні. Біля вашої греблі вбрід не ходити ніколи, навіть коли гребля відкрита {{{DAM_PATHS}}}. "
            f"Пам'ятка для мешканців біля річки вважає брід вище греблі безпечним лише за умов відкритої греблі "
            f"й спалої води {{{FORDS}}}, але для вашої рукавички діє місцеве правило: воно має пріоритет над "
            f"порадою для всієї частини лісу {{{DAM_PATHS}}}.")


async def test_local_prohibition_vs_regional_permission_is_explained_by_specificity(conversation, archive):
    html, calls, prompt = await ask(conversation, DAM_Q, DAM_ROUTE, GOOD_DAM)
    body, sources = split(html)
    titles = evidence_titles(prompt)
    i, j = titles.index(DAM_PATHS) + 1, titles.index(FORDS) + 1
    assert f"[{i}] і [{j}] говорять про той самий предмет" in prompt
    assert "пріоритет конкретнішого місця" in prompt
    # «лише за умов» restates «тільки тоді, коли»: no qualifier issue, no correction call (the observed
    # false positive sent the answer to a fallback model that invented the distinction).
    assert len(calls) == 1
    assert "вбрід не ходити ніколи" in body and "має пріоритет" in body and DAM_PATHS in sources


async def test_fabricated_distinction_between_same_words_is_removed(conversation, archive):
    observed = (f"Ні. Біля вашої греблі вбрід не ходити ніколи, навіть коли гребля відкрита {{{DAM_PATHS}}}. "
                f"Дозвіл у пам'ятці біля річки стосується саме бріду вище греблі, а не переходу вбрід біля "
                f"вашої рукавички {{{FORDS}}}.")
    html, calls, _ = await ask(conversation, DAM_Q, DAM_ROUTE, observed)         # correction rate-limited
    body, sources = split(html)
    assert len(calls) == 2
    assert "а не переходу вбрід" not in body
    assert "вбрід не ходити ніколи, навіть коли гребля відкрита" in body and DAM_PATHS in sources


# ------------------------------------------------------------------ answers that were correct stay correct

async def test_previously_correct_answers_pass_unchanged(conversation, archive):
    cases = [
        ("Біля верби каменів із води не видно. Чи можна перейти річку по камінню?",
         route("pid_verboyu", "перейти річку по камінню біля верби", ["камені", "перейти"]),
         f"Ні. Біля верби річку можна перейти по камінню, лише якщо камені видно з води; не видно каменів — тут "
         f"не переходьте {{{FORDS}}}."),
        ("Коли Їжак на узліссі лягає в сплячку і хто платить за нього внески?",
         route("na_uzlissi", "Їжак сплячка внески хто платить", ["їжак", "сплячка", "внески"]),
         "Їжак з листопада в сплячці; поки він спить, внески за нього платить Кріт "
         "{Членські внески рукавички На узліссі}."),
        ("Де біля рукавички «У ліщині» знайти мед і до якого часу можна збирати малину?",
         route("u_lishchyni", "мед малина де знайти до якого часу", ["мед", "малина"]),
         "Мед є в дуплі старої липи за 80 кроків на північ, там живуть дикі бджоли. Малина росте на вирубці за "
         "ярком; її можна знайти до середини серпня {Чим поживитись поруч із рукавичкою У ліщині}."),
    ]
    for question, r, answer in cases:
        conversation.llm.calls.clear()
        html, calls, _ = await ask(conversation, question, r, answer)
        body, _ = split(html)
        assert len(calls) == 1, question                         # no grounding issue, no correction
        expected = answer.split("{")[0].strip()[:40]
        assert expected in body, (question, body)


def test_wording_recommended_by_the_prompt_passes_the_answer_checks(archive, store):
    import re
    from forest_bot.qa import ANSWER_SYSTEM
    from forest_bot.retrieval import EvidenceDoc
    docs = [EvidenceDoc(d, [], {}, 1.0) for d in store.active_documents() if d.title in (DAM_PATHS, FORDS)]
    rule = next(line for line in ANSWER_SYSTEM.splitlines() if line.startswith("- Пояснюючи"))
    example = re.search(r"«(.+?)»", rule).group(1)
    assert grounding_issues(example[0].upper() + example[1:] + " [1].", docs, False) == []


# ------------------------------------------------------------------ retest: ford answer reduced to a fragment

# Reconstructed from the production log of the retest (full text was not recorded): gpt-oss-120b's first
# answer cited with «【n】». The number check read those as unsupported numbers 1, 4, 3, the correction was
# rate-limited, and the fallback deleted the three sentences, leaving «Тому, …» as the whole answer.
# (In production the guests' rule was cited from «Правила співжиття…», which offline keyword-only
# retrieval does not fetch; the local «Стежки…» document states the same alternative.)
OBSERVED_DAM_RETEST = (
    "У вашій рукавичці «Біля бобрової греблі» діє локальне правило, яке забороняє переходити річку вбрід, навіть "
    "коли гребля відкрита<<" + DAM_PATHS + ">>. Це правило має пріоритет над загальним правилом частини лісу, яке "
    "дозволяє переходити брід лише за умови відкритої греблі та спалої води<<" + FORDS + ">>. Крім того, для гостей "
    "встановлено правило переходити лише по греблі<<" + DAM_PATHS + ">>, що також підтверджує заборону вбрід. "
    "Тому, незважаючи на те, що умови згідно з {" + FORDS + "} виконані, для вашого місця заборона з {" + DAM_PATHS
    + "} залишається діючою. Отже, перейти вбрід біля бобрової греблі не можна.")


def fullwidth(template):
    """Template with «<<Title>>» rendered as the model's «【n】» citation form."""
    import re

    def answer_fn(messages):
        titles = evidence_titles(messages[1]["content"])
        if "Знайдено проблеми" in messages[-1]["content"]:
            raise LLMRateLimited(30)
        text = re.sub(r"<<(.+?)>>", lambda m: f"【{titles.index(m.group(1)) + 1}】", template)
        for n, t in enumerate(titles, start=1):
            text = text.replace("{" + t + "}", f"[{n}]")
        return {"status": "answered", "answer": text}

    return answer_fn


async def test_fullwidth_citations_do_not_count_as_numbers_and_the_answer_stays_whole(conversation, archive):
    conversation.llm.route = DAM_ROUTE
    conversation.llm.answer_fn = fullwidth(OBSERVED_DAM_RETEST)
    body, sources = split((await conversation.on_text(1, DAM_Q))[-1].text)
    assert body.startswith("У вашій рукавичці «Біля бобрової греблі» діє локальне правило")   # not «Тому, …»
    assert "забороняє переходити річку вбрід" in body and "по греблі" in body and "пріоритет" in body
    assert "【" not in body and DAM_PATHS in sources
    assert len(conversation.llm.answer_prompts) == 1                         # nothing to correct


async def test_expected_self_contained_dam_answer_is_delivered_unchanged(conversation, archive):
    expected = (f"Ні. Біля бобрової греблі вбрід не ходять навіть за відкритої греблі — переходити потрібно тільки "
                f"по греблі {{{DAM_PATHS}}}. Місцева заборона має пріоритет над регіональною порадою {{{DAM_PATHS}}}.")
    html, calls, _ = await ask(conversation, DAM_Q, DAM_ROUTE, expected)
    body, sources = split(html)
    assert len(calls) == 1
    assert body.startswith("Ні. Біля бобрової греблі вбрід не ходять навіть за відкритої греблі — переходити "
                           "потрібно тільки по греблі")
    assert "Місцева заборона має пріоритет над регіональною порадою" in body and DAM_PATHS in sources


def test_fallback_does_not_leave_a_dangling_connective(archive, store):
    from forest_bot.qa import apply_grounding_fallback
    from forest_bot.retrieval import EvidenceDoc
    docs = [EvidenceDoc(d, [], {}, 1.0) for d in store.active_documents() if d.title == DAM_PATHS]
    text = ("Від рукавички до броду 7 кроків [1]. Тому вбрід біля греблі не ходять ніколи [1]. "
            "На той берег — тільки по греблі [1].")
    out = apply_grounding_fallback(text, docs, comparison=False)
    assert out == "Вбрід біля греблі не ходять ніколи [1]. На той берег — тільки по греблі [1]."


async def test_router_season_without_a_season_in_the_message_is_ignored(conversation, archive):
    conversation.llm.route = {**DAM_ROUTE, "context": {"season": "зима"}}     # observed: «вода вже спала»
    conversation.llm.answer_fn, _ = model(GOOD_DAM)
    await conversation.on_text(1, DAM_Q)
    prompt = conversation.llm.answer_prompts[-1]
    assert not _winter_in_prompt(prompt) and "зима" not in prompt.split("<document>")[0]
    assert (conversation.store.get_state(1).get("context") or {}).get("season") is None


def _winter_in_prompt(prompt):
    """The established circumstances line precedes the documents in the answer prompt."""
    head = prompt.split("<document>")[0]
    return "Обставини користувача" in head and "зима" in head


async def test_inherited_question_season_survives_a_follow_up_without_season_words(conversation, archive):
    conversation.llm.answer_fn, _ = model("Узимку внески вдвічі менші {Членські внески рукавички У соснах}.")
    conversation.llm.route = {**route("u_sosnakh", "внески узимку", ["внески"]), "context": {"season": "зима"}}
    await conversation.on_text(7, "Скільки платити у соснах узимку?")
    assert conversation.store.get_state(7)["context"].get("season") == "зима"
    # The router repeats the season for the follow-up; the message has no season word, so the router's
    # value is dropped, but the season inherited from the conversation stays.
    conversation.llm.route = {**route("pid_yalynoyu", "внески узимку", ["внески"]), "follow_up": True,
                              "context": {"season": "зима"}}
    await conversation.on_text(7, "А під ялиною?")
    assert conversation.store.get_state(7)["context"].get("season") == "зима"
    assert _winter_in_prompt(conversation.llm.answer_prompts[-1])


async def test_explicitly_established_current_season_is_kept_for_later_questions(conversation, archive):
    conversation.llm.answer_fn, _ = model("Узимку внески вдвічі менші {Членські внески рукавички У соснах}.")
    conversation.llm.route = {"intent": "statement", "context": {"season": "зима"}}
    await conversation.on_text(8, "Зараз зима.")
    assert conversation.store.get_state(8)["context"] == {"season": "зима", "season_scope": "current"}
    conversation.llm.route = {**route("u_sosnakh", "внески", ["внески"]), "context": {"season": "зима"}}
    await conversation.on_text(8, "Скільки платити у соснах?")                    # new question, no season word
    assert conversation.store.get_state(8)["context"].get("season") == "зима"
    assert _winter_in_prompt(conversation.llm.answer_prompts[-1])


# ------------------------------------------------------------------ batch of 18 Oct (evening)

OAK_FEES = "Членські внески рукавички Під дубом"
FALSE_PREMISE_Q = "Під дубом внесок 40 жолудів на місяць, правильно? Просто підтвердь."
OAK_ROUTE = route("pid_dubom", "членський внесок розмір", ["внесок", "жолуді"])


async def test_false_premise_correction_is_kept_whole(conversation, archive):
    # Observed final answer: «Це зазначено в документі … [1].» — the sentence correcting 40 was deleted as an
    # unsupported number (time budget exhausted, no correction call), leaving an apparent confirmation.
    observed = (f"Ні, внесок під дубом — 4 жолуді з дорослого звіра на місяць, а не 40 {{{OAK_FEES}}}. "
                f"Це зазначено в документі «{OAK_FEES}» {{{OAK_FEES}}}.")
    html, calls, _ = await ask(conversation, FALSE_PREMISE_Q, OAK_ROUTE, observed)
    body, sources = split(html)
    assert body.startswith("Ні, внесок під дубом — 4 жолуді з дорослого звіра на місяць, а не 40")
    assert OAK_FEES in sources and len(calls) == 1


async def test_confirmed_false_premise_never_leaves_an_affirmative_fragment(conversation, archive):
    observed = (f"Так, під дубом внесок 40 жолудів на місяць {{{OAK_FEES}}}. "
                f"Це зазначено в документі «{OAK_FEES}» {{{OAK_FEES}}}.")
    html, _, _ = await ask(conversation, FALSE_PREMISE_Q, OAK_ROUTE, observed)     # correction rate-limited
    body, _ = split(html)
    assert "40" not in body and "Це зазначено" not in body and not body.lstrip().startswith("Так")


async def test_an_invented_number_is_still_rejected(conversation, archive):
    html, _, _ = await ask(conversation, "Скільки внесок під дубом?", OAK_ROUTE,
                           f"Внесок під дубом — 40 жолудів на місяць {{{OAK_FEES}}}.")
    assert "40" not in split(html)[0]


HAZEL_FEES = "Членські внески рукавички У ліщині"
DECIDUOUS_INSULATION = "Як утеплити рукавичку в листяному лісі"


async def test_fee_exemption_is_not_attributed_to_the_insulation_document(conversation, archive):
    # Observed: «[1]» corrected to «[1][2]» because «потрібно» and «тих» also occur in the insulation text.
    answer = (f"Так, у квітні внесок потрібно сплачувати. Правило про звільнення від сплати для тих, хто спить "
              f"узимку, діє лише з листопада по березень {{{HAZEL_FEES}}}.")
    html, _, prompt = await ask(conversation, "Я дорослий мешканець «У ліщині» й сплю взимку. А у квітні треба платити?",
                                route("u_lishchyni", "членський внесок сплячка звільнення", ["внесок", "спить"]), answer)
    body, sources = split(html)
    assert HAZEL_FEES in sources and DECIDUOUS_INSULATION not in sources
    assert re.search(r"по березень \[\d\]\.$", body.strip())


async def test_half_contribution_with_explicit_value_is_kept(conversation, archive):
    answer = ("Дятел платить половину внеску, бо лікує ялину від шкідників, тобто 1,5 шишки на місяць "
              "{Членські внески рукавички Під ялиною}.")
    html, calls, _ = await ask(conversation, "Я Дятел із рукавички «Під ялиною». Який у мене місячний внесок?",
                               route("pid_yalynoyu", "членський внесок дятел", ["внесок", "дятел"]), answer)
    assert "1,5 шишки" in split(html)[0] and len(calls) == 1


async def test_other_regions_advice_is_inapplicable_not_lower_priority(conversation, archive):
    q = "Біля бобрової греблі хочу утеплити рукавичку мохом, як радять для хвойного лісу. Підійде?"
    answer = ("Ні. Біля річки мох не годиться, бо біля води мох гниє; утеплюйте очеретом "
              "{Як утеплити рукавичку біля річки}. «Біля бобрової греблі» не належить до хвойного лісу, тож порада "
              "утеплювати мохом для неї не застосовується {Як утеплити рукавичку біля річки}.")
    conversation.llm.route = {"intent": "question", "needs_location": False, "search_query": "утеплення мох",
                              "keywords": ["утеплення", "мох"]}                # places come from the message
    conversation.llm.answer_fn, calls = model(answer)
    html = (await conversation.on_text(1, q))[-1].text
    prompt = conversation.llm.answer_prompts[0]
    # Since the routing fix the coniferous forest is a cited source, not a compared place.
    assert "Користувач посилається на пораду для частини лісу «Хвойний ліс»" in prompt
    assert "не порівнюй її з порадами для запитаного місця за пріоритетом" in prompt
    body, _ = split(html)
    assert "не належить до хвойного лісу" in body and "очеретом" in body and len(calls) == 1


def test_a_bare_yes_or_no_is_never_left_after_removals(archive, store):
    from forest_bot.qa import NO_RELIABLE_ANSWER, apply_grounding_fallback
    from forest_bot.retrieval import EvidenceDoc
    docs = [EvidenceDoc(d, [], {}, 1.0) for d in store.active_documents() if d.title == OAK_FEES]
    out = apply_grounding_fallback("Ні. Під дубом внесок — 40 жолудів на місяць [1].", docs, False,
                                   question="Скільки внесок під дубом?")
    assert out == NO_RELIABLE_ANSWER                     # not «Ні.»
