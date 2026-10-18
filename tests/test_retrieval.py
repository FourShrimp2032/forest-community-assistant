"""Geography-aware retrieval over the real archive texts (keyword search; no embedding model)."""

from forest_bot import geography as geo
from forest_bot.retrieval import QueryScope, Retriever


def titles(result):
    return [d.document.title for d in result.docs]


def retrieve(store, library, query, scope, **kw):
    return Retriever(store, None, max_docs=8, per_target_docs=4, evidence_chars=9000, **kw).retrieve(
        query, scope, library.revision)


async def test_membership_fee_under_oak(archive, store, library):
    r = retrieve(store, library, "Скільки внесок під дубом і до якого числа?", QueryScope("mittens", ["pid_dubom"]))
    assert titles(r)[0] == "Членські внески рукавички Під дубом"
    assert "4 жолуді" in r.docs[0].text and "2 лісові горіхи" in r.docs[0].text


async def test_hunters_near_dam_finds_forest_wide_advice(archive, store, library):
    r = retrieve(store, library, "Біля греблі мисливці! Як кричати?", QueryScope("mittens", ["bilia_hrebli"]))
    assert "Як кричати, коли бачиш мисливців" in titles(r)
    doc = next(d for d in r.docs if d.document.title == "Як кричати, коли бачиш мисливців")
    assert doc.levels == {"bilia_hrebli": geo.LEVEL_FOREST}


async def test_mushrooms_near_willow_combines_local_and_both_regions(archive, store, library):
    # Router keywords (synonyms) are appended to the search query in production.
    r = retrieve(store, library, "Де шукати гриби? гриби опеньки білі гриби підберезники",
                 QueryScope("mittens", ["pid_verboyu"]))
    t = titles(r)
    assert "Чим поживитись поруч із рукавичкою Під вербою" in t
    assert "Грибний путівник листяного лісу" in t
    assert "Грибний путівник біля річки" in t
    assert "Грибний путівник хвойного лісу" not in t           # no leakage from an unrelated region
    levels = {d.document.title: d.levels["pid_verboyu"] for d in r.docs}
    assert levels["Чим поживитись поруч із рукавичкою Під вербою"] == geo.LEVEL_MITTEN
    assert levels["Грибний путівник листяного лісу"] == levels["Грибний путівник біля річки"] == geo.LEVEL_REGION
    # Local advice is presented first.
    assert t[0] == "Чим поживитись поруч із рукавичкою Під вербою"


async def test_insulation_on_forest_edge_uses_only_forest_wide_advice(archive, store, library):
    r = retrieve(store, library, "Коли утеплювати рукавичку на узліссі?", QueryScope("mittens", ["na_uzlissi"]))
    t = titles(r)
    assert "Календар сезонів" in t
    for regional in ("Як утеплити рукавичку в хвойному лісі", "Як утеплити рукавичку в листяному лісі",
                     "Як утеплити рукавичку біля річки"):
        assert regional not in t
    assert r.coverage["na_uzlissi"] == {geo.LEVEL_MITTEN, geo.LEVEL_FOREST}    # no regional level exists


async def test_no_region_leakage_for_any_mitten(archive, store, library):
    for mitten in geo.MITTENS:
        r = retrieve(store, library, "Як утеплити рукавичку? утеплення мох листя очерет",
                     QueryScope("mittens", [mitten]))
        for d in r.docs:
            assert geo.applicability(d.scope, mitten) > 0, (mitten, d.document.title, d.scope)


async def test_ford_passage_for_willow_does_not_reach_the_dam(archive, store, library):
    q = "Як перейти річку? брід переправа перейти гребля"
    dam = retrieve(store, library, q, QueryScope("mittens", ["bilia_hrebli"]))
    willow = retrieve(store, library, q, QueryScope("mittens", ["pid_verboyu"]))
    dam_text = "\n".join(d.text for d in dam.docs)
    willow_text = "\n".join(d.text for d in willow.docs)
    assert "вбрід не ходити ніколи" in dam_text             # mitten rule for the dam
    assert "Брід вище греблі" in dam_text                   # broader riverside rule, conflicting
    assert "по камінню" not in dam_text                     # willow-only passage
    assert "по камінню" in willow_text


async def test_comparison_of_all_mittens_covers_each_mitten(archive, store, library):
    r = retrieve(store, library, "Порівняй членські внески внесок", QueryScope("forest", comparison=True))
    fee_docs = {d.document.title for d in r.docs if d.document.title.startswith("Членські внески")}
    assert len(fee_docs) == 8


async def test_two_mitten_comparison(archive, store, library):
    r = retrieve(store, library, "внесок", QueryScope("mittens", ["pid_dubom", "u_lishchyni"], comparison=True))
    t = titles(r)
    assert "Членські внески рукавички Під дубом" in t and "Членські внески рукавички У ліщині" in t
    assert not any("Біля мурашника" in x for x in t)


async def test_regional_question_includes_region_and_local_differences(archive, store, library):
    r = retrieve(store, library, "Що їсти? пожива їжа", QueryScope("regions", regions=["richka"]))
    t = titles(r)
    assert "Що їсти біля річки" in t
    assert not any("листяному" in x or "хвойному" in x for x in t)
    for d in r.docs:
        assert geo.applicability_for_region(d.scope, "richka") > 0


