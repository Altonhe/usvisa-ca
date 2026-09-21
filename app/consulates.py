"""Canadian consulate (facility) mapping and alias resolution.

The numeric ids below are the ``facility_id`` values used by
``appointments[consulate_appointment][facility_id]`` on the AIS appointment
form.  All seven were read directly from the live ``<select>`` on
``/en-ca/niv/schedule/{id}/appointment`` and are confirmed correct.
"""

from datetime import datetime
from typing import Dict, List, Union
from zoneinfo import ZoneInfo

# facility_id -> canonical display name (as labelled by the site)
FACILITIES: Dict[int, str] = {
    89: "Calgary",
    90: "Halifax",
    91: "Montreal",
    92: "Ottawa",
    93: "Quebec City",
    94: "Toronto",
    95: "Vancouver",
}

# facility_id -> IANA timezone of the post itself.
#
# Appointment slots are released by the consulate, so "business hours" is a
# fact about where the post is, not about where this process happens to run.
# Canada spans 4.5 zones: when it is 08:00 in Toronto it is 05:00 in Vancouver,
# so driving a schedule off a single container-wide TZ misjudges every post
# except one. The TZ environment variable stays what it was meant to be -- the
# timezone log timestamps are rendered in.
#
# Montreal, Ottawa and Quebec City are all Eastern; IANA treats
# America/Montreal as a link to America/Toronto, so the canonical name is used.
TIMEZONES: Dict[int, str] = {
    89: "America/Edmonton",     # Calgary, Alberta -- Mountain
    90: "America/Halifax",      # Atlantic
    91: "America/Toronto",      # Montreal -- Eastern
    92: "America/Toronto",      # Ottawa -- Eastern
    93: "America/Toronto",      # Quebec City -- Eastern
    94: "America/Toronto",      # Eastern
    95: "America/Vancouver",    # Pacific
}

# Kept for backwards compatibility with older configs / scripts that did
# ``CONSULATES["Toronto"]``.
CONSULATES: Dict[str, int] = {name: fid for fid, name in FACILITIES.items()}

# Accepted spellings: canonical name, consular post code, and common variants.
# Everything is matched case-insensitively after whitespace/punctuation folding.
ALIASES: Dict[str, int] = {
    "cal": 89, "calgary": 89, "yyc": 89,
    "hal": 90, "halifax": 90, "yhz": 90,
    "mtl": 91, "montreal": 91, "yul": 91,
    "ott": 92, "ottawa": 92, "yow": 92,
    "qub": 93, "que": 93, "quebec": 93, "quebeccity": 93, "yqb": 93,
    "trt": 94, "tor": 94, "toronto": 94, "yyz": 94,
    "van": 95, "vancouver": 95, "yvr": 95,
}


class UnknownConsulate(ValueError):
    """Raised when a configured consulate token cannot be resolved."""


def _fold(token: str) -> str:
    """Lowercase and strip everything that is not a letter or a digit."""
    return "".join(ch for ch in token.lower() if ch.isalnum())


def resolve_consulate(token: Union[str, int]) -> int:
    """Resolve a user-supplied consulate token to a numeric ``facility_id``.

    Accepts the canonical name (``Toronto``), a post code (``TRT``), an
    airport-style alias (``YYZ``), or the raw numeric id (``94`` / ``"94"``).
    Accents and punctuation are ignored, so ``"Quebec City"``, ``"quebec-city"``
    and ``"QUB"`` all resolve to 93.
    """
    if isinstance(token, int):
        if token in FACILITIES:
            return token
        raise UnknownConsulate(f"Unknown facility id: {token}")

    raw = str(token).strip()
    if not raw:
        raise UnknownConsulate("Empty consulate token")

    if raw.isdigit():
        fid = int(raw)
        if fid in FACILITIES:
            return fid
        raise UnknownConsulate(f"Unknown facility id: {raw}")

    folded = _fold(raw)

    # Fold accented spellings such as "Montréal" / "Québec" down to ASCII.
    for accented, plain in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a")):
        folded = folded.replace(accented, plain)

    if folded in ALIASES:
        return ALIASES[folded]

    for fid, name in FACILITIES.items():
        if _fold(name) == folded:
            return fid

    raise UnknownConsulate(
        f"Unknown consulate {raw!r}. Accepted values: "
        + ", ".join(sorted(FACILITIES.values()))
        + " (or post codes CAL/HAL/MTL/OTT/QUB/TRT/VAN, or ids 89-95)"
    )


def consulate_name(facility_id: int) -> str:
    """Human readable name for a facility id (falls back to the raw id)."""
    return FACILITIES.get(facility_id, f"facility#{facility_id}")


def consulate_timezone(facility_id: int) -> str:
    """IANA timezone name for a facility id.

    Falls back to Eastern, which is where four of the seven posts are, rather
    than raising: a missing entry must never be able to stop polling.
    """
    return TIMEZONES.get(facility_id, "America/Toronto")


def consulate_now(facility_id: int) -> datetime:
    """Current local time at the consulate.

    ``zoneinfo`` needs a tz database, which neither Windows nor a slim Docker
    image provides on its own -- hence the ``tzdata`` dependency. If the lookup
    somehow still fails, this degrades to process-local time rather than
    breaking the caller.
    """
    try:
        return datetime.now(ZoneInfo(consulate_timezone(facility_id)))
    except Exception:  # noqa: BLE001 - never worth failing a sweep over
        return datetime.now()


def consulate_local_hour(facility_id: int) -> int:
    """Hour of day (0-23) as it currently reads at the consulate."""
    return consulate_now(facility_id).hour


def parse_consulate_list(raw: Union[str, None], default_all: bool = True) -> List[int]:
    """Parse a comma/semicolon separated consulate list into facility ids.

    Order is preserved because it doubles as the preference order used when
    several consulates offer an acceptable slot.  Duplicates are dropped.
    An empty/missing value yields every consulate when ``default_all`` is set,
    otherwise an empty list.
    """
    if raw is None or not str(raw).strip():
        return list(FACILITIES) if default_all else []

    tokens = [t for t in str(raw).replace(";", ",").split(",") if t.strip()]
    resolved: List[int] = []
    for token in tokens:
        fid = resolve_consulate(token)
        if fid not in resolved:
            resolved.append(fid)
    return resolved


def parse_id_list(raw: Union[str, None]) -> List[str]:
    """Parse a comma/semicolon separated list of numeric ids into strings.

    Used for ``SCHEDULE_IDS``.  Non-numeric entries are rejected loudly rather
    than silently ignored, because a typo here means the bot would quietly work
    on the wrong application.
    """
    if raw is None or not str(raw).strip():
        return []

    tokens = [t.strip() for t in str(raw).replace(";", ",").split(",") if t.strip()]
    ids: List[str] = []
    for token in tokens:
        if not token.isdigit():
            raise ValueError(f"SCHEDULE_IDS entry {token!r} is not a numeric id")
        if token not in ids:
            ids.append(token)
    return ids
