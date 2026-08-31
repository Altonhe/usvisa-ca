"""Persistent store shared between the polling worker and the dashboard.

One JSON document under a data directory (``data/store.json`` by default) holds
everything that is not configuration: which applications exist, what each one is
targeting, the last availability seen per consulate, what has been booked, and a
rolling activity log.

The worker writes and the dashboard reads, so every mutation takes a lock.
Writes are atomic (temp file + replace) and the whole document is restored on
startup, which means a container restart shows the last known state immediately
instead of an empty page until the first sweep finishes.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .consulates import consulate_name

STORE_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def mask_email(email: str) -> str:
    """``someone@example.com`` -> ``s*****e@example.com``."""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class ConsulateStatus:
    """Latest poll result for one consulate of one application."""

    facility_id: int
    name: str = ""
    total_days: int = 0
    earliest: Optional[date] = None
    earliest_acceptable: Optional[date] = None
    acceptable_count: int = 0
    error: str = ""
    checked_at: Optional[datetime] = None

    @property
    def summary(self) -> str:
        if self.error:
            return "error"
        if self.total_days == 0:
            return "none"
        if self.acceptable_count:
            return "match"
        return "out-of-window"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "facility_id": self.facility_id,
            "name": self.name or consulate_name(self.facility_id),
            "total_days": self.total_days,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "earliest_acceptable": (
                self.earliest_acceptable.isoformat() if self.earliest_acceptable else None
            ),
            "acceptable_count": self.acceptable_count,
            "error": self.error,
            "checked_at": _iso(self.checked_at),
            "status": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ConsulateStatus":
        return cls(
            facility_id=int(raw["facility_id"]),
            name=raw.get("name", ""),
            total_days=int(raw.get("total_days") or 0),
            earliest=_parse_date(raw.get("earliest")),
            earliest_acceptable=_parse_date(raw.get("earliest_acceptable")),
            acceptable_count=int(raw.get("acceptable_count") or 0),
            error=raw.get("error", ""),
            checked_at=_parse_dt(raw.get("checked_at")),
        )


@dataclass
class ApplicationStatus:
    """One application (schedule id) and everything the dashboard shows for it."""

    account: str
    schedule_id: str
    label: str = ""
    kind: str = "unknown"            # first_time | reschedule | done | unknown
    action_label: str = ""
    target_consulates: List[int] = field(default_factory=list)
    target_window: str = ""
    target_earliest: Optional[date] = None
    target_latest: Optional[date] = None
    current_appointment: Optional[str] = None
    booked_for: Optional[date] = None
    booked_time: str = ""
    booked_consulate: str = ""
    best_found: Optional[date] = None
    best_found_consulate: str = ""
    consulates: Dict[int, ConsulateStatus] = field(default_factory=dict)
    message: str = ""
    last_checked: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        if self.label:
            return f"{self.label} ({self.schedule_id})"
        return self.schedule_id

    @property
    def state(self) -> str:
        """Coarse status used for colour-coding in the UI."""
        if self.booked_for:
            return "booked"
        if self.kind == "done":
            return "inactive"
        if any(c.summary == "match" for c in self.consulates.values()):
            return "match"
        if self.consulates and all(c.summary == "error" for c in self.consulates.values()):
            return "error"
        return "watching"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "schedule_id": self.schedule_id,
            "label": self.label,
            "display_name": self.display_name,
            "kind": self.kind,
            "action_label": self.action_label,
            "target_consulate_ids": list(self.target_consulates),
            "target_consulates": [consulate_name(c) for c in self.target_consulates],
            "target_window": self.target_window,
            "target_earliest": self.target_earliest.isoformat() if self.target_earliest else None,
            "target_latest": self.target_latest.isoformat() if self.target_latest else None,
            "current_appointment": self.current_appointment,
            "booked_for": self.booked_for.isoformat() if self.booked_for else None,
            "booked_time": self.booked_time,
            "booked_consulate": self.booked_consulate,
            "best_found": self.best_found.isoformat() if self.best_found else None,
            "best_found_consulate": self.best_found_consulate,
            "consulates": [self.consulates[k].to_dict() for k in sorted(self.consulates)],
            "message": self.message,
            "last_checked": _iso(self.last_checked),
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ApplicationStatus":
        app = cls(
            account=raw.get("account", ""),
            schedule_id=str(raw["schedule_id"]),
            label=raw.get("label", ""),
            kind=raw.get("kind", "unknown"),
            action_label=raw.get("action_label", ""),
            target_consulates=[int(c) for c in raw.get("target_consulate_ids") or []],
            target_window=raw.get("target_window", ""),
            target_earliest=_parse_date(raw.get("target_earliest")),
            target_latest=_parse_date(raw.get("target_latest")),
            current_appointment=raw.get("current_appointment"),
            booked_for=_parse_date(raw.get("booked_for")),
            booked_time=raw.get("booked_time", ""),
            booked_consulate=raw.get("booked_consulate", ""),
            best_found=_parse_date(raw.get("best_found")),
            best_found_consulate=raw.get("best_found_consulate", ""),
            message=raw.get("message", ""),
            last_checked=_parse_dt(raw.get("last_checked")),
        )
        for c in raw.get("consulates") or []:
            try:
                status = ConsulateStatus.from_dict(c)
            except (KeyError, TypeError, ValueError):
                continue
            app.consulates[status.facility_id] = status
        return app


@dataclass
class AccountStatus:
    name: str
    email_masked: str = ""
    applications: Dict[str, ApplicationStatus] = field(default_factory=dict)
    error: str = ""
    pending_discovery: bool = False
    last_login: Optional[datetime] = None
    last_checked: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        apps = [self.applications[k].to_dict() for k in sorted(self.applications)]
        return {
            "name": self.name,
            "email_masked": self.email_masked,
            "application_count": len(apps),
            "applications": apps,
            "error": self.error,
            "pending_discovery": self.pending_discovery,
            "last_login": _iso(self.last_login),
            "last_checked": _iso(self.last_checked),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AccountStatus":
        acc = cls(
            name=raw.get("name", ""),
            email_masked=raw.get("email_masked", ""),
            error=raw.get("error", ""),
            pending_discovery=bool(raw.get("pending_discovery")),
            last_login=_parse_dt(raw.get("last_login")),
            last_checked=_parse_dt(raw.get("last_checked")),
        )
        for a in raw.get("applications") or []:
            try:
                app = ApplicationStatus.from_dict(a)
            except (KeyError, TypeError, ValueError):
                continue
            app.account = acc.name
            acc.applications[app.schedule_id] = app
        return acc


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Thread-safe JSON-backed store for runtime state."""

    def __init__(self, path: Optional[Path] = None, log_limit: int = 300):
        self._lock = threading.RLock()
        self._accounts: Dict[str, AccountStatus] = {}
        self.path = Path(path) if path else None
        self.log_limit = log_limit
        self.started_at = _now()
        self.sweep_count = 0
        self.last_sweep_started: Optional[datetime] = None
        self.last_sweep_finished: Optional[datetime] = None
        self.next_sweep_at: Optional[datetime] = None
        self.test_mode = True
        self.worker_status = "starting"
        self.worker_error = ""
        self.log: List[str] = []
        self.restored_from: Optional[str] = None

    # -- registration ----------------------------------------------------

    def register_account(self, name: str, email: str) -> AccountStatus:
        with self._lock:
            acc = self._accounts.get(name)
            if acc is None:
                acc = AccountStatus(name=name)
                self._accounts[name] = acc
            # Always refresh the mask: the address may have changed in config.
            acc.email_masked = mask_email(email)
            return acc

    def register_application(
        self, account: str, schedule_id: str, label: str, target
    ) -> ApplicationStatus:
        with self._lock:
            acc = self._accounts[account]
            app = acc.applications.get(schedule_id)
            if app is None:
                app = ApplicationStatus(account=account, schedule_id=schedule_id)
                acc.applications[schedule_id] = app
            app.label = label or app.label
            # Config always wins over whatever was restored from disk.
            app.target_consulates = list(target.consulates)
            app.target_window = target.describe_window()
            app.target_earliest = target.earliest
            app.target_latest = target.latest
            return app

    # -- mutation --------------------------------------------------------

    def update_application(self, account: str, schedule_id: str, **fields) -> None:
        with self._lock:
            acc = self._accounts.get(account)
            if acc is None:
                return
            app = acc.applications.get(schedule_id)
            if app is None:
                return
            for key, value in fields.items():
                setattr(app, key, value)
            app.last_checked = _now()
            acc.last_checked = app.last_checked

    def update_consulate(
        self, account: str, schedule_id: str, status: ConsulateStatus
    ) -> None:
        with self._lock:
            acc = self._accounts.get(account)
            if acc is None:
                return
            app = acc.applications.get(schedule_id)
            if app is None:
                return
            status.name = status.name or consulate_name(status.facility_id)
            status.checked_at = _now()
            app.consulates[status.facility_id] = status

    def set_account_error(self, account: str, error: str) -> None:
        with self._lock:
            if account in self._accounts:
                self._accounts[account].error = error
                self._accounts[account].last_checked = _now()

    def set_pending_discovery(self, account: str, pending: bool) -> None:
        with self._lock:
            if account in self._accounts:
                self._accounts[account].pending_discovery = pending

    def mark_login(self, account: str) -> None:
        with self._lock:
            if account in self._accounts:
                self._accounts[account].last_login = _now()
                self._accounts[account].error = ""

    def prune_applications(self, account: str, keep: List[str]) -> None:
        """Drop applications that are neither configured nor present on the site."""
        with self._lock:
            acc = self._accounts.get(account)
            if acc is None:
                return
            for sid in list(acc.applications):
                if sid not in keep:
                    del acc.applications[sid]

    def prune_accounts(self, keep: List[str]) -> None:
        """Drop accounts that are no longer in the configuration."""
        with self._lock:
            for name in list(self._accounts):
                if name not in keep:
                    del self._accounts[name]

    def add_log(self, message: str) -> None:
        stamped = f"{_now().strftime('%Y-%m-%d %H:%M:%S')}Z  {message}"
        with self._lock:
            self.log.append(stamped)
            if len(self.log) > self.log_limit:
                del self.log[: len(self.log) - self.log_limit]

    # -- reads -----------------------------------------------------------

    def booked_schedule_ids(self) -> List[str]:
        with self._lock:
            return [
                app.schedule_id
                for acc in self._accounts.values()
                for app in acc.applications.values()
                if app.booked_for
            ]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            accounts = [self._accounts[k].to_dict() for k in sorted(self._accounts)]
            applications = [app for a in accounts for app in a["applications"]]
            active = [a for a in applications if a["state"] != "inactive"]
            return {
                "version": STORE_VERSION,
                "generated_at": _iso(_now()),
                "started_at": _iso(self.started_at),
                "restored_from": self.restored_from,
                "test_mode": self.test_mode,
                "worker_status": self.worker_status,
                "worker_error": self.worker_error,
                "sweep_count": self.sweep_count,
                "last_sweep_started": _iso(self.last_sweep_started),
                "last_sweep_finished": _iso(self.last_sweep_finished),
                "next_sweep_at": _iso(self.next_sweep_at),
                "totals": {
                    "accounts": len(accounts),
                    "applications": len(applications),
                    "active": len(active),
                    "booked": len([a for a in active if a["state"] == "booked"]),
                    "matches": len([a for a in active if a["state"] == "match"]),
                },
                "accounts": accounts,
                "log": list(reversed(self.log[-60:])),
            }

    # -- persistence -----------------------------------------------------

    def save(self) -> bool:
        """Write the store atomically. Returns False if it could not be written."""
        if not self.path:
            return False
        with self._lock:
            payload = self.snapshot()
            payload["log"] = list(self.log)      # persist in chronological order
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError as exc:
            self.add_log(f"could not persist store: {exc}")
            return False

    def load(self) -> bool:
        """Restore a previous store. Returns True when something was restored.

        A missing or corrupt file is not an error: the store simply starts empty
        and is rewritten on the next save.
        """
        if not self.path or not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.add_log(f"ignoring unreadable store {self.path}: {exc}")
            return False
        if not isinstance(raw, dict):
            self.add_log(f"ignoring malformed store {self.path}")
            return False
        if raw.get("version") != STORE_VERSION:
            self.add_log(
                f"ignoring store written by another version "
                f"(found {raw.get('version')!r}, expected {STORE_VERSION})"
            )
            return False

        with self._lock:
            for acc_raw in raw.get("accounts") or []:
                try:
                    acc = AccountStatus.from_dict(acc_raw)
                except (KeyError, TypeError, ValueError):
                    continue
                if acc.name:
                    self._accounts[acc.name] = acc
            self.sweep_count = int(raw.get("sweep_count") or 0)
            self.last_sweep_started = _parse_dt(raw.get("last_sweep_started"))
            self.last_sweep_finished = _parse_dt(raw.get("last_sweep_finished"))
            restored_log = raw.get("log") or []
            if isinstance(restored_log, list):
                self.log = [str(x) for x in restored_log][-self.log_limit:]
            self.restored_from = raw.get("generated_at")
        return bool(self._accounts)
