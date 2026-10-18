import pytest

from forest_bot import geography as geo
from forest_bot.geography import FOREST, applicability, detect_locations, parse_location_answer


def test_mapping_matches_agreed_table():
    assert geo.regions_of("pid_yalynoyu") == {"khvoinyi"}
    assert geo.regions_of("u_sosnakh") == {"khvoinyi"}
    assert geo.regions_of("bilia_murashnyka") == {"khvoinyi"}
    assert geo.regions_of("pid_dubom") == {"lystianyi"}
    assert geo.regions_of("u_lishchyni") == {"lystianyi"}
    assert geo.regions_of("pid_verboyu") == {"lystianyi", "richka"}
    assert geo.regions_of("bilia_hrebli") == {"richka"}
    assert geo.regions_of("na_uzlissi") == frozenset()


def test_willow_gets_both_regions_at_equal_specificity():
    assert applicability(["lystianyi"], "pid_verboyu") == geo.LEVEL_REGION
    assert applicability(["richka"], "pid_verboyu") == geo.LEVEL_REGION
    assert applicability(["pid_verboyu"], "pid_verboyu") == geo.LEVEL_MITTEN
    assert applicability([FOREST], "pid_verboyu") == geo.LEVEL_FOREST


def test_forest_edge_gets_only_forest_and_own_advice():
    for region in geo.REGIONS:
        assert applicability([region], "na_uzlissi") == 0
    assert applicability([FOREST], "na_uzlissi") == geo.LEVEL_FOREST
    assert applicability(["na_uzlissi"], "na_uzlissi") == geo.LEVEL_MITTEN


def test_region_advice_does_not_leak_to_unrelated_mittens():
    assert applicability(["khvoinyi"], "pid_dubom") == 0
    assert applicability(["richka"], "u_lishchyni") == 0
    assert applicability(["lystianyi"], "bilia_hrebli") == 0
    assert applicability(["pid_dubom"], "u_lishchyni") == 0


def test_region_question_shows_member_mittens_as_local():
    assert geo.applicability_for_region(["pid_verboyu"], "richka") == geo.LEVEL_MITTEN
    assert geo.applicability_for_region(["pid_verboyu"], "lystianyi") == geo.LEVEL_MITTEN
    assert geo.applicability_for_region(["richka"], "richka") == geo.LEVEL_REGION
    assert geo.applicability_for_region(["na_uzlissi"], "khvoinyi") == 0


@pytest.mark.parametrize("text,expected", [
    ("Скільки внесок під дубом і до якого числа?", ["pid_dubom"]),
    ("Біля греблі мисливці! Як кричати?", ["bilia_hrebli"]),
    ("Де шукати гриби біля верби?", ["pid_verboyu"]),
    ("Коли утеплювати рукавичку на узліссі?", ["na_uzlissi"]),
    ("а у ліщині?", ["u_lishchyni"]),
    ("Що можна їсти біля мурашника", ["bilia_murashnyka"]),
    ("правила в соснах", ["u_sosnakh"]),
    ("Як утеплити рукавичку біля бобрової греблі", ["bilia_hrebli"]),
    ("під ялиною тиша коли?", ["pid_yalynoyu"]),
])
def test_detects_short_and_inflected_mitten_names(text, expected):
    assert detect_locations(text).mittens == expected


@pytest.mark.parametrize("text", [
    "Коли бобри відкривають греблю?",       # the dam itself, not the mitten
    "Мурашники кусаються?",                  # generic ant hills
    "Чи ростуть опеньки на старих вербах?",  # generic willows
    "Що робити при пожежі?",
])
def test_no_false_mitten_detection(text):
    assert detect_locations(text).mittens == []


def test_regions_and_forest_and_comparison():
    d = detect_locations("Що їсти в листяному лісі?")
    assert d.regions == ["lystianyi"] and not d.mittens
    d = detect_locations("Що їсти біля річки?")
    assert d.regions == ["richka"]
    d = detect_locations("Порівняй внески в усіх рукавичках")
    assert d.forest and d.comparison
    d = detect_locations("Де внесок менший: під дубом чи у ліщині?")
    assert d.mittens == ["pid_dubom", "u_lishchyni"] and d.comparison


def test_region_absorbed_by_its_own_mitten():
    d = detect_locations("Гриби під вербою біля річки")
    assert d.mittens == ["pid_verboyu"] and d.regions == []


def test_parse_location_answer():
    assert parse_location_answer("Під дубом") == ["pid_dubom"]
    assert parse_location_answer("під дубом і у ліщині") == ["pid_dubom", "u_lishchyni"]
    assert parse_location_answer("увесь ліс") == [FOREST]
    assert parse_location_answer("хвойний ліс") == ["khvoinyi"]
    assert parse_location_answer("не знаю") == []


def test_strip_locations_keeps_topic():
    assert geo.strip_locations("Скільки внесок під дубом і до якого числа?") == "скільки внесок і до якого числа?"
    assert "річку" in geo.strip_locations("Як перейти річку біля греблі?")


def test_place_only_follow_up_detection_and_place_replacement():
    assert geo.is_place_only("А у соснах?") and geo.is_place_only("а що під вербою?")
    assert not geo.is_place_only("А у соснах кому віддавати?")
    assert not geo.is_place_only("Скільки внесок?")
    assert geo.replace_place("Кому віддавати членський внесок у рукавичці «Під дубом»?", ["u_sosnakh"]) == \
        "Кому віддавати членський внесок у рукавичці «У соснах»?"
    assert geo.replace_place("Кому віддавати внесок?", ["u_sosnakh"]) == "Кому віддавати внесок — для: «У соснах»?"
