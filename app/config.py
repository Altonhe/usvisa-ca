"""Configuration loading.

``config.yaml`` is the single source of application configuration: accounts,
credentials, targets, notification tokens and dashboard auth all live there.
``.env`` is reserved for Docker-level knobs (the published port, the timezone)
and is read by Compose itself, not by this application.

``${VAR}`` placeholders are still expanded from the environment for anyone who
prefers to inject a value that way, but nothing requires it.

Settings cascade in three levels, each overriding the one before it::

    defaults:          -> applies to every account
      accounts[].      -> applies to every application on that account
        applications[] -> applies to that one application

That is what lets you say "watch Toronto for everyone, but this one applicant
should also accept Vancouver and must land before 2026-12-30".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .consulates import consulate_name, parse_consulate_list

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATH = Path(os.getenv("USVISA_CONFIG", "config.yaml"))


class ConfigError(RuntimeError):
    """Raised for any unusable configuration, with a message aimed at humans."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _interpolate(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` from the environment."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            env = os.getenv(name)
            if env is not None and env != "":
                return env
            if default is not None:
                return default
            return ""
        return ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _as_date(value: Any, label: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip().replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(
            f"{label}: {value!r} is not a valid date, expected YYYY-MM-DD"
        ) from exc


def _as_exclusions(value: Any, label: str) -> List[Tuple[date, date]]:
    """Parse a list of ``[start, end]`` pairs (or ``{from:, to:}`` maps)."""
    if not value:
        return []
    out: List[Tuple[date, date]] = []
    for i, item in enumerate(value, 1):
        if isinstance(item, dict):
            start = _as_date(item.get("from") or item.get("start"), f"{label}[{i}].from")
            end = _as_date(item.get("to") or item.get("end"), f"{label}[{i}].to")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start = _as_date(item[0], f"{label}[{i}][0]")
            end = _as_date(item[1], f"{label}[{i}][1]")
        else:
            raise ConfigError(
                f"{label}[{i}]: expected [start, end] or {{from:, to:}}, got {item!r}"
            )
        if not start or not end:
            raise ConfigError(f"{label}[{i}]: both ends of the range are required")
        if start > end:
            raise ConfigError(f"{label}[{i}]: start {start} is after end {end}")
        out.append((start, end))
    return out


def _as_hour_windows(value: Any, label: str) -> List[Tuple[int, int]]:
    """Parse local-hour windows for fast polling.

    Accepts ``[[9, 17], ...]``, ``[{from: 9, to: 17}]`` or the shorthand
    ``"9-17, 20-22"``. Ends are inclusive of the start hour and exclusive of
    the end hour, so ``[9, 17]`` means 09:00:00 up to 16:59:59. A window that
    wraps midnight (``[22, 6]``) is allowed and handled by the caller.
    """
    if not value:
        return []
    items: Any = value
    if isinstance(value, str):
        items = [
            part.split("-", 1)
            for part in value.replace(";", ",").split(",")
            if part.strip()
        ]
    out: List[Tuple[int, int]] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            pair = (item.get("from") or item.get("start"), item.get("to") or item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pair = (item[0], item[1])
        else:
            raise ConfigError(
                f"{label}[{i}]: expected [start_hour, end_hour], got {item!r}"
            )
        try:
            start, end = int(str(pair[0]).strip()), int(str(pair[1]).strip())
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"{label}[{i}]: hours must be integers, got {item!r}") from exc
        for h in (start, end):
            if not 0 <= h <= 24:
                raise ConfigError(f"{label}[{i}]: hour {h} is outside 0..24")
        if start == end:
            raise ConfigError(
                f"{label}[{i}]: start and end hour are both {start}, which selects "
                "no time at all"
            )
        out.append((start, end))
    return out


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _as_int(value: Any, default: int, label: str = "") -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{label or 'value'}: {value!r} is not an integer") from exc


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """The date/consulate goal shared by accounts and applications."""

    consulates: List[int] = field(default_factory=list)
    earliest: Optional[date] = None
    latest: Optional[date] = None
    exclusions: List[Tuple[date, date]] = field(default_factory=list)
    prefer_earliest: bool = True

    def describe_window(self) -> str:
        if self.earliest and self.latest:
            return f"{self.earliest} .. {self.latest}"
        if self.latest:
            return f"before {self.latest}"
        if self.earliest:
            return f"after {self.earliest}"
        return "any date"

    def describe_consulates(self) -> str:
        return ", ".join(consulate_name(c) for c in self.consulates) or "none"

    def accepts(self, day: date) -> bool:
        if self.earliest and day < self.earliest:
            return False
        if self.latest and day > self.latest:
            return False
        return self.excluded_by(day) is None

    def excluded_by(self, day: date) -> Optional[Tuple[date, date]]:
        for start, end in self.exclusions:
            if start <= day <= end:
                return (start, end)
        return None


@dataclass
class Application:
    """One AIS application (one ``schedule_id``)."""

    schedule_id: str = ""
    label: str = ""
    target: Target = field(default_factory=Target)

    @property
    def display_name(self) -> str:
        if self.label and self.schedule_id:
            return f"{self.label} ({self.schedule_id})"
        return self.label or self.schedule_id or "unknown"


@dataclass
class Group:
    """A joint constraint across several applications on the same account.

    Unlike a plain ``Target``, a group is not satisfied by each member finding
    its own best day independently: every member must land on the *same* day,
    and only once that day carries enough capacity for all of them. Typical
    use: two applicants who must attend together, watching a consulate that
    is small enough that grabbing it alone is risky.
    """

    members: List[str] = field(default_factory=list)  # schedule_ids
    consulates: List[int] = field(default_factory=list)
    target: Target = field(default_factory=Target)
    # Minimum number of distinct time slots the day must offer (summed across
    # members' /times.json calls) before booking is attempted. Below this the
    # day is treated as not-yet-safe and left for the next sweep -- with only
    # one slot, both members racing for it would likely mean one succeeds and
    # one fails.
    min_slots: int = 2

    def describe_consulates(self) -> str:
        return ", ".join(consulate_name(c) for c in self.consulates) or "none"


@dataclass
class Account:
    """One AIS login, holding one or more applications."""

    name: str
    email: str
    password: str
    target: Target = field(default_factory=Target)
    applications: List[Application] = field(default_factory=list)
    groups: List[Group] = field(default_factory=list)

    @property
    def auto_discover(self) -> bool:
        """True when no explicit application list was configured."""
        return not self.applications


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    notify_on_no_availability: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class NewRelicConfig:
    """Metric API push target. Free tier is plenty for this volume."""

    license_key: str = ""
    region: str = "us"                 # us | eu | jp
    # auto   -> use system DNS, fall back to DoH if the host is blackholed
    # system -> system DNS only
    # doh    -> always resolve over DNS-over-HTTPS
    dns_mode: str = "auto"
    # Attached to every data point, handy when several deployments report in.
    environment: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.license_key)


