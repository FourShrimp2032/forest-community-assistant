"""Persistent dialogue context: circumstances the user established earlier in the conversation.

Only two kinds are tracked, because they change which document rules apply:
- season ("узимку")          -> seasonal rules (e.g. «Узимку внески вдвічі менші»)
- identity ("дорослий борсук") -> rules named for a specific animal or group

They persist across turns until the user states something different or sends /reset, and are passed
explicitly to routing, retrieval, answer generation and the grounding check, so that a follow-up does
not silently lose them.
"""

from __future__ import annotations

import re

from .geography import normalize

SEASONS: dict[str, str] = {
    "зима": r"\b(узимку|взимку|зимою|взимі|зима|зими|зимов\w*)\b",
    "весна": r"\b(навесні|весною|весна|весни|весн\w+)\b",
    "літо": r"\b(влітку|улітку|літом|літо|літа|літн\w*)\b",
    "осінь": r"\b(восени|осінь|осені|осінн\w*)\b",
}
SEASON_SEARCH_TERMS = {"зима": "узимку зима", "весна": "навесні весна", "літо": "влітку літо",
                       "осінь": "восени осінь"}

# Residents named in the archive plus a few generic animals.
_ANIMALS = (
    "борсук бобер видра кабан сова сойк ведмід сорок вивірк жабк качк горобц горобец мишк білочк дятел дятл "
    "зайчик зайц заєць вовчик вовк синиц лисичк лис ворон дрізд їжак кріт крот перепілк сплюх пташ поросят"
).split()
_IDENTITY = re.compile(
    r"\bя\s*(?:[—–-]\s*|це\s+|є\s+)?(?:(?:тепер|вже|зараз|насправді|теж|також)\s+)?"
    r"((?:(?:доросл|мал|молод|стар)\w*\s+)?(?:" + "|".join(_ANIMALS) + r")\w*)"
)


_CURRENT = re.compile(r"\b(зараз|тепер|наразі|надворі|сьогодні|у\s+нас|в\s+нас|вже\s+настал\w*|настал\w*)\b")


def extract(message: str) -> dict:
    """Circumstances stated in this message (deterministic).

    A season named inside a question («хто приймає внески взимку?») is the topic of that question
    (season_scope="question"); only an explicit statement of the present («зараз зима», «у нас зима»)
    is the user's current circumstance (season_scope="current")."""
    norm = normalize(message)
    found: dict = {}
    for season, pattern in SEASONS.items():
        if re.search(pattern, norm):
            found["season"] = season
            found["season_scope"] = "current" if _CURRENT.search(norm) else "question"
            break
    m = _IDENTITY.search(norm)
    if m:
        found["identity"] = re.sub(r"\s+", " ", m.group(1)).strip()
    return found


_HYPOTHETICAL = re.compile(r"\b(якби|якщо\s+б|коли\s+б|уяв\w*|припуст\w*|припуст\w*|гіпотетичн\w*|"
                           r"а\s+що\s+як|що\s+було\s+б|якщо\s+я|якщо\s+зараз|якщо\s+це|наприклад\s+якщо)\b")
_FIRST_PERSON = re.compile(r"\b(я|мене|мені|мій|моя|моє|мої)\b")
_SUBJECT = re.compile(r"\b(?:для|про|щодо)\s+((?:(?:доросл|мал|молод|стар)\w*\s+)?(?:" + "|".join(_ANIMALS) + r")\w*)")


def is_hypothetical(message: str) -> bool:
    """Conditional / imagined circumstances apply to this answer only, not to the user's profile."""
    return bool(_HYPOTHETICAL.search(normalize(message)))


def is_first_person(message: str) -> bool:
    return bool(_FIRST_PERSON.search(normalize(message)))


def question_subject(message: str) -> str | None:
    """Another animal the current question is about ("А для зайчика?") — for this turn only."""
    m = _SUBJECT.search(normalize(message))
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


_GENITIVE_IRREGULAR = {"ведмідь": "ведмедя", "кріт": "крота", "горобець": "горобця", "дятел": "дятла",
                       "бобер": "бобра", "заєць": "зайця", "орел": "орла", "сплюх": "сплюха"}


def _genitive_word(word: str) -> str:
    w = word.lower()
    if w in _GENITIVE_IRREGULAR:
        return _GENITIVE_IRREGULAR[w]
    if w.endswith("ий"):
        return w[:-2] + "ого"
    if w.endswith("ій"):
        return w[:-2] + "ього"
    if w.endswith(("ка", "га", "ха")):
        return w[:-1] + "и"
    if w.endswith("ця") or w.endswith("я"):
        return w[:-1] + "і"
    if w.endswith("а"):
        return w[:-1] + "и"
    if w.endswith("ь") or w.endswith("й"):
        return w[:-1] + "я"
    if w.endswith("о"):
        return w[:-1] + "а"
    return w + "а"


def genitive(phrase: str) -> str:
    """«дорослий борсук» -> «дорослого борсука», «доросла видра» -> «дорослої видри» (archive animals)."""
    words = phrase.split()
    out = []
    for w in words:
        if w.lower() in ("доросла", "мала", "молода", "стара"):
            out.append(w[:-1] + "ої")
        else:
            out.append(_genitive_word(w))
    return " ".join(out)


def merge(previous: dict | None, *updates: dict | None) -> dict:
    """Later updates override earlier values per key; empty values are ignored."""
    merged = dict(previous or {})
    for update in updates:
        for key in ("season", "season_scope", "identity"):
            value = (update or {}).get(key)
            if isinstance(value, str) and value.strip() and value.strip().lower() not in ("null", "none"):
                merged[key] = value.strip().lower()
    return merged


def describe(context: dict | None) -> str:
    parts = []
    if (context or {}).get("season"):
        parts.append(f"сезон — {context['season']}")
    if (context or {}).get("identity"):
        parts.append(f"користувач — {context['identity']}")
    return "; ".join(parts)


def search_terms(context: dict | None) -> str:
    return SEASON_SEARCH_TERMS.get((context or {}).get("season", ""), "")


def season_pattern(context: dict | None) -> str | None:
    season = (context or {}).get("season")
    return SEASONS.get(season) if season else None