async def test_general_question_without_place(archive, store, library):
    r = retrieve(store, library, "Що робити при пожежі?", QueryScope("general"))
    assert titles(r)[0] == "Що робити при пожежі"


async def test_evidence_budget_is_respected(archive, store, library):
    r = Retriever(store, None, evidence_chars=1500).retrieve("внесок", QueryScope("forest", comparison=True),
                                                            library.revision)
    assert sum(len(d.text) for d in r.docs) <= 1500 or len(r.docs) == 1


async def test_specific_document_has_priority_while_regional_one_stays_active_elsewhere(library, store):
    from .test_versioning import tg_doc
    await tg_doc(library, 1, "Тестовий ліхтар.txt",
                 "Тестовий ліхтар\nЦя порада для мешканців листяного лісу. Запасний ліхтар зберігається в зеленій "
                 "шафі біля дверей.", "2026-10-18T09:44:00Z")
    await tg_doc(library, 1, "Ліхтар під вербою.txt",
                 "Ліхтар під вербою\nПорада для рукавички «Під вербою». Запасний ліхтар зберігається в жовтій "
                 "скрині на ґанку.", "2026-10-18T09:45:00Z")
    willow = retrieve(store, library, "Де запасний ліхтар?", QueryScope("mittens", ["pid_verboyu"]))
    assert [(d.document.title, d.levels["pid_verboyu"]) for d in willow.docs][:2] == [
        ("Ліхтар під вербою", geo.LEVEL_MITTEN), ("Тестовий ліхтар", geo.LEVEL_REGION)]
    oak = retrieve(store, library, "Де запасний ліхтар?", QueryScope("mittens", ["pid_dubom"]))
    assert [d.document.title for d in oak.docs] == ["Тестовий ліхтар"]          # regional advice applies here
    edge = retrieve(store, library, "Де запасний ліхтар?", QueryScope("mittens", ["na_uzlissi"]))
    assert edge.docs == []                                                          # and not outside the region


async def _with_lamps(library):
    from .test_versioning import tg_doc
    await tg_doc(library, 1, "Тестовий ліхтар.txt",
                 "Тестовий ліхтар\nЦя порада для мешканців листяного лісу. Запасний ліхтар зберігається в зеленій "
                 "шафі біля дверей.", "2026-10-18T09:44:00Z")
    await tg_doc(library, 1, "Ліхтар під вербою.txt",
                 "Ліхтар під вербою\nПорада для рукавички «Під вербою». Запасний ліхтар зберігається в жовтій "
                 "скрині на ґанку.", "2026-10-18T09:45:00Z")


async def test_same_subject_pair_is_flagged_only_for_potential_conflicts(archive, store, library):
    from forest_bot.qa import build_answer_messages, same_subject_pairs
    from .conftest import KYIV
    await _with_lamps(library)
    lamp = retrieve(store, library, "Де запасний ліхтар?", QueryScope("mittens", ["pid_verboyu"]))
    pairs = same_subject_pairs(lamp)
    titles = [d.document.title for d in lamp.docs]
    assert [(titles[i - 1], titles[j - 1]) for i, j, _ in pairs] == [("Ліхтар під вербою", "Тестовий ліхтар")]
    prompt = build_answer_messages("Де запасний ліхтар під вербою?", lamp, [], KYIV)[1]["content"]
    assert "[1] і [2] говорять про той самий предмет" in prompt and "[2] тут не застосовується" in prompt
    # Complementary advice (customer's mushroom example) is not paired, so nothing is suppressed.
    mushrooms = retrieve(store, library, "Де шукати гриби? опеньки білі гриби", QueryScope("mittens", ["pid_verboyu"]))
    assert same_subject_pairs(mushrooms) == []
    assert {"Грибний путівник листяного лісу", "Грибний путівник біля річки"} <= {d.document.title for d in mushrooms.docs}


def test_priority_rule_suppresses_only_the_conflicting_point():
    from forest_bot.qa import ANSWER_SYSTEM
    assert "щодо ЦЬОГО пункту відповідай лише конкретнішою" in ANSWER_SYSTEM
    assert "доповнювальні поради частини лісу чи всього лісу" in ANSWER_SYSTEM
    assert "Доповнювальні поради з різних рівнів об'єднуй" in ANSWER_SYSTEM


def test_season_remark_from_the_observed_flashlight_answer_is_removed():
    from types import SimpleNamespace
    from forest_bot.qa import deterministic_repairs
    docs = [SimpleNamespace(text="Порада для рукавички «Під вербою». Запасний ліхтар зберігається в жовтій скрині на "
                                 "ґанку.", document=None),
            SimpleNamespace(text="Запасний ліхтар зберігається в зеленій шафі біля дверей.", document=None)]
    observed = ("Для рукавички «Під вербою» запасний ліхтар зберігається в жовтій скрині на ґанку [1].\n"
                "Тому головним місцем зберігання запасного ліхтаря є жовта скриня на ґанку. "
                "(Сезон — зима, у документах немає сезонних змін щодо цього питання.)")
    out = deterministic_repairs(observed, docs, {"season": "зима", "season_scope": "question"})
    assert "Сезон" not in out and "жовтій скрині на ґанку [1]" in out
