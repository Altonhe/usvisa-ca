"""Metric samples derived from the store.

The store holds *dates*; this module converts them to the numbers you actually
want to graph, at the moment they are requested. That matters for the day
counters: ``earliest_slot_days`` is recomputed against today on every emit, so it
decays on its own instead of freezing between polls.

One neutral :class:`Sample` list feeds two serialisers:

* :func:`to_newrelic` -- payload for the New Relic Metric API (the real destination)
* :func:`to_prometheus` -- exposition text on ``GET /metrics``, kept only so you
  can ``curl`` the current values without waiting for ingestion

Attributes are the dimensions: ``account``, ``application`` (plus ``schedule_id``
as a stable identity) and ``consulate``.

Samples are **omitted rather than zeroed** when there is nothing to report. A
consulate with no availability emits no ``earliest_slot_days`` sample at all,
because 0 would read as "a slot is available today". Use ``consulate_status`` and
``consulate_poll_ok`` to tell "nothing free" from "request failed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

PREFIX = "usvisa"


@dataclass
class Sample:
    """One gauge reading with its dimensions."""

    name: str                      # snake_case, no prefix
    value: float
    help: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)

    @property
    def prometheus_name(self) -> str:
        return f"{PREFIX}_{self.name}"

    @property
    def newrelic_name(self) -> str:
        return f"{PREFIX}.{self.name}"


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(snapshot: Dict[str, Any], today: Optional[date] = None) -> List[Sample]:
    """Turn a store snapshot into gauge samples."""
    today = today or datetime.now(timezone.utc).date()
    out: List[Sample] = []

    def add(name: str, value: Any, help_text: str, **attrs: Any) -> None:
        out.append(Sample(
            name=name,
            value=float(value),
            help=help_text,
            attributes={k: v for k, v in attrs.items() if v not in (None, "")},
        ))

    # -- process ---------------------------------------------------------
    add("up", 1, "Always 1; presence means the watcher reported in.")
    add("test_mode", bool(snapshot.get("test_mode")),
        "1 when test_mode is on and bookings are never submitted.")
    add("worker_running", snapshot.get("worker_status") == "running",
        "1 when the polling thread is alive.")
    add("sweeps_total", snapshot.get("sweep_count") or 0,
        "Completed polling sweeps since first start.")

    totals = snapshot.get("totals") or {}
    for key in ("accounts", "applications", "active", "matches", "booked"):
        add(f"{key}_count", totals.get(key) or 0,
            f"Number of {key} known to the watcher.")

    # -- per account -----------------------------------------------------
    for account in snapshot.get("accounts") or []:
        acc = account.get("name") or "unknown"
        add("account_login_ok", not account.get("error"),
            "1 when the last sign-in for this account succeeded.", account=acc)
        add("account_applications", account.get("application_count") or 0,
            "Applications tracked for this account.", account=acc)

        # -- per application ---------------------------------------------
        for app in account.get("applications") or []:
            name = app.get("label") or app.get("schedule_id") or "unknown"
            ident = dict(account=acc, application=name,
                         schedule_id=app.get("schedule_id"))

            # Single sample carrying the current state, not one per state.
            add("application_state", 1,
                "Always 1; read the state attribute for the current status.",
                **ident, state=app.get("state"))
            add("application_booked", bool(app.get("booked_for")),
                "1 once an appointment has been booked.", **ident)

            deadline = _parse_date(app.get("target_latest"))
            if deadline:
                add("target_deadline_days", (deadline - today).days,
                    "Days until latest_acceptable_date; negative once it passes.",
                    **ident)

            booked = _parse_date(app.get("booked_for"))
            if booked:
                add("booked_slot_days", (booked - today).days,
                    "Days from today to the booked appointment.", **ident)

            staleness = _epoch(app.get("last_checked"))
            if staleness is not None:
                add("seconds_since_check",
                    max(0, int(datetime.now(timezone.utc).timestamp() - staleness)),
                    "Seconds since this application was last polled.", **ident)

            # -- per consulate -------------------------------------------
            for cons in app.get("consulates") or []:
                loc = dict(ident, consulate=cons.get("name"))
                status = cons.get("status")

                add("consulate_poll_ok", status != "error",
                    "1 when the last availability request succeeded.", **loc)
                add("consulate_status", 1,
                    "Always 1; read the status attribute "
                    "(match/none/out-of-window/error).", **loc, status=status)

                if status == "error":
                    continue        # nothing was measured, so measure nothing

                add("available_days", cons.get("total_days") or 0,
                    "Days offered by this consulate at the last poll.", **loc)
                add("acceptable_days", cons.get("acceptable_count") or 0,
                    "Offered days falling inside the target window.", **loc)

                earliest = _parse_date(cons.get("earliest"))
                if earliest:
                    add("earliest_slot_days", (earliest - today).days,
                        "Days from today to the earliest slot offered, "
                        "regardless of the target window.", **loc)

                acceptable = _parse_date(cons.get("earliest_acceptable"))
                if acceptable:
                    add("earliest_acceptable_slot_days", (acceptable - today).days,
                        "Days from today to the earliest slot satisfying the "
                        "target window.", **loc)

    return out


# ---------------------------------------------------------------------------
# Groups: joint "must land on the same day" constraints
# ---------------------------------------------------------------------------

@dataclass
class GroupRecord:
    """One /days.json + /times.json check for a group at one consulate.

    Built by the worker after each ``_sweep_group_facility`` call and handed
    to :func:`collect_groups`. Not persisted in the store -- this is a report
    of the last check only, meant to answer "how close is this joint
    constraint to being satisfiable" on the dashboard.
    """

    account: str
    members: str            # display labels, joined with " + "
    consulate: str
    common_days: int
    earliest_common: Optional[date]
    slots_found: int
    min_slots: int
    error: str = ""

    @property
    def ready(self) -> bool:
        return not self.error and self.common_days > 0 and self.slots_found >= self.min_slots


def collect_groups(
    records: List[GroupRecord], today: Optional[date] = None
) -> List[Sample]:
    """Turn the latest group checks into gauge samples.

    Mirrors the per-consulate samples in :func:`collect`, but keyed on the
    joint constraint (``members``, ``consulate``) rather than a single
    application, since a group's availability only means something once every
    member is accounted for.
    """
    today = today or datetime.now(timezone.utc).date()
    out: List[Sample] = []

    def add(name: str, value: Any, help_text: str, **attrs: Any) -> None:
        out.append(Sample(
            name=name,
            value=float(value),
            help=help_text,
            attributes={k: v for k, v in attrs.items() if v not in (None, "")},
        ))

    for rec in records:
        loc = dict(account=rec.account, members=rec.members, consulate=rec.consulate)

        add("group_poll_ok", not rec.error,
            "1 when the last joint availability check succeeded.", **loc)
        if rec.error:
            continue

        add("group_common_days", rec.common_days,
            "Days every member of this group can attend, at the last check.",
            **loc)
        add("group_slots_found", rec.slots_found,
            "Total distinct time slots found on the earliest common day.",
            **loc)
        add("group_min_slots", rec.min_slots,
            "Slots required before the group books; from config.", **loc)
        add("group_ready", rec.ready,
            "1 when the earliest common day has enough slots to book "
            "every member.", **loc)

        if rec.earliest_common:
            add("group_earliest_common_days", (rec.earliest_common - today).days,
                "Days from today to the earliest day every member of this "
                "group can attend.", **loc)

    return out


# ---------------------------------------------------------------------------
# New Relic Metric API
# ---------------------------------------------------------------------------

def to_newrelic(
    samples: List[Sample],
    common_attributes: Optional[Dict[str, Any]] = None,
    timestamp_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Build a Metric API payload.

    https://docs.newrelic.com/docs/data-apis/ingest-apis/metric-api/report-metrics-metric-api/
    """
    if timestamp_ms is None:
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return [{
        "common": {
            "timestamp": timestamp_ms,
            "attributes": dict(common_attributes or {}),
        },
        "metrics": [
            {
                "name": s.newrelic_name,
                "type": "gauge",
                "value": s.value,
                "attributes": s.attributes,
            }
            for s in samples
        ],
    }]


# ---------------------------------------------------------------------------
# Prometheus exposition (debug convenience only)
# ---------------------------------------------------------------------------

def _escape(value: str) -> str:
    return (
        str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def to_prometheus(samples: List[Sample]) -> str:
    lines: List[str] = []
    declared: set[str] = set()
    for s in samples:
        if s.prometheus_name not in declared:
            lines.append(f"# HELP {s.prometheus_name} {s.help}")
            lines.append(f"# TYPE {s.prometheus_name} gauge")
            declared.add(s.prometheus_name)
        labels = ",".join(
            f'{k}="{_escape(v)}"' for k, v in s.attributes.items()
        )
        suffix = f"{{{labels}}}" if labels else ""
        value = int(s.value) if float(s.value).is_integer() else s.value
        lines.append(f"{s.prometheus_name}{suffix} {value}")
    return "\n".join(lines) + "\n"


def render_metrics(snapshot: Dict[str, Any], today: Optional[date] = None) -> str:
    """Convenience wrapper used by ``GET /metrics``."""
    return to_prometheus(collect(snapshot, today=today))