@dataclass
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    username: str = ""
    password: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.username and self.password)


@dataclass
class Config:
    accounts: List[Account] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    newrelic: NewRelicConfig = field(default_factory=NewRelicConfig)
    capsolver_api_key: str = ""
    test_mode: bool = True
    poll_interval: int = 300
    http_timeout: int = 30
    consulate_poll_delay: int = 2
    account_poll_delay: int = 10
    max_consecutive_failures: int = 5
    # Discovery (the landing page plus one continue_actions page per
    # application) is all HTML, dwarfs days.json, and carries no availability.
    # What it reports changes at most once, so it is cached this many seconds
    # instead of being repaid every sweep. Dropped early whenever the answer
    # could really have changed: a booking landed, or the session lapsed.
    discovery_interval: int = 900
    # days.json calls per sweep that may be in flight at once. The site is
    # HTTP/1.1 (verified), so there is no multiplexing -- each slot is a
    # separate TCP connection. Kept deliberately small: the point is to stop
    # paying consulate_poll_delay serially, not to flood the host.
    max_concurrent_requests: int = 3
    # How long a pre-warmed booking form stays usable. The appointment page
    # costs ~480ms to fetch and parse (measured); holding its
    # authenticity_token and hidden fields ready means a match only has to pay
    # times.json plus the POST. Refreshed in the background well inside the
    # session's own lifetime.
    booking_prewarm_ttl: int = 120
    # Collapse availability requests that the server itself says are the same
    # resource. days.json for a given (visa class, facility) is byte-identical
    # across sibling schedule_ids -- confirmed by the server returning one
    # ETag for both -- so one poll can answer for every member of that class.
    calendar_sharing: bool = True
    # Sweeps between forced re-observation of those equivalence classes. Guards
    # against a class silently diverging: every Nth sweep polls each member
    # again and re-derives the grouping from fresh ETags.
    calendar_reverify_sweeps: int = 10
    # Fire the booking POST with a guessed time in parallel with times.json,
    # saving one round trip on a match. Off by default: a wrong-time POST is
    # an unvalidated interaction with a live booking system.
    speculative_booking: bool = False
    # Poll faster during the hours slots are actually released. Empty disables
    # it and poll_interval applies around the clock. Same hourly request
    # budget, spent where it can actually win something.
    fast_poll_interval: int = 0
    active_hours: List[Tuple[int, int]] = field(default_factory=list)
    # The site sometimes reports a schedule as completed/locked (or an account
    # as holding no actionable applications) when the response is really just
    # transiently bad. Require "nothing actionable" to hold for this many
    # sweeps in a row before the poller stops, so one glitch cannot end
    # polling for good.
    done_confirmations: int = 5
    # A day can show as available in /days.json and then be empty in
    # /times.json a few seconds later -- someone else took the last slot in
    # between. These retries buy back a few seconds of that race instead of
    # giving up until the next full poll_interval.
    booking_retry_attempts: int = 3
    booking_retry_delay: float = 2.0
    data_dir: Path = Path("data")
    locale: str = "en-ca"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    )

    @property
    def capsolver_enabled(self) -> bool:
        return bool(self.capsolver_api_key)

    @property
    def store_file(self) -> Path:
        """Where the JSON store lives inside the data directory."""
        return self.data_dir / "store.json"

    def total_applications(self) -> int:
        return sum(len(a.applications) for a in self.accounts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_target(raw: Dict[str, Any], base: Target, label: str) -> Target:
    """Build a Target from ``raw``, inheriting anything absent from ``base``."""
    if "consulates" in raw and raw["consulates"] not in (None, ""):
        value = raw["consulates"]
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        try:
            consulates = parse_consulate_list(value, default_all=False)
        except ValueError as exc:
            raise ConfigError(f"{label}.consulates: {exc}") from exc
        if not consulates:
            raise ConfigError(f"{label}.consulates resolved to an empty list")
    else:
        consulates = list(base.consulates)

    earliest = base.earliest
    if "earliest_acceptable_date" in raw:
        earliest = _as_date(raw["earliest_acceptable_date"], f"{label}.earliest_acceptable_date")
    latest = base.latest
    if "latest_acceptable_date" in raw:
        latest = _as_date(raw["latest_acceptable_date"], f"{label}.latest_acceptable_date")
    if earliest and latest and earliest > latest:
        raise ConfigError(
            f"{label}: earliest_acceptable_date {earliest} is after "
            f"latest_acceptable_date {latest}"
        )

    exclusions = list(base.exclusions)
    if "exclusions" in raw:
        exclusions = _as_exclusions(raw["exclusions"], f"{label}.exclusions")

    prefer = base.prefer_earliest
    if "prefer_earliest" in raw:
        prefer = _as_bool(raw["prefer_earliest"], base.prefer_earliest)

    return Target(
        consulates=consulates,
        earliest=earliest,
        latest=latest,
        exclusions=exclusions,
        prefer_earliest=prefer,
    )


def _parse_group(
    raw: Dict[str, Any],
    index: int,
    account_label: str,
    account_target: Target,
    known_ids: List[str],
) -> Group:
    label = f"{account_label}.groups[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label}: expected a mapping, got {type(raw).__name__}")

    members = [str(m).strip() for m in (raw.get("members") or [])]
    if len(members) < 2:
        raise ConfigError(f"{label}.members needs at least 2 schedule ids")
    for m in members:
        if m not in known_ids:
            raise ConfigError(
                f"{label}.members references schedule_id {m!r}, which is not "
                f"listed in {account_label}.applications"
            )
    if len(set(members)) != len(members):
        raise ConfigError(f"{label}.members lists the same schedule_id twice")

    if "consulates" not in raw or raw["consulates"] in (None, ""):
        raise ConfigError(f"{label}.consulates is required")
    value = raw["consulates"]
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    try:
        consulates = parse_consulate_list(value, default_all=False)
    except ValueError as exc:
        raise ConfigError(f"{label}.consulates: {exc}") from exc
    if not consulates:
        raise ConfigError(f"{label}.consulates resolved to an empty list")

    target = _parse_target(raw, account_target, label)
    min_slots = _as_int(raw.get("min_slots"), 2, f"{label}.min_slots")
    if min_slots < 1:
        raise ConfigError(f"{label}.min_slots must be at least 1")

    return Group(
        members=members,
        consulates=consulates,
        target=target,
        min_slots=min_slots,
    )


