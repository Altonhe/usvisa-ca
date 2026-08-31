import pytest

from app.consulates import (FACILITIES, UnknownConsulate, consulate_name,
                            parse_consulate_list, parse_id_list,
                            resolve_consulate)


@pytest.mark.parametrize(
    "token,expected",
    [
        # canonical names
        ("Toronto", 94), ("toronto", 94), ("Vancouver", 95),
        ("Quebec City", 93), ("quebec-city", 93), ("QUEBEC CITY", 93),
        # consular post codes
        ("TRT", 94), ("trt", 94), ("VAN", 95), ("CAL", 89),
        ("HAL", 90), ("MTL", 91), ("OTT", 92), ("QUB", 93),
        # airport-style aliases
        ("YYZ", 94), ("YVR", 95),
        # raw ids
        ("94", 94), (94, 94), ("89", 89),
    ],
)
def test_resolve_consulate(token, expected):
    assert resolve_consulate(token) == expected


@pytest.mark.parametrize("bad", ["Winnipeg", "", "999", "  ", 42])
def test_resolve_consulate_rejects_unknown(bad):
    with pytest.raises((UnknownConsulate, ValueError)):
        resolve_consulate(bad)


def test_all_seven_facilities_present():
    # Verified against the live facility_id <select> on the appointment form.
    assert FACILITIES == {
        89: "Calgary", 90: "Halifax", 91: "Montreal", 92: "Ottawa",
        93: "Quebec City", 94: "Toronto", 95: "Vancouver",
    }


def test_consulate_name():
    assert consulate_name(94) == "Toronto"
    assert consulate_name(999) == "facility#999"


def test_parse_consulate_list_preserves_order_and_dedupes():
    assert parse_consulate_list("TRT,VAN") == [94, 95]
    assert parse_consulate_list("VAN,TRT") == [95, 94]
    assert parse_consulate_list("Toronto, 95 ; TRT") == [94, 95]


def test_parse_consulate_list_empty():
    assert parse_consulate_list("") == list(FACILITIES)
    assert parse_consulate_list(None, default_all=False) == []


def test_parse_id_list():
    assert parse_id_list("72856817, 72856546") == ["72856817", "72856546"]
    assert parse_id_list("111,111") == ["111"]
    assert parse_id_list("") == []


def test_parse_id_list_rejects_non_numeric():
    # A typo here would silently make the bot work on the wrong application.
    with pytest.raises(ValueError):
        parse_id_list("abc")
