"""Geographic model of the forest: mittens, regions and applicability rules.

The mapping is maintained here, independently of individual advice documents
(it matches the archive's «Карта лісу»).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

FOREST = "forest"

MITTENS: dict[str, str] = {
    "pid_yalynoyu": "Під ялиною",
    "u_sosnakh": "У соснах",
    "bilia_murashnyka": "Біля мурашника",
    "pid_dubom": "Під дубом",
    "u_lishchyni": "У ліщині",
    "pid_verboyu": "Під вербою",
    "bilia_hrebli": "Біля бобрової греблі",
    "na_uzlissi": "На узліссі",
}

REGIONS: dict[str, str] = {
    "khvoinyi": "Хвойний ліс",
    "lystianyi": "Листяний ліс",
    "richka": "Біля річки",
}

MITTEN_REGIONS: dict[str, frozenset[str]] = {
    "pid_yalynoyu": frozenset({"khvoinyi"}),
    "u_sosnakh": frozenset({"khvoinyi"}),
    "bilia_murashnyka": frozenset({"khvoinyi"}),
    "pid_dubom": frozenset({"lystianyi"}),
    "u_lishchyni": frozenset({"lystianyi"}),
    "pid_verboyu": frozenset({"lystianyi", "richka"}),
    "bilia_hrebli": frozenset({"richka"}),
    "na_uzlissi": frozenset(),
}

ALL_CODES = (*MITTENS, *REGIONS, FOREST)

# Specificity levels used for priority: mitten > region > whole forest.
LEVEL_MITTEN = 3
LEVEL_REGION = 2
LEVEL_FOREST = 1
LEVEL_NAMES = {LEVEL_MITTEN: "рукавичка", LEVEL_REGION: "частина лісу", LEVEL_FOREST: "увесь ліс"}


def is_mitten(code: str) -> bool:
    return code in MITTENS


def is_region(code: str) -> bool:
    return code in REGIONS


def regions_of(mitten: str) -> frozenset[str]:
    return MITTEN_REGIONS[mitten]


def mittens_in_region(region: str) -> list[str]:
    return [m for m, regs in MITTEN_REGIONS.items() if region in regs]


def name_of(code: str) -> str:
    if code in MITTENS:
        return MITTENS[code]
    if code in REGIONS:
        return REGIONS[code]
    if code == FOREST:
        return "Увесь ліс"
    raise KeyError(code)


def label_of(code: str) -> str:
    """Human label: «рукавичка «Під дубом»», «Листяний ліс», «увесь ліс»."""
    if code in MITTENS:
        return f"рукавичка «{MITTENS[code]}»"
    if code in REGIONS:
        return REGIONS[code]
    return "увесь ліс"


def scope_label(scope) -> str:
    codes = sort_codes(scope)
    return ", ".join(label_of(c) for c in codes) if codes else "—"


def sort_codes(codes) -> list[str]:
    order = {c: i for i, c in enumerate(ALL_CODES)}
    return sorted(set(codes), key=lambda c: order.get(c, 99))


def validate_codes(codes) -> list[str]:
    return sort_codes(c for c in codes if c in ALL_CODES)


def normalize_scope(codes) -> list[str]:
    """Canonical document scope: FOREST absorbs everything; a region absorbs its own mittens is NOT
    applied (a mitten-specific passage stays mitten-specific even if its region is also listed)."""
    codes = set(validate_codes(codes))
    if FOREST in codes:
        return [FOREST]
    return sort_codes(codes)


def applicability(scope, mitten: str) -> int:
    """Specificity of advice with `scope` for `mitten`; 0 = does not apply.

    - advice for this mitten              -> 3
    - advice for one of its regions       -> 2 («Під вербою» has two regions, both apply)
    - advice for the whole forest         -> 1
    «На узліссі» belongs to no region, so only forest-wide and its own advice applies.
    """
    scope = set(scope)
    if mitten in scope:
        return LEVEL_MITTEN
    if scope & MITTEN_REGIONS[mitten]:
        return LEVEL_REGION
    if FOREST in scope:
        return LEVEL_FOREST
    return 0


def applicability_for_region(scope, region: str) -> int:
    """Specificity for a question about a whole region.

    Region advice -> 2, forest-wide -> 1, advice for a mitten inside the region -> 3
    (shown as a local difference, labelled with its mitten)."""
    scope = set(scope)
    if any(m in scope for m in mittens_in_region(region)):
        return LEVEL_MITTEN
    if region in scope:
        return LEVEL_REGION
    if FOREST in scope:
        return LEVEL_FOREST
    return 0


def scope_level(scope) -> int:
    scope = set(scope)
    if scope & set(MITTENS):
        return LEVEL_MITTEN
    if scope & set(REGIONS):
        return LEVEL_REGION
    if FOREST in scope:
        return LEVEL_FOREST
    return 0


# --------------------------------------------------------------------- text detection

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def normalize(text: str) -> str:
    text = nfc(text).lower()
    text = re.sub(r"[’ʼ`´‘]", "'", text)
    text = text.replace("«", " ").replace("»", " ").replace("\"", " ").replace("“", " ").replace("”", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text


# Patterns for user questions and clarification answers. They accept shortened and inflected
# forms ("біля греблі", "біля верби") but avoid generic words ("мурашники", "на вербах",
# "бобри відкривають греблю").
QUERY_PATTERNS: dict[str, list[str]] = {
    "pid_yalynoyu": [r"під\s+ялин(ою|кою)\b", r"\b[ув]\s+ялин(і|ці)\b", r"\b(біля|коло|до)\s+ялин(и|ки)\b"],
    "u_sosnakh": [r"\bсоснах\b", r"\b(біля|коло|до)\s+сосон\b"],
    "bilia_murashnyka": [r"\bмурашник(а|у|ом|і)?\b"],
    "pid_dubom": [r"під\s+дубом\b", r"\b(біля|коло|до)\s+дуба\b", r"\b[ув]\s+дубі\b"],
    "u_lishchyni": [r"\bліщин(а|и|і|у|ою)\b", r"\bліщинник\w*"],
    "pid_verboyu": [r"\bверб(а|и|і|у|ою)\b"],
    "bilia_hrebli": [r"бобров\w*\s+гребл\w*", r"\b(біля|коло|під|на|у|в|до)\s+гребл(і|ею)\b"],
    "na_uzlissi": [r"\bузлісс\w*"],
    "khvoinyi": [r"\bхвойн\w*"],
    "lystianyi": [r"\bлистян\w*"],
    "richka": [r"\b(біля|коло|на|до|уздовж|над|при)\s+річ(ки|ці|ку|кою)\b", r"\bрічк(а|и|у|ою)\b",
               r"\bприбереж\w*"],
}

FOREST_PATTERNS = [
    r"\b(у|в)\s+(усьому|всьому|цілому)\s+лісі\b",
    r"\bпо\s+(усьому|всьому)\s+лісу\b",
    r"\b(у|в)\s+лісі\s+загалом\b",
    r"\b(у|в)?\s*(ус|вс)(і|іх|ім|ьому)\s+рукавич\w*",
    r"\bкожн\w+\s+рукавич\w*",
    r"\b(у|в)?весь\s+ліс\b",
    r"\bвсього\s+лісу\b|\bусього\s+лісу\b",
    r"\b(у|в)\s+лісі\b",
    r"\bрізних\s+рукавич\w*",
]

COMPARISON_PATTERNS = [
    r"\bпорівня\w*",
    r"\bрізниц\w*",
    r"\bвідрізня\w*",
    r"\bнайменш\w*|\bнайбільш\w*|\bнайдешевш\w*|\bнайдорожч\w*",
    r"\b(у|в)\s+як(ій|их)\s+рукавич\w*",
    r"\bде\s+(більш|менш)\w*",
    r"\bа\s+(у|в)\s+інших\b",
]


@dataclass
class DetectedLocations:
    mittens: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    forest: bool = False
    comparison: bool = False

    @property
    def codes(self) -> list[str]:
        return [*self.mittens, *self.regions]

    @property
    def any(self) -> bool:
        return bool(self.mittens or self.regions or self.forest)


def detect_locations(text: str) -> DetectedLocations:
    """Explicit location references in a user's question or answer."""
    norm = normalize(text)
    found: list[str] = []
    for code, patterns in QUERY_PATTERNS.items():
        if any(re.search(p, norm) for p in patterns):
            found.append(code)
    mittens = [c for c in found if c in MITTENS]
    regions = [c for c in found if c in REGIONS]
    # A region mentioned together with one of its own mittens only describes that mitten
    # ("під вербою біля річки").
    regions = [r for r in regions if not any(r in MITTEN_REGIONS[m] for m in mittens)]
    forest = any(re.search(p, norm) for p in FOREST_PATTERNS)
    if re.search(r"\bвесь\s+ліс\b|\bувесь\s+ліс\b|\bцілий\s+ліс\b", norm):
        forest = True
    comparison = len(mittens) + len(regions) > 1 or any(re.search(p, norm) for p in COMPARISON_PATTERNS)
    return DetectedLocations(mittens, regions, forest, comparison)


