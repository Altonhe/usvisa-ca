"""Polling engine.

Runs in a background thread next to the dashboard.  One sweep visits every
account, then every application on that account, then every consulate that
application targets, and books the best acceptable slot it finds.

Design notes worth knowing:

* An empty availability list is *not* an error.  Four of the seven Canadian
  posts routinely return zero days, and conflating that with a failed request
  made the old implementation unreadable.
* Each account keeps its own session.  A login failure isolates to that account
  instead of stopping the whole service.
* ``test_mode`` assembles the booking request and logs it, but never sends it.
"""

from __future__ import annotations

import threading
import time
import traceback
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ais_client import (AisClient, AisError, LoginFailed, Schedule,
                         SessionExpired, Slot, TransientNetworkError,
                         parse_site_date)
from .capsolver import CapSolver, CapSolverError
from .config import Account, Application, Config, Group, Target
from .calendars import CalendarCache
from .consulates import consulate_local_hour, consulate_name
from .metrics import GroupRecord, collect, collect_groups
from .newrelic import NewRelicClient
from .notifier import TelegramNotifier
from .store import ConsulateStatus, Store


class Worker:
    def __init__(
        self,
        config: Config,
        store: Store,
        notifier: TelegramNotifier,
        newrelic: Optional[NewRelicClient] = None,
    ):
        self.config = config
        self.store = store
        self.notifier = notifier
        self.newrelic = newrelic
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._clients: Dict[str, AisClient] = {}
        self._failures: Dict[str, int] = {}
        # schedule_id -> True once booked, so a restart or a later sweep does
        # not try to rebook something that is already settled.
        self._booked: Dict[str, bool] = {}
        # Latest joint-availability check per (account, members, consulate),
        # rebuilt every sweep and pushed to New Relic alongside the regular
        # per-application metrics. Not persisted -- it is a live report, not
        # history.
        self._group_records: Dict[Tuple[str, str, int], GroupRecord] = {}
        # account -> True if the current session was resumed from disk rather
        # than signed in fresh this run. Dashboard-only diagnostic.
        self._session_resumed: Dict[str, bool] = {}
        # account -> (monotonic timestamp, resolved (Application, Schedule) pairs).
        # Discovery costs one landing page plus one continue_actions page per
        # application -- all HTML, all far larger than days.json, and none of it
        # carries availability. What it reports (is this application still
        # actionable, is it already booked) changes at most once, so paying for
        # it every sweep was the single largest waste in the polling loop.
        self._discovery_cache: Dict[str, Tuple[float, List[Tuple[Application, Schedule]]]] = {}
        # Learns, from the server's own ETags, which (schedule_id, facility)
        # pairs are the same calendar, so one request can answer for several
        # applications instead of asking the identical question per applicant.
        self._calendars = CalendarCache(
            enabled=config.calendar_sharing,
            reverify_sweeps=config.calendar_reverify_sweeps,
        )
        # Consecutive sweeps that saw "nothing actionable any more". The site
        # intermittently reports a schedule as completed/locked when it is
        # really just a bad response, so we require this to hold for several
        # sweeps in a row before stopping -- a single glitch must not end
        # polling for good.
        self._done_streak: int = 0

    # -- logging ---------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)
        self.store.add_log(message)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.store.test_mode = self.config.test_mode
        self._seed_state()
        for sid in self.store.booked_schedule_ids():
            self._booked[sid] = True
        if self._booked:
            self.log(f"restored {len(self._booked)} previously booked application(s)")

        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self._thread.start()

    def _seed_state(self) -> None:
        """Publish the configured targets before any network call happens.

        Account count, applications per account, target consulates and target
        date window are all known from config alone.  Seeding them here means
        the dashboard is useful immediately -- and stays useful when a login
        fails, which is exactly when you need to look at it.
        """
        for account in self.config.accounts:
            self.store.register_account(account.name, account.email)
            self.store.set_pending_discovery(account.name, account.auto_discover)
            group_consulates = self._group_consulates_by_member(account)
            for app in account.applications:
                display_target = app.target
                extra = group_consulates.get(app.schedule_id)
                if extra:
                    display_target = replace(
                        app.target,
                        consulates=list(app.target.consulates) + extra,
                    )
                status = self.store.register_application(
                    account.name, app.schedule_id, app.label, display_target
                )
                if status.last_checked is None:
                    self.store.update_application(
                        account.name, app.schedule_id, message="not polled yet"
                    )

        # Forget accounts and applications the configuration no longer mentions.
        self.store.prune_accounts([a.name for a in self.config.accounts])
        for account in self.config.accounts:
            if not account.auto_discover:
                self.store.prune_applications(
                    account.name, [a.schedule_id for a in account.applications]
                )

    @staticmethod
    def _group_consulates_by_member(account: Account) -> Dict[str, List[int]]:
        """schedule_id -> consulates it watches via a group, for display only."""
        out: Dict[str, List[int]] = {}
        for group in account.groups:
            for member in group.members:
                out.setdefault(member, []).extend(group.consulates)
        return out


    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def dashboard_context(self) -> Dict[str, object]:
        """Extra, non-persisted state the dashboard shows alongside the store.

        Group checks and session-resume status only make sense as "since this
        process started" facts, so they live on the worker instead of the
        store: restarting the process is exactly the event that resets them.
        """
        groups = [
            {
                "account": rec.account,
                "members": rec.members,
                "consulate": rec.consulate,
                "common_days": rec.common_days,
                "earliest_common": rec.earliest_common.isoformat() if rec.earliest_common else None,
                "slots_found": rec.slots_found,
                "min_slots": rec.min_slots,
                "error": rec.error,
                "ready": rec.ready,
            }
            for rec in sorted(
                self._group_records.values(), key=lambda r: (r.account, r.members, r.consulate)
            )
        ]
        sessions = []
        for account in self.config.accounts:
            path = self._session_path(account.name)
            sessions.append({
                "account": account.name,
                "resumed": self._session_resumed.get(account.name, False),
                "saved": path.exists(),
                "path": str(path),
            })
        return {"groups": groups, "sessions": sessions}

    # -- main loop -------------------------------------------------------

    def _run(self) -> None:
        self.store.worker_status = "running"
        total_apps = sum(
            len(a.applications) if a.applications else 1 for a in self.config.accounts
        )
        self.notifier.startup(len(self.config.accounts), total_apps, self.config.test_mode)

        while not self._stop.is_set():
            self.store.sweep_count += 1
            self.store.last_sweep_started = datetime.now(timezone.utc)
            self.log(f"--- sweep #{self.store.sweep_count} ---")
            try:
                self._sweep()
                self.store.worker_error = ""
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self.store.worker_error = str(exc)
                self.log(f"sweep crashed: {exc}")
                traceback.print_exc()

            self.store.last_sweep_finished = datetime.now(timezone.utc)
            interval = self._current_interval()
            self.store.next_sweep_at = self.store.last_sweep_finished + timedelta(
                seconds=interval
            )
            self.store.save()
            self._emit_metrics()

            if self._all_booked():
                booked = [
                    app
                    for acc in self.store.snapshot()["accounts"]
                    for app in acc["applications"]
                    if app["state"] == "booked"
                ]
                self.log(
                    f"{len(booked)} application(s) booked and nothing else is "
                    "actionable; polling stops here"
                )
                self.store.worker_status = "finished"
                return

            if self._all_done():
                # Reached via "inactive / no scheduling action", which the site
                # reports transiently. Require it to hold for several sweeps in
                # a row before believing it.
                self._done_streak += 1
                needed = max(1, self.config.done_confirmations)
                if self._done_streak < needed:
                    self.log(
                        "nothing actionable this sweep "
                        f"({self._done_streak}/{needed}); the site sometimes "
                        "reports this transiently, so re-checking before "
                        "stopping"
                    )
                else:
                    self.log(
                        "no application offers a scheduling action any more "
                        f"for {needed} sweep(s); polling stops here"
                    )
                    self.store.worker_status = "finished"
                    return
            else:
                # Something is actionable again: the terminal state was not
                # real, so forget the streak.
                self._done_streak = 0

            self.log(f"sleeping {interval}s")
            if self._stop.wait(interval):
                break

        self.store.worker_status = "stopped"

    def _watched_consulates(self) -> List[int]:
        """Every facility the configuration actually watches, in config order."""
        found: List[int] = []
        for account in self.config.accounts:
            targets: List[Target] = [account.target]
            targets.extend(a.target for a in account.applications)
            for group in account.groups:
                targets.append(group.target)
                found.extend(group.consulates)
            for target in targets:
                found.extend(target.consulates)

        seen = set()
        unique: List[int] = []
        for facility_id in found:
            if facility_id not in seen:
                seen.add(facility_id)
                unique.append(facility_id)
        return unique

    def _current_interval(self) -> int:
        """Seconds to wait before the next sweep.

        ``poll_interval`` normally, or ``fast_poll_interval`` while any watched
        consulate is inside one of ``active_hours``.

        The hours are read **at the consulate**, not on this machine. Slots are
        released by the post, so its business hours are the thing worth
        tracking, and Canada spans 4.5 timezones -- 08:00 in Toronto is 05:00 in
        Vancouver. Judging that by the container's own clock would be right for
        at most one post.

        A sweep covers every account at once and cannot be sped up for one
        consulate alone, so the window is treated as open when *any* watched
        post is inside it.
        """
        fast = self.config.fast_poll_interval
        if fast <= 0 or not self.config.active_hours:
            return self.config.poll_interval

        for facility_id in self._watched_consulates():
            hour = consulate_local_hour(facility_id)
            for start, end in self.config.active_hours:
                inside = (
                    start <= hour < end if start < end
                    else hour >= start or hour < end
                )
                if inside:
                    return fast
        return self.config.poll_interval

    def _prewarm_bookings(self, client: AisClient, account: Account) -> None:
        """Keep a parsed booking form on hand for every actionable application.

        Runs between sweeps, never on the hot path. Failures are swallowed
        because this is purely an optimisation -- if the form is missing the
        booking path just fetches it inline, exactly as it used to.
        """
        if not hasattr(client, "prewarm_booking"):
            return
        cached = self._discovery_cache.get(account.name)
        if cached is None:
            return
        for app, schedule in cached[1]:
            if self._stop.is_set():
                return
            if self._booked.get(app.schedule_id) or not schedule.actionable:
                continue
            try:
                client.prewarm_booking(
                    app.schedule_id, ttl=self.config.booking_prewarm_ttl
                )
            except Exception as exc:  # noqa: BLE001 - never critical
                self.log(f"[{account.name}] prewarm skipped: {exc}")

    def _emit_metrics(self) -> None:
        """Push one batch of gauges after each sweep.

        Wrapped so a monitoring outage can never interrupt polling.
        """
        if not (self.newrelic and self.newrelic.enabled):
            return
        try:
            samples = collect(self.store.snapshot())
            samples += collect_groups(list(self._group_records.values()))
            self.newrelic.send(samples)
        except Exception as exc:  # noqa: BLE001 - metrics are never critical
            self.log(f"metrics push failed (ignored): {exc}")

    def _all_done(self) -> bool:
        """True only when every watched application has reached a terminal state.

        An account we could not sign into registers zero applications, which
        must not be mistaken for "everything is booked" -- that would silently
        stop polling after a single transient login failure.
        """
        snapshot = self.store.snapshot()
        if not snapshot["accounts"]:
            return False
        if any(acc["error"] for acc in snapshot["accounts"]):
            return False
        applications = [
            app for acc in snapshot["accounts"] for app in acc["applications"]
        ]
        if not applications:
            return False
        return all(app["state"] in ("inactive", "booked") for app in applications)

    def _all_booked(self) -> bool:
        """True when every watched application is booked.

        This is a genuinely terminal state -- a booked appointment does not
        revert -- so it needs no confirmation streak, unlike the "inactive /
        no scheduling action" case, which the site reports transiently.
        """
        snapshot = self.store.snapshot()
        if not snapshot["accounts"]:
            return False
        if any(acc["error"] for acc in snapshot["accounts"]):
            return False
        applications = [
            app for acc in snapshot["accounts"] for app in acc["applications"]
        ]
        if not applications:
            return False
        return all(app["state"] == "booked" for app in applications)

    # -- per account -----------------------------------------------------

    def _sweep(self) -> None:
        for index, account in enumerate(self.config.accounts):
            if self._stop.is_set():
                return
            self.store.register_account(account.name, account.email)
            try:
                self._sweep_account(account)
                self._failures[account.name] = 0
            except SessionExpired as exc:
                # Expected periodically; not a failure worth counting or alerting.
                self.log(f"[{account.name}] {exc}")
                self._clients.pop(account.name, None)
                self._discard_saved_session(account.name)
                self._invalidate_discovery(account.name)
            except TransientNetworkError as exc:
                # The network blipped, not the session. Keep the cookies and try
                # again next sweep; a full traceback here is pure noise.
                count = self._failures.get(account.name, 0) + 1
                self._failures[account.name] = count
                self.log(
                    f"[{account.name}] network error, keeping the session: {exc} "
                    f"(consecutive: {count})"
                )
                self.store.set_account_error(
                    account.name, f"network error: {exc}"
                )
                if count == self.config.max_consecutive_failures:
                    self.notifier.account_error(
                        account.name,
                        f"{count} consecutive network errors reaching the site. "
                        f"Still retrying every {self.config.poll_interval}s.",
                    )
            except (LoginFailed, CapSolverError) as exc:
                self._handle_account_failure(account, f"login failed: {exc}")
            except AisError as exc:
                self._handle_account_failure(account, str(exc))
            except Exception as exc:  # noqa: BLE001 - isolate to this account
                self._handle_account_failure(account, f"unexpected error: {exc}")
                traceback.print_exc()

            if index < len(self.config.accounts) - 1:
                if self._stop.wait(self.config.account_poll_delay):
                    return

    def _handle_account_failure(self, account: Account, reason: str) -> None:
        count = self._failures.get(account.name, 0) + 1
        self._failures[account.name] = count
        self.log(f"[{account.name}] {reason} (consecutive failures: {count})")
        self.store.set_account_error(account.name, reason)
        # A fresh session invalidates the CSRF token in any pre-warmed form and
        # the discovery that was read with the old one.
        self._invalidate_discovery(account.name)
        # Force a fresh session next sweep.
        self._clients.pop(account.name, None)
        self._discard_saved_session(account.name)
        if count == self.config.max_consecutive_failures:
            self.notifier.account_error(
                account.name,
                f"{reason}\n{count} consecutive failures; still retrying every "
                f"{self.config.poll_interval}s",
            )

    def _session_path(self, account_name: str) -> Path:
        """Where this account's cookies + landing_url are persisted.

        Kept next to store.json in the same data_dir/volume, so a container
        restart can resume the session instead of always signing in fresh --
        re-authenticating on every restart is unnecessary load on the site and
        the pattern most likely to arm reCAPTCHA.
        """
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", account_name).strip("_") or "account"
        return self.config.data_dir / "sessions" / f"{safe}.json"

    def _discard_saved_session(self, account_name: str) -> None:
        """Remove a saved session known to be invalid.

        Best-effort: leaving a stale file behind is harmless (the next
        _client_for call will just find it does not resume and re-login), so
        any error here is swallowed.
        """
        try:
            self._session_path(account_name).unlink(missing_ok=True)
        except OSError:
            pass

    def _client_for(self, account: Account) -> AisClient:
        """Reuse a logged-in session, creating one on demand.

        A brand-new client first tries to resume the session saved on disk
        (survives a process/container restart) before falling back to a full
        sign-in.
        """
        client = self._clients.get(account.name)
        if client is not None:
            return client

        solver = None
        if self.config.capsolver_enabled:
            solver = CapSolver(
                self.config.capsolver_api_key,
                logger=lambda m: self.log(f"[{account.name}] {m}"),
            )
        client = AisClient(
            email=account.email,
            password=account.password,
            capsolver=solver,
            locale=self.config.locale,
            user_agent=self.config.user_agent,
            timeout=self.config.http_timeout,
            request_delay=self.config.consulate_poll_delay,
            logger=lambda m: self.log(f"[{account.name}] {m}"),
        )
        session_path = self._session_path(account.name)
        resumed = False
        if client.load_cookies(session_path):
            resumed = client.resume_session()
            if not resumed:
                self.log(f"[{account.name}] saved session is no longer valid, signing in")
        else:
            self.log(f"[{account.name}] no saved session found, signing in")

        if not resumed:
            client.login()

        client.save_cookies(session_path)
        self._session_resumed[account.name] = resumed
        self.store.mark_login(account.name)
        self._clients[account.name] = client
        return client

    def _sweep_account(self, account: Account) -> None:
        client = self._client_for(account)
        applications = self._resolve_applications(account, client)
        if not applications:
            self.log(f"[{account.name}] nothing actionable on this account")
            return

        by_id = {app.schedule_id: (app, sched) for app, sched in applications}
        for group in account.groups:
            if self._stop.is_set():
                return
            self._sweep_group(account, client, group, by_id)

        checked: List[str] = []
        pending = [
            (app, sched) for app, sched in applications
            if not self._booked.get(app.schedule_id)
        ]
        # One batched, deduplicated, concurrent fetch for the whole account
        # rather than a delayed request per application per consulate.
        pairs = [
            (app.schedule_id, facility_id)
            for app, _ in pending
            for facility_id in app.target.consulates
        ]
        calendars = self._fetch_calendars(account, client, pairs)

        for app, schedule in pending:
            if self._stop.is_set():
                return
            checked.append(app.display_name)
            self._sweep_application(account, client, app, schedule, calendars)

        if checked:
            self.notifier.no_availability(account.name, checked)
        # Reload the booking form for next time, off the hot path, so a match
        # on the next sweep only has to pay times.json plus the POST.
        self._prewarm_bookings(client, account)

    def _resolve_applications(
        self, account: Account, client: AisClient
    ) -> List[Tuple[Application, Schedule]]:
        """Pair applications with their live state, reusing a fresh discovery.

        Discovery is expensive and nearly static: one landing page plus one
        ``continue_actions`` page per application, all HTML, none of it
        carrying availability. Re-running it every sweep was costing more
        requests than the availability polling it exists to set up, so the
        result is cached for ``discovery_interval`` seconds.

        The cache is dropped whenever the answer could actually have changed
        -- a booking landed, the session lapsed, or the account errored -- so
        staleness never outlives the fact that produced it.
        """
        cached = self._discovery_cache.get(account.name)
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < self.config.discovery_interval:
                return cached[1]

        pairs = self._discover_applications(account, client)
        self._discovery_cache[account.name] = (time.monotonic(), pairs)
        return pairs

    def _invalidate_discovery(self, account_name: str) -> None:
        """Force a fresh discovery on this account's next sweep."""
        self._discovery_cache.pop(account_name, None)

    def _discover_applications(
        self, account: Account, client: AisClient
    ) -> List[Tuple[Application, Schedule]]:
        """Pair configured applications with their live state on the site.

        With no explicit list the account is auto-discovered and every
        actionable application inherits the account-level target.
        """
        wanted_ids = [a.schedule_id for a in account.applications] or None
        schedules = client.discover_schedules(only_ids=wanted_ids)
        by_id = {s.id: s for s in schedules}

        pairs: List[Tuple[Application, Schedule]] = []
        if account.auto_discover:
            for sched in schedules:
                app = Application(
                    schedule_id=sched.id,
                    label=sched.action_label or "",
                    target=account.target,
                )
                pairs.append((app, sched))
        else:
            for app in account.applications:
                sched = by_id.get(app.schedule_id)
                if sched is None:
                    self.log(
                        f"[{account.name}] configured schedule_id {app.schedule_id} "
                        "is not present on this account; skipping"
                    )
                    self.store.update_application(
                        account.name,
                        app.schedule_id,
                        kind="unknown",
                        message="not found on this account -- check schedule_id",
                    )
                    continue
                pairs.append((app, sched))

        self.store.set_pending_discovery(account.name, False)

        keep: List[str] = [a.schedule_id for a in account.applications]
        actionable: List[Tuple[Application, Schedule]] = []
        for app, sched in pairs:
            display_target = app.target
            extra = self._group_consulates_by_member(account).get(app.schedule_id)
            if extra:
                display_target = replace(
                    app.target, consulates=list(app.target.consulates) + extra
                )
            status = self.store.register_application(
                account.name, app.schedule_id, app.label, display_target
            )
            if app.schedule_id not in keep:
                keep.append(app.schedule_id)
            self.store.update_application(
                account.name,
                app.schedule_id,
                kind=sched.kind,
                action_label=sched.action_label,
                current_appointment=sched.current_appointment,
            )
            self.log(f"[{account.name}] {sched.describe()}")
            if sched.actionable:
                actionable.append((app, sched))
            elif not status.booked_for:
                self.store.update_application(
                    account.name,
                    app.schedule_id,
                    message="no scheduling action offered by the site",
                )
        self.store.prune_applications(account.name, keep)
        return actionable

    # -- per group ---------------------------------------------------------

    def _sweep_group(
        self,
        account: Account,
        client: AisClient,
        group: Group,
        by_id: Dict[str, Tuple[Application, Schedule]],
    ) -> None:
        """Find a day that works for every member of ``group`` at once.

        Each member's /days.json is fetched independently, then intersected:
        a day only counts if every member's application can be scheduled on
        it. The earliest such day is then checked for total time-slot
        capacity across all members (/times.json, one call per member) --
        booking only proceeds once that total reaches ``group.min_slots``,
        since with fewer slots than members some of them would inevitably
        lose the race to each other or to an outside applicant.
        """
        members = [m for m in group.members if not self._booked.get(m)]
        if len(members) < 2:
            return  # already settled (or down to one member left to book alone)

        label = ", ".join(by_id[m][0].display_name for m in members if m in by_id)
        for facility_id in group.consulates:
            if self._stop.is_set():
                return
            self._sweep_group_facility(account, client, group, members, by_id, facility_id, label)

    def _sweep_group_facility(
        self,
        account: Account,
        client: AisClient,
        group: Group,
        members: List[str],
        by_id: Dict[str, Tuple[Application, Schedule]],
        facility_id: int,
        label: str,
    ) -> None:
        # Route the group's members through the same batched, ETag-deduplicated
        # fetch the per-application sweep uses. Members of a joint constraint
        # are usually siblings of one visa class, so the server reports their
        # calendars as one resource and this collapses to a single request --
        # and it drops the per-member consulate_poll_delay that made a group
        # check the slowest part of a sweep.
        calendars = self._fetch_calendars(
            account, client, [(member, facility_id) for member in members]
        )

        per_member_days: Dict[str, List[date]] = {}
        any_error = False
        for member in members:
            if self._stop.is_set():
                return
            days = calendars.get((member, facility_id))
            status = ConsulateStatus(facility_id=facility_id)
            if days is None:
                status.error = "request failed"
                any_error = True
            elif days:
                status.total_days = len(days)
                status.earliest = days[0]
            self.store.update_consulate(account.name, member, status)
            per_member_days[member] = days or []

        if any_error:
            self._record_group_metric(
                account, label, facility_id, group.min_slots,
                common_days=0, earliest_common=None, slots_found=0, error="request failed",
            )
            return

        common = sorted(set.intersection(*(set(d) for d in per_member_days.values())))
        common = [d for d in common if group.target.accepts(d)]

        per_member_earliest = {
            m: (min(days) if days else None) for m, days in per_member_days.items()
        }
        earliest_desc = ", ".join(
            f"{(by_id[m][0].label or m)}: "
            f"{per_member_earliest[m] if per_member_earliest[m] else 'none'}"
            for m in members
        )
        self.log(
            f"[{account.name}] group ({label}) {consulate_name(facility_id)}: "
            f"{len(common)} common acceptable day(s)"
            + (f", earliest {common[0]}" if common else "")
            + f" | individually: {earliest_desc}"
        )
        for member in members:
            self.store.update_application(
                account.name,
                member,
                message=(
                    ""
                    if common
                    else f"group: no common day yet at {consulate_name(facility_id)}"
                ),
            )
        if not common:
            self._record_group_metric(
                account, label, facility_id, group.min_slots,
                common_days=0, earliest_common=None, slots_found=0,
            )
            return

        day = common[0]
        per_member_times: Dict[str, List[str]] = {}
        for member in members:
            if self._stop.is_set():
                return
            times = client.get_available_times(member, facility_id, day)
            per_member_times[member] = times or []

        total_slots = sum(len(t) for t in per_member_times.values())
        self._record_group_metric(
            account, label, facility_id, group.min_slots,
            common_days=len(common), earliest_common=day, slots_found=total_slots,
        )
        if total_slots < group.min_slots or any(not t for t in per_member_times.values()):
            self.log(
                f"[{account.name}] group ({label}) {consulate_name(facility_id)} "
                f"@ {day}: only {total_slots} time slot(s) between them, need "
                f"{group.min_slots}; waiting for more capacity"
            )
            for member in members:
                self.store.update_application(
                    account.name,
                    member,
                    message=(
                        f"group: {day} at {consulate_name(facility_id)} has only "
                        f"{total_slots} slot(s), waiting for {group.min_slots}"
                    ),
                )
            return

        # Enough capacity: give each member a distinct time so their booking
        # requests do not collide on the exact same slot.
        pool = sorted({t for times in per_member_times.values() for t in times})
        self.log(
            f"[{account.name}] group ({label}) MATCH {consulate_name(facility_id)} "
            f"@ {day} ({total_slots} slot(s) available)"
        )
        for i, member in enumerate(members):
            app, _ = by_id[member]
            candidate_times = per_member_times[member] or pool
            time_value = candidate_times[min(i, len(candidate_times) - 1)]
            slot = Slot(member, facility_id, day, time=time_value)
            self._attempt_booking(account, client, app, slot)

    def _record_group_metric(
        self,
        account: Account,
        label: str,
        facility_id: int,
        min_slots: int,
        common_days: int,
        earliest_common: Optional[date],
        slots_found: int,
        error: str = "",
    ) -> None:
        """Remember the latest joint-availability check for _emit_metrics.

        Keyed on (account, members, consulate) so a later sweep overwrites the
        previous check for the same group rather than accumulating history --
        this is a live snapshot, not a series.
        """
        key = (account.name, label, facility_id)
        self._group_records[key] = GroupRecord(
            account=account.name,
            members=label,
            consulate=consulate_name(facility_id),
            common_days=common_days,
            earliest_common=earliest_common,
            slots_found=slots_found,
            min_slots=min_slots,
            error=error,
        )

    # -- per application -------------------------------------------------

    def _fetch_calendars(
        self,
        account: Account,
        client: AisClient,
        pairs: List[Tuple[str, int]],
    ) -> Dict[Tuple[str, int], Optional[List[date]]]:
        """Fetch every calendar the sweep needs, in as few requests as possible.

        Two savings compound here. Requests the server reports as the same
        resource are made once and shared (see :mod:`app.calendars`), and what
        remains is issued concurrently instead of paying
        ``consulate_poll_delay`` between each one -- that delay alone was
        turning a handful of 200ms calls into a 16-second sweep.

        Concurrency is bounded by ``max_concurrent_requests`` because the host
        is HTTP/1.1: every slot is a separate TCP connection, so this is a knob
        for footprint, not just speed. With it set to 1 the old sequential,
        delayed behaviour is restored exactly.

        Returns ``(schedule_id, facility_id) -> days``, where ``None`` means the
        request failed and ``[]`` means it succeeded and nothing is available.
        """
        if not pairs:
            return {}

        plans = self._calendars.plan(pairs, self.store.sweep_count)
        if len(plans) < len(pairs):
            self.log(
                f"[{account.name}] {CalendarCache.summarise(plans, len(pairs))}"
            )

        results: Dict[Tuple[str, int], Optional[List[date]]] = {}

        def fetch(plan):
            return plan, client.get_available_days_meta(
                plan.schedule_id, plan.facility_id
            )

        def record(plan, days, etag) -> None:
            # Only the polled pair teaches us its ETag; the ones riding along
            # keep whatever class they were already in.
            self._calendars.observe(plan.schedule_id, plan.facility_id, etag)
            for schedule_id in plan.covers:
                results[(schedule_id, plan.facility_id)] = days

        workers = min(self.config.max_concurrent_requests, len(plans))
        if workers > 1:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="days"
            ) as pool:
                futures = [pool.submit(fetch, plan) for plan in plans]
                for future in as_completed(futures):
                    plan, (days, etag) = future.result()
                    record(plan, days, etag)
        else:
            for index, plan in enumerate(plans):
                if self._stop.is_set():
                    break
                if index and self.config.consulate_poll_delay:
                    if self._stop.wait(self.config.consulate_poll_delay):
                        break
                plan, (days, etag) = fetch(plan)
                record(plan, days, etag)

        return results

    def _sweep_application(
        self,
        account: Account,
        client: AisClient,
        app: Application,
        schedule: Schedule,
        calendars: Optional[Dict[Tuple[str, int], Optional[List[date]]]] = None,
    ) -> None:
        calendars = calendars if calendars is not None else {}
        target = app.target
        if not target.consulates:
            self.store.update_application(
                account.name, app.schedule_id, message="no consulates configured"
            )
            return

        self.log(
            f"[{account.name}] {app.display_name}: checking "
            f"{target.describe_consulates()} for {target.describe_window()}"
        )

        candidates: List[Slot] = []
        best_overall: Optional[Tuple[date, int]] = None

        for i, facility_id in enumerate(target.consulates):
            if self._stop.is_set():
                return
            if (app.schedule_id, facility_id) in calendars:
                days = calendars[(app.schedule_id, facility_id)]
            else:
                # Not in the batch (a consulate added mid-sweep, or a caller
                # that did not prefetch): fall back to asking directly.
                days = client.get_available_days(app.schedule_id, facility_id)
            status = ConsulateStatus(facility_id=facility_id)

            if days is None:
                status.error = "request failed"
                self.log(f"    {consulate_name(facility_id):<12} request failed")
            elif not days:
                self.log(f"    {consulate_name(facility_id):<12} no availability")
            else:
                status.total_days = len(days)
                status.earliest = days[0]
                acceptable = [d for d in days if target.accepts(d)]
                status.acceptable_count = len(acceptable)
                if acceptable:
                    status.earliest_acceptable = acceptable[0]
                    candidates.append(Slot(app.schedule_id, facility_id, acceptable[0]))
                if best_overall is None or days[0] < best_overall[0]:
                    best_overall = (days[0], facility_id)

                detail = f"{len(days)} day(s), earliest {days[0]}"
                if acceptable:
                    detail += f" | {len(acceptable)} acceptable, earliest {acceptable[0]}"
                else:
                    blocked = target.excluded_by(days[0])
                    detail += (
                        f" | excluded by {blocked[0]}..{blocked[1]}"
                        if blocked
                        else " | outside target window"
                    )
                self.log(f"    {consulate_name(facility_id):<12} {detail}")

            self.store.update_consulate(account.name, app.schedule_id, status)
            if i < len(target.consulates) - 1:
                if (app.schedule_id, facility_id) not in calendars:
                    # Only space out requests we actually made ourselves; the
                    # batch already did its own pacing.
                    if self._stop.wait(self.config.consulate_poll_delay):
                        return

        self.store.update_application(
            account.name,
            app.schedule_id,
            best_found=best_overall[0] if best_overall else None,
            best_found_consulate=consulate_name(best_overall[1]) if best_overall else "",
            message="" if candidates else "no acceptable slot yet",
        )

        if not candidates:
            return

        slot = self._pick(candidates, target)
        self.log(f"[{account.name}] {app.display_name}: MATCH {slot}")
        self._attempt_booking(account, client, app, slot)

    @staticmethod
    def _pick(candidates: List[Slot], target: Target) -> Slot:
        if target.prefer_earliest:
            order = target.consulates
            return min(candidates, key=lambda s: (s.day, order.index(s.facility_id)))
        return candidates[0]

    def _attempt_booking(
        self, account: Account, client: AisClient, app: Application, slot: Slot
    ) -> None:
        # Notified *after* the booking attempt, not before: the site can take
        # the last slot on this day within seconds, so nothing should sit
        # between the match and the booking request itself. The Telegram
        # message still reports what was attempted, just a moment later.
        try:
            warmed = client.peek_prewarmed(
                app.schedule_id, self.config.booking_prewarm_ttl
            ) if hasattr(client, "peek_prewarmed") else None

            if warmed is not None:
                # The appointment page, its hidden fields and its CSRF token
                # were already fetched and parsed, so the only round trips left
                # are times.json and the POST itself. On a live site that
                # difference is most of the window in which somebody else takes
                # the slot.
                ok = client.book_prewarmed(
                    slot, warmed, dry_run=self.config.test_mode,
                    speculative=self.config.speculative_booking,
                )
            else:
                ok = client.book(
                    slot,
                    dry_run=self.config.test_mode,
                    retry_attempts=self.config.booking_retry_attempts,
                    retry_delay=self.config.booking_retry_delay,
                )
        except AisError as exc:
            self.log(f"[{account.name}] booking failed: {exc}")
            self.store.update_application(
                account.name, app.schedule_id, message=f"booking failed: {exc}"
            )
            self.notifier.booking_failed(account.name, app.display_name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - keep other applications alive
            self.log(f"[{account.name}] unexpected booking error: {exc}")
            traceback.print_exc()
            self.store.update_application(
                account.name, app.schedule_id, message=f"booking error: {exc}"
            )
            return

        if ok:
            self._booked[app.schedule_id] = True
            # The site's own view of this application just changed, so the
            # cached discovery no longer describes it, and the consumed
            # authenticity_token cannot be reused.
            self._invalidate_discovery(account.name)
            if hasattr(client, "invalidate_prewarm"):
                client.invalidate_prewarm(app.schedule_id)
            self.store.update_application(
                account.name,
                app.schedule_id,
                booked_for=slot.day,
                booked_time=slot.time,
                booked_consulate=slot.consulate,
                current_appointment=client.current_appointment(app.schedule_id),
                message="booked",
            )
            self.log(f"[{account.name}] BOOKED {slot} for {app.display_name}")
            self.notifier.booked(
                account.name, app.display_name, slot.consulate, slot.day, slot.time
            )
            self.store.save()
        elif self.config.test_mode:
            self.notifier.slot_found(
                account.name,
                app.display_name,
                slot.consulate,
                slot.day,
                app.target.describe_window(),
                booking=False,
            )
            self.store.update_application(
                account.name,
                app.schedule_id,
                message=f"test mode: would book {slot}",
            )
        else:
            self.notifier.slot_found(
                account.name,
                app.display_name,
                slot.consulate,
                slot.day,
                app.target.describe_window(),
                booking=True,
            )
            self.store.update_application(
                account.name,
                app.schedule_id,
                message="booking did not go through; will retry",
            )
