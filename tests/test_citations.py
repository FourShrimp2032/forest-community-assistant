"""Every factual clause must be backed by the document(s) it cites."""

import json
from types import SimpleNamespace as N

from forest_bot.qa import render_answer, verify_citations
from forest_bot.retrieval import QueryScope, RetrievalResult

from .conftest import FIXTURES, KYIV

LOCAL = "Порада для рукавички «Під вербою». Запасний ліхтар зберігається в жовтій скрині на ґанку."
REGIONAL = "Запасний ліхтар зберігається в зеленій шафі біля дверей."


def lamp_docs():
    return [N(text=LOCAL, document=N(title="Ліхтар під вербою")),
            N(text=REGIONAL, document=N(title="Тестовий ліхтар"))]


def test_regional_clause_cannot_keep_the_local_documents_citation():
    """Observed: the green-cupboard clause cited [1] «Ліхтар під вербою»."""
    out = verify_citations("Порада листяного лісу про зелену шафу біля дверей для «Під вербою» не діє [1].",
                           lamp_docs())
    assert out.endswith("не діє [2].")


def test_sentence_combining_facts_from_two_documents_cites_both():
    out = verify_citations("У листяному лісі ліхтар зберігається в зеленій шафі біля дверей, але під вербою — у "
                           "жовтій скрині на ґанку [1].", lamp_docs())
    assert out.endswith("[1][2].")


def test_concise_correct_answer_keeps_a_single_source():
    text = "Під вербою запасний ліхтар зберігається в жовтій скрині на ґанку [1]."
    assert verify_citations(text, lamp_docs()) == text


def test_rendered_sources_follow_the_corrected_citations(store):
    from types import SimpleNamespace
    docs = []
    for i, (title, text) in enumerate((("Ліхтар під вербою", LOCAL), ("Тестовий ліхтар", REGIONAL)), start=1):
        doc = SimpleNamespace(id=i, title=title, source="telegram", uploader_name="Аліна", tg_file_id=f"f{i}",
                              scope=["pid_verboyu"] if i == 1 else ["lystianyi"], drive_link=None, drive_file_id=None,
                              version_dt=__import__("datetime").datetime(2026, 10, 18, 9, 45,
                                                                         tzinfo=__import__("datetime").timezone.utc))
        docs.append(N(text=text, document=doc, scope=doc.scope, levels={"pid_verboyu": 3 if i == 1 else 2}))
    result = RetrievalResult(QueryScope("mittens", ["pid_verboyu"]), docs, {})
    observed = ("Для рукавички «Під вербою» запасний ліхтар зберігається в жовтій скрині на ґанку [1].\n"
                "Порада листяного лісу про зелену шафу біля дверей тут не діє [1].")
    html = render_answer({"status": "answered", "answer": observed}, result, KYIV).html
    assert "тут не діє [2]" in html
    assert "«Ліхтар під вербою»" in html and "«Тестовий ліхтар»" in html


def test_real_answers_from_live_runs_are_not_changed():
    d = {x["filename"].rsplit(".", 1)[0]: x["text"]
         for x in json.loads((FIXTURES / "archive_texts.json").read_text("utf-8"))}

    def docs(*titles):
        return [N(text=d[t], document=N(title=t)) for t in titles]

    cases = [
        (docs("Чим поживитись поруч із рукавичкою Під вербою", "Грибний путівник біля річки",
              "Грибний путівник листяного лісу", "Їстівне й отруйне"),
         "• Опеньки ростуть безпосередньо на корінні верби, біля вашої рукавички [1].\n"
         "• Біля річки опеньки ростуть на старих вербах і пеньках біля води [2].\n"
         "• Біля річки опеньки йдуть уже наступного дня після невеликого дощу [2], а в листяному лісі — на 4–5 "
         "день після дощу [3].\n• Гриби шукайте після дощу, коли земля волога, але не хлюпає [4]."),
        (docs("Членські внески рукавички У соснах", "Членські внески рукавички Під дубом"),
         "• Розмір: 2 соснові шишки з дорослого звіра щотижня [1].\n• Зайчик може платити морквою з поля "
         "(1 морквина за 2 шишки) [1].\n• Пільга: узимку внески вдвічі менші [1].\n• Розмір: 4 жолуді з дорослого "
         "звіра на місяць, до 10 числа [2].\n• Пташенята й поросята до року не платять [2].\nВизначити, де дешевше, "
         "неможливо: соснові шишки проти жолудів, щотижня проти щомісяця [1][2]."),
        (docs("Як кричати, коли бачиш мисливців"),
         "Крикни тричі коротко, як Сорока [1]. Почув постріл — не біжи на відкрите, ховайся в гущавину [1]."),
    ]
    for evidence, text in cases:
        assert verify_citations(text, evidence) == text
