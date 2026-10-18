import shutil

import pytest

from forest_bot.extraction import (ExtractionError, OcrSettings, _numeric_value, _vote_numbers, extract_text,
                                   first_heading, reflow_pdf_text)

from .conftest import FIXTURES

FILES = FIXTURES / "files"
HAS_TESSERACT = shutil.which("tesseract") is not None


def test_txt():
    r = extract_text((FILES / "Членські внески рукавички Під дубом.txt").read_bytes(), "x.txt")
    assert r.method == "text"
    assert "4 жолуді з дорослого звіра на місяць" in r.text


def test_txt_cp1251():
    r = extract_text("Внесок — 4 жолуді з дорослого звіра на місяць.".encode("cp1251"), "a.txt")
    assert "жолуді" in r.text


def test_docx():
    r = extract_text((FILES / "Чим поживитись поруч із рукавичкою У ліщині.docx").read_bytes(), "a.docx")
    assert r.method == "docx"
    assert "Мед є в дуплі старої липи" in r.text


def test_pdf_with_text_layer_is_reflowed():
    r = extract_text((FILES / "Як кричати, коли бачиш мисливців.pdf").read_bytes(), "a.pdf")
    assert r.method == "pdf-text"
    assert "Крикни тричі коротко, як Сорока." in r.text
    assert "Не біжи на відкрите. Ховайся в гущавину." in r.text


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed (runs inside Docker)")
def test_scanned_pdf_ocr_reads_numbers_correctly():
    r = extract_text((FILES / "Членські внески рукавички У ліщині.pdf").read_bytes(), "a.pdf")
    assert r.method == "pdf-ocr"
    assert "6 горіхів з дорослого звіра на місяць" in r.text
    assert "до 5 числа" in r.text          # texture next to the digit used to produce "95"
    assert "Вивірці в дупло граба" in r.text


def test_scanned_pdf_without_ocr_fails_cleanly():
    with pytest.raises(ExtractionError) as e:
        extract_text((FILES / "Членські внески рукавички У ліщині.pdf").read_bytes(), "a.pdf",
                     OcrSettings(enabled=False))
    assert "OCR" in e.value.reason


@pytest.mark.parametrize("data,name,fragment", [
    (b"", "a.txt", "порожній"),
    (b"\x00\x01binary", "a.txt", "двійкові"),
    (b"not a zip", "a.docx", "DOCX"),
    (b"%PDF-broken", "a.pdf", "PDF"),
    (b"hello", "a.doc", "не підтримується"),
    (b"  \n  ", "a.txt", "тексту"),
])
def test_bad_inputs(data, name, fragment):
    with pytest.raises(ExtractionError) as e:
        extract_text(data, name)
    assert fragment in e.value.reason


def test_reflow_joins_wrapped_lines_and_bullets():
    raw = ("Правила співжиття в рукавичці «Біля\n бобрової греблі»\nЩоб мешканцям рукавички жилося мирно, ми\n"
           "домовилися про такі правила.\nКоротко:\n–\nтиша з півночі до світанку;")
    out = reflow_pdf_text(raw)
    assert "«Біля бобрової греблі»" in out
    assert "мирно, ми домовилися" in out
    assert "– тиша з півночі до світанку;" in out


def test_first_heading():
    assert first_heading("Членські внески\n\nТекст.") == "Членські внески"
    assert first_heading("Це речення.\nІнше") is None


def _ocr_dict(words, confs, boxes):
    n = len(words)
    return {"text": words, "conf": confs, "left": [b[0] for b in boxes], "top": [b[1] for b in boxes],
            "width": [b[2] for b in boxes], "height": [b[3] for b in boxes],
            "block_num": [1] * n, "par_num": [1] * n, "line_num": [1] * n}


def test_number_vote_uses_majority_across_variants():
    boxes = [(0, 0, 50, 20), (60, 0, 20, 20), (90, 0, 60, 20)]
    main = _ocr_dict(["до", "95", "числа"], [96, 67, 96], boxes)
    a = _ocr_dict(["до", "5", "числа"], [96, 96, 96], boxes)
    b = _ocr_dict(["до", "5", "числа"], [96, 91, 96], boxes)
    c = _ocr_dict(["до", "9", "числа"], [96, 93, 96], boxes)
    notes = _vote_numbers(main, [main, a, b, c])
    assert main["text"][1] == "5"
    assert notes  # disagreement is recorded for provenance


def test_cyrillic_ze_as_digit_only_mid_sentence():
    assert _numeric_value("З") == "3"
    boxes = [(0, 0, 50, 20), (60, 0, 20, 20), (90, 0, 60, 20)]
    start = _ocr_dict(["З", "ранку", "вартує"], [90, 90, 90], boxes)
    _vote_numbers(start, [start])
    assert start["text"][0] == "З"
    mid = _ocr_dict(["становить", "З", "шишки"], [90, 70, 90], boxes)
    _vote_numbers(mid, [mid])
    assert mid["text"][1] == "3"