_STRICT_NAMES = [r"під\s+ялиною", r"[ув]\s+соснах", r"біля\s+мурашника", r"під\s+дубом", r"[ув]\s+ліщині",
                 r"під\s+вербою", r"біля\s+бобрової\s+греблі", r"на\s+узліссі", r"рукавичк\w*"]


def strip_locations(text: str) -> str:
    """Remove place references from a search query (places are handled by scope filtering)."""
    norm = normalize(text)
    patterns = [p for code, ps in QUERY_PATTERNS.items() for p in ps
                if not (code == "richka" and p.startswith(r"\bрічк"))]  # keep "перейти річку"
    patterns += _STRICT_NAMES + FOREST_PATTERNS
    for p in patterns:
        norm = re.sub(p, " ", norm)
    norm = re.sub(r"\b(біля|під|на|у|в|коло)\s*$", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


_FILLER = {"а", "і", "й", "та", "ну", "як", "що", "щодо", "про", "там", "тоді", "для", "у", "в", "на", "біля", "під",
           "коло", "рукавичка", "рукавичці", "рукавички", "рукавичку", "теж", "також", "ж", "же"}


def is_place_only(text: str) -> bool:
    """True if the message names a place and nothing else ("А у соснах?", "а що під вербою?")."""
    if not detect_locations(text).any:
        return False
    rest = re.sub(r"[^\w\s']", " ", strip_locations(text))
    return all(w in _FILLER for w in rest.split())


def replace_place(question: str, new_codes: list[str]) -> str:
    """Put new place(s) into a resolved question: «Під дубом» -> «У соснах» (mitten names only)."""
    new = [c for c in new_codes if c in MITTENS or c in REGIONS]
    if not new:
        return question
    label = ", ".join(f"«{name_of(c)}»" for c in new)
    replaced = question
    for name in MITTENS.values():
        replaced = re.sub(rf"«?{re.escape(name)}»?", label, replaced, flags=re.I)
    if replaced == question:
        replaced = question.rstrip(" ?.") + f" — для: {label}?"
    return replaced


def parse_location_answer(text: str) -> list[str]:
    """Locations from a free-text clarification answer ("під дубом і у ліщині", "увесь ліс")."""
    det = detect_locations(text)
    norm = normalize(text).strip(" .!")
    codes = det.codes
    if det.forest or norm in ("ліс", "весь ліс", "увесь ліс", "усі", "всі", "для всіх", "увесь", "весь"):
        codes = [*codes, FOREST]
    # Region names typed in nominative without a preposition: "хвойний ліс", "річка".
    if re.fullmatch(r"річка|біля річки", norm):
        codes.append("richka")
    return validate_codes(codes)
