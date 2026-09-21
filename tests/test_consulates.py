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



# ---------------------------------------------------------------------------
# Consulate-local time
# ---------------------------------------------------------------------------

def test_every_consulate_has_a_timezone():
    from app.consulates import FACILITIES, TIMEZONES
    assert set(TIMEZONES) == set(FACILITIES), "a post without a zone falls back silently"


def test_every_timezone_actually_resolves():
    """Guards the tzdata dependency: zoneinfo has no database of its own on
    Windows or in a slim Docker image."""
    from zoneinfo import ZoneInfo

    from app.consulates import TIMEZONES
    for facility_id, name in TIMEZONES.items():
        assert ZoneInfo(name) is not None, f"{facility_id} -> {name}"


def test_the_three_canadian_offsets_are_distinct():
    """Toronto, Vancouver and Halifax must not collapse to one clock."""
    from app.consulates import consulate_now

    toronto, vancouver, halifax = (consulate_now(f) for f in (94, 95, 90))
    offsets = {t.utcoffset() for t in (toronto, vancouver, halifax)}
    assert len(offsets) == 3, f"expected three distinct offsets, got {offsets}"
    # Vancouver is Pacific, so always behind Eastern.
    assert vancouver.utcoffset() < toronto.utcoffset()
    # Halifax is Atlantic, so always ahead of Eastern.
    assert halifax.utcoffset() > toronto.utcoffset()


def test_vancouver_is_three_hours_behind_toronto():
    """The concrete case that makes a single container TZ wrong."""
    from app.consulates import consulate_now

    delta = consulate_now(94).utcoffset() - consulate_now(95).utcoffset()
    assert delta.total_seconds() == 3 * 3600


def test_eastern_posts_share_one_zone():
    from app.consulates import consulate_timezone
    eastern = {consulate_timezone(f) for f in (91, 92, 93, 94)}  # MTL OTT QUB TRT
    assert eastern == {"America/Toronto"}


def test_calgary_is_mountain_not_pacific():
    from app.consulates import consulate_timezone
    assert consulate_timezone(89) == "America/Edmonton"


def test_an_unknown_facility_falls_back_instead_of_raising():
    """A missing entry must never be able to stop polling."""
    from app.consulates import consulate_local_hour, consulate_timezone
    assert consulate_timezone(99999) == "America/Toronto"
    assert 0 <= consulate_local_hour(99999) <= 23


def test_local_hour_is_in_range_for_every_post():
    from app.consulates import FACILITIES, consulate_local_hour
    for facility_id in FACILITIES:
        assert 0 <= consulate_local_hour(facility_id) <= 23