def _parse_account(raw: Dict[str, Any], defaults: Target, index: int) -> Account:
    label = f"accounts[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label}: expected a mapping, got {type(raw).__name__}")

    email = str(raw.get("email") or "").strip()
    password = str(raw.get("password") or "")
    name = str(raw.get("name") or email or f"account{index}").strip()
    if not email:
        raise ConfigError(f"{label}.email is required (account {name!r})")
    if not password:
        raise ConfigError(
            f"{label}.password is empty for {name!r}. Set it in config.yaml (if you "
            "used ${VAR}, make sure that variable is exported)."
        )

    account_target = _parse_target(raw, defaults, label)

    applications: List[Application] = []
    for j, app_raw in enumerate(raw.get("applications") or []):
        app_label = f"{label}.applications[{j}]"
        if isinstance(app_raw, (str, int)):
            app_raw = {"schedule_id": str(app_raw)}
        if not isinstance(app_raw, dict):
            raise ConfigError(f"{app_label}: expected a mapping or a schedule id")
        schedule_id = str(app_raw.get("schedule_id") or "").strip()
        if not schedule_id.isdigit():
            raise ConfigError(
                f"{app_label}.schedule_id must be the numeric id from the AIS URL, "
                f"got {schedule_id!r}"
            )
        applications.append(
            Application(
                schedule_id=schedule_id,
                label=str(app_raw.get("label") or "").strip(),
                target=_parse_target(app_raw, account_target, app_label),
            )
        )

    known_ids = [a.schedule_id for a in applications]
    groups: List[Group] = []
    for j, group_raw in enumerate(raw.get("groups") or []):
        group = _parse_group(group_raw, j, label, account_target, known_ids)
        groups.append(group)

    # A group takes over its consulates for its members entirely: letting a
    # member also poll the same consulate on its own would let it book alone
    # the instant a day appears, defeating the "must be the same day" promise
    # the group exists to make.
    by_id = {a.schedule_id: a for a in applications}
    for group in groups:
        for member in group.members:
            app = by_id[member]
            clash = set(app.target.consulates) & set(group.consulates)
            if clash:
                names = ", ".join(consulate_name(c) for c in clash)
                raise ConfigError(
                    f"{label}: {app.display_name or member} watches {names} both "
                    f"on its own and via a group -- remove {names} from its own "
                    "consulates list, the group already covers it"
                )

    return Account(
        name=name,
        email=email,
        password=password,
        target=account_target,
        applications=applications,
        groups=groups,
    )


