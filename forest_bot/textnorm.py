"""Tokenisation and light Ukrainian stemming for keyword (BM25) search."""

from __future__ import annotations

import re

from .geography import nfc

# Common Ukrainian inflectional endings, longest first.
_ENDINGS = sorted(
    """ями ами ові еві ого ому ему ими іми ій ий ої ою ею ах ях ів їв их іх им ім ам ям
    ати ити іти ють ують юють ується ються ться ся сь ює ує ає єш еш иш ать ять
    іння ення ання ність ості ість ові а я о е и і ї у ю ь й""".split(),
    key=len,
    reverse=True,
)

STOPWORDS = set(
    """і й та а але або чи що як де коли хто це цей ця ці той та те ті в у на з із зі до від для по про при
    під над за без між через біля коло не ні так же ж би б бо якщо щоб тож тому його її їх їм ми ви вони
    він вона воно я ти мене тебе нас вас є був була було були буде бути можна треба має мають який яка яке
    які яких скільки чому ще вже теж також лише тільки дуже свій своя своє свої наш наша наше наші""".split()
)

_TOKEN = re.compile(r"[a-zа-щьюяґєії0-9']+")


# All case forms of a month name -> one token («жовтень», «жовтня», «жовтні», «жовтнем» -> «жовтн»), so the
# nominative of a month and its locative match. Only exact month forms are mapped («береза» stays a tree).
_MONTH_FORMS = re.compile(
    r"^(?:(січ)(?:ень|ня|ні|нем)|(лют)(?:ий|ого|ому)|(берез)(?:ень|ня|ні|нем)|(квіт)(?:ень|ня|ні|нем)|"
    r"(трав)(?:ень|ня|ні|нем)|(черв)(?:ень|ня|ні|нем)|(лип)(?:ень|ня|ні|нем)|(серп)(?:ень|ня|ні|нем)|"
    r"(верес)(?:ень|ня|ні|нем)|(жовт)(?:ень|ня|ні|нем)|(листопад)(?:а|і|ом|у)?|(груд)(?:ень|ня|ні|нем))$")
_MONTH_TOKENS = ["січн", "лютий", "березн", "квітн", "травн", "червн", "липн", "серпн", "вересн", "жовтн",
                 "листопад", "грудн"]
MONTH_TOKENS = frozenset(_MONTH_TOKENS)


def stem(word: str) -> str:
    m = _MONTH_FORMS.match(word)
    if m:
        return _MONTH_TOKENS[next(i for i, g in enumerate(m.groups()) if g)]
    if len(word) > 5 and word.endswith(("ся", "сь")):
        word = word[:-2]                      # reflexive verbs: купатися -> купати
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[: -len(ending)]
            break
    return word[:5]


def tokenize(text: str) -> list[str]:
    text = nfc(text).lower().replace("’", "'").replace("ʼ", "'")
    out = []
    for tok in _TOKEN.findall(text):
        tok = tok.strip("'")
        if not tok or tok in STOPWORDS:
            continue
        if tok.isdigit():
            out.append(tok)
            continue
        if len(tok) < 2:
            continue
        out.append(stem(tok))
    return out
