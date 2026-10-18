"""Telegram rendering of answers: the exact payload sent with parse_mode=HTML and what Telegram displays.

Presentation only: entities written by the model are decoded once, bold (Markdown or <b>/<strong>) is
paired per line, malformed markers stay readable, and ordinary angle-bracket text is kept.
"""

import re
from html.parser import HTMLParser

import pytest

from forest_bot.qa import _to_html, parse_answer_text
from forest_bot.telegram_bot import _strip_html

ALLOWED = {"b", "strong", "i", "em", "u", "s", "a", "code", "pre", "blockquote"}


class TelegramHTML(HTMLParser):
    """Strict model of Telegram's HTML mode: only supported tags, balanced; entities decoded."""

    def __init__(self, payload: str):
        super().__init__(convert_charrefs=True)
        self.shown, self.bold, self.stack, self._b = [], [], [], None
        self.feed(payload)
        self.close()
        assert not self.stack, f"unclosed tags {self.stack}"

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED, f"unsupported tag <{tag}>"
        self.stack.append(tag)
        if tag == "b":
            self._b = []

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}>"
        self.stack.pop()
        if tag == "b":
            self.bold.append("".join(self._b))
            self._b = None

    def handle_data(self, data):
        self.shown.append(data)
        if self._b is not None:
            self._b.append(data)

    @property
    def text(self) -> str:
        return "".join(self.shown)


def payload(model_text: str) -> str:
    """The real pipeline: parse the model output (entities decoded once), then render for Telegram."""
    return _to_html(parse_answer_text(f"СТАТУС: answered\nВІДПОВІДЬ:\n{model_text}")["answer"])


def shown(model_text: str) -> TelegramHTML:
    return TelegramHTML(payload(model_text))


ARTEFACTS = ("&quot;", "&amp;", "&lt;", "&gt;", "&nbsp;", "&#", "**", "<b>", "</b>", "<strong>")


@pytest.mark.parametrize("model_text, expected_text, expected_bold", [
    # numeric entities (hex and decimal) and named entities written by the model: decoded once
    ("Ді&#x43D; бро&#1076;у [1].", "Дін броду [1].", []),
    ("Правило &quot;тільки по греблі&quot; [1].", "Правило \"тільки по греблі\" [1].", []),
    ("Тобто&nbsp;вбрід не можна; гребля &amp; брід [1].", "Тобто вбрід не можна; гребля & брід [1].", []),
    ("Подвійно: &amp;quot;", "Подвійно: &quot;", []),                       # decoded exactly once
    # bold: Markdown and HTML, consistently
    ("**Висновок:** вбрід не можна [1].", "Висновок: вбрід не можна [1].", ["Висновок:"]),
    ("<b>Висновок:</b> вбрід не можна [1].", "Висновок: вбрід не можна [1].", ["Висновок:"]),
    ("<strong>Висновок:</strong> вбрід не можна [1].", "Висновок: вбрід не можна [1].", ["Висновок:"]),
    ("<B>Ні.</B> Тільки по **греблі** [1].", "Ні. Тільки по греблі [1].", ["Ні.", "греблі"]),
    # malformed bold stays readable, without stray markers
    ("**Висновок: перейти вбрід\nне можна** [1].", "Висновок: перейти вбрід\nне можна [1].", []),
    ("**Висновок:** перейти вбрід не можна** [1].", "Висновок: перейти вбрід не можна [1].", ["Висновок:"]),
    ("<b>Висновок: не можна [1].", "Висновок: не можна [1].", []),
    ("Ні.</b> Тільки по греблі.", "Ні. Тільки по греблі.", []),
    # ordinary angle-bracket text is kept as text, not stripped or interpreted
    ("Глибина <30 см, а течія > 2 кроків [1].", "Глибина <30 см, а течія > 2 кроків [1].", []),
    ("Формула: a<b і c>d; тег <i>не курсив</i> [1].", "Формула: a<b і c>d; тег <i>не курсив</i> [1].", []),
    ("Рядок<br>новий рядок", "Рядок\nновий рядок", []),
    # Ukrainian characters, apostrophes, quotes, numbers and citations unchanged
    ("Пам’ятка «Броди» [2][1]: 2 жмені, до 25 числа, 80 кроків; ґанок, їжак, є.",
     "Пам’ятка «Броди» [2][1]: 2 жмені, до 25 числа, 80 кроків; ґанок, їжак, є.", []),
])
def test_payload_is_valid_telegram_html_and_displays_clean_text(model_text, expected_text, expected_bold):
    rendered = shown(model_text)
    assert rendered.text == expected_text
    assert rendered.bold == expected_bold
    leftovers = [a for a in ARTEFACTS if a in rendered.text and a not in expected_text]
    assert leftovers == []


def test_plain_text_fallback_keeps_ordinary_angle_bracket_text():
    assert _strip_html(payload("**Ні.** Глибина <30 см, а течія > 2 кроків; a<b [1].")) == "Ні. Глибина <30 см, а течія > 2 кроків; a<b [1]."


async def test_final_telegram_payload_keeps_sources_urls_and_formatting(conversation, archive):
    conversation.llm.route = {"intent": "question", "locations": ["bilia_hrebli"], "needs_location": False,
                              "search_query": "перейти вбрід гребля", "keywords": ["вбрід", "гребля"]}
    conversation.llm.answer_fn = lambda m: {
        "status": "answered",
        "answer": "<b>Ні.</b> Біля бобрової греблі вбрід не ходять навіть за відкритої греблі — переходити "
                  "потрібно тільки по греблі【1】.\n**Місцева заборона має пріоритет над регіональною порадою** "
                  "&#x2014; &lt;увага&gt; [1]."}
    sent = (await conversation.on_text(1, "Я біля бобрової греблі. Можна перейти вбрід?"))[-1].text
    rendered = TelegramHTML(sent)                         # exactly what send_message receives
    body, _, sources = rendered.text.partition("Джерела:")
    assert body.startswith("Ні. Біля бобрової греблі вбрід не ходять навіть за відкритої греблі — переходити "
                           "потрібно тільки по греблі [1].") or body.startswith(
        "Ні. Біля бобрової греблі вбрід не ходять навіть за відкритої греблі — переходити потрібно тільки по "
        "греблі[1].")
    assert "Місцева заборона має пріоритет над регіональною порадою — <увага> [1]" in body
    assert rendered.bold[:2] == ["Ні.", "Місцева заборона має пріоритет над регіональною порадою"]
    assert not [a for a in ARTEFACTS if a in rendered.text]
    assert "Стежки до сусідів від рукавички Біля бобрової греблі" in sources and "— Лісовий архів, файл" in sources
    assert len(conversation.llm.answer_prompts) == 1      # «&#x2014;» no longer read as the number 2014
    links = re.findall(r'<a href="([^"]+)">', sent)
    assert links and all(u.startswith("https://") for u in links)