def load_config(path: Optional[Path] = None) -> Config:
    """Read, interpolate and validate the YAML configuration."""
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise ConfigError(
            f"config file {path} not found. Copy config.example.yaml to {path} "
            "and fill it in."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    raw = _interpolate(raw)

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError("defaults: expected a mapping")
    # Consulates default to Toronto only rather than all seven, so an
    # unconfigured run does not fan out across every post.
    seed = Target(consulates=parse_consulate_list("Toronto", default_all=False))
    defaults = _parse_target(defaults_raw, seed, "defaults")

    accounts_raw = raw.get("accounts") or []
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("accounts: at least one account is required")
    accounts = [_parse_account(a, defaults, i) for i, a in enumerate(accounts_raw)]

    seen_emails = set()
    for acc in accounts:
        key = acc.email.lower()
        if key in seen_emails:
            raise ConfigError(f"account email {acc.email} appears more than once")
        seen_emails.add(key)

    tg_raw = raw.get("telegram") or {}
    telegram = TelegramConfig(
        bot_token=str(tg_raw.get("bot_token") or "").strip(),
        chat_id=str(tg_raw.get("chat_id") or "").strip(),
        notify_on_no_availability=_as_bool(tg_raw.get("notify_on_no_availability"), False),
    )

    dash_raw = raw.get("dashboard") or {}
    dashboard = DashboardConfig(
        host=str(dash_raw.get("host") or "0.0.0.0"),
        port=_as_int(dash_raw.get("port"), 8000, "dashboard.port"),
        username=str(dash_raw.get("username") or "").strip(),
        password=str(dash_raw.get("password") or ""),
    )

    nr_raw = raw.get("newrelic") or {}
    newrelic = NewRelicConfig(
        license_key=str(nr_raw.get("license_key") or "").strip(),
        region=str(nr_raw.get("region") or "us").strip().lower(),
        dns_mode=str(nr_raw.get("dns_mode") or "auto").strip().lower(),
        environment=str(nr_raw.get("environment") or "").strip(),
    )
    if newrelic.region not in ("us", "eu", "jp"):
        raise ConfigError(
            f"newrelic.region: {newrelic.region!r} is not one of us, eu, jp"
        )
    if newrelic.dns_mode not in ("auto", "system", "doh"):
        raise ConfigError(
            f"newrelic.dns_mode: {newrelic.dns_mode!r} is not one of "
            "auto, system, doh"
        )

    if raw.get("state_file"):
        print(
            "note: `state_file` is obsolete; the store now lives at "
            "<data_dir>/store.json. Set `data_dir` instead."
        )

    return Config(
        accounts=accounts,
        telegram=telegram,
        dashboard=dashboard,
        newrelic=newrelic,
        capsolver_api_key=str(raw.get("capsolver_api_key") or "").strip(),
        test_mode=_as_bool(raw.get("test_mode"), True),
        poll_interval=_as_int(raw.get("poll_interval"), 300, "poll_interval"),
        http_timeout=_as_int(raw.get("http_timeout"), 30, "http_timeout"),
        consulate_poll_delay=_as_int(raw.get("consulate_poll_delay"), 2, "consulate_poll_delay"),
        account_poll_delay=_as_int(raw.get("account_poll_delay"), 10, "account_poll_delay"),
        max_consecutive_failures=_as_int(
            raw.get("max_consecutive_failures"), 5, "max_consecutive_failures"
        ),
        done_confirmations=_as_int(
            raw.get("done_confirmations"), 5, "done_confirmations"
        ),
        discovery_interval=_as_int(
            raw.get("discovery_interval"), 900, "discovery_interval"
        ),
        max_concurrent_requests=max(1, _as_int(
            raw.get("max_concurrent_requests"), 3, "max_concurrent_requests"
        )),
        booking_prewarm_ttl=_as_int(
            raw.get("booking_prewarm_ttl"), 120, "booking_prewarm_ttl"
        ),
        calendar_sharing=_as_bool(raw.get("calendar_sharing"), True),
        calendar_reverify_sweeps=max(1, _as_int(
            raw.get("calendar_reverify_sweeps"), 10, "calendar_reverify_sweeps"
        )),
        speculative_booking=_as_bool(raw.get("speculative_booking"), False),
        fast_poll_interval=_as_int(
            raw.get("fast_poll_interval"), 0, "fast_poll_interval"
        ),
        active_hours=_as_hour_windows(
            raw.get("active_hours"), "active_hours"
        ),
        booking_retry_attempts=_as_int(
            raw.get("booking_retry_attempts"), 3, "booking_retry_attempts"
        ),
        booking_retry_delay=float(
            _as_int(raw.get("booking_retry_delay"), 2, "booking_retry_delay")
        ),
        data_dir=Path(str(raw.get("data_dir") or "data")),
        locale=str(raw.get("locale") or "en-ca"),
        user_agent=str(raw.get("user_agent") or Config.user_agent),
    )
