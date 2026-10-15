"""Polling engine.

Runs in a background thread next to the dashboard.  One sweep prepares every
account in the pool, asks for each distinct calendar once -- from a canary
where one fits, spread over the pool otherwise (see :mod:`app.planner`) -- and
then books each application on its own account from that shared calendar.

Design notes worth knowing:

* An empty availability list is *not* an error.  Four of the seven Canadian
  posts routinely return zero days, and conflating that with a failed request
  made the old implementation unreadable.
* Each account keeps its own session.  A login failure isolates to that account
  instead of stopping the whole service, and an account that keeps failing is
  rested for ``account_cooldown`` seconds while the rest of the pool covers it.
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
from typing import Dict, List, Optional, Set, Tuple

from .ais_client import (AisClient, AisError, LoginFailed, Schedule,
                         SessionExpired, Slot, TransientNetworkError,
                         parse_site_date)
from .capsolver import CapSolver, CapSolverError
from .config import Account, Application, Config, Group, Target
from .consulates import consulate_local_hour, consulate_name
from .metrics import GroupRecord, collect, collect_groups
from .newrelic import NewRelicClient
from .network import NetworkGuard
from .notifier import TelegramNotifier
from .planner import CalendarPlanner, Poll, Poller, PollResult, Subscription
from .store import ConsulateStatus, Store

# (account, client, actionable (Application, Schedule) pairs) for one sweep.
Prepared = Tuple[Account, AisClient, List[Tuple[Application, Schedule]]]
# (account, exception, reason): one failure awaiting classification. A None
# exception is a "soft" failure -- the requests failed, the session did not.
Incident = Tuple[Account, Optional[Exception], str]


def _site_replies(client: object) -> int:
    """How many application-level answers a client's session has seen."""
    count = getattr(getattr(client, "session", None), "site_replies", 0)
    return count if isinstance(count, int) else 0


class Worker:
    def __init__(
        self,
        config: Config,
        store: Store,
        notifier: TelegramNotifier,
        newrelic: Optional[NewRelicClient] = None,
        network: Optional[NetworkGuard] = None,
    ):
        self.config = config
        self.store = store
        self.notifier = notifier
        self.newrelic = newrelic
        # Owns the egress route. None means direct, with no IP-ban detection:
        # every failure is then the account's, exactly as before.
        self.network = network
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
        # Decides, for the whole account pool at once, which account asks for
        # which calendar, so one request answers every application of a class
        # and the requests are spread over canaries and subscribers instead of
        # landing on whichever account holds the applicant.
        self._planner = CalendarPlanner(
            enabled=config.calendar_sharing,
            reverify_sweeps=config.calendar_reverify_sweeps,
            logger=self.log,
        )
        # account -> monotonic time it rejoins the pool. Set once an account
        # reaches max_consecutive_failures: retrying a blocked account every
        # sweep only deepens the block, while the rest of the pool can carry
        # its calendars in the meantime.
        self._cooldown_until: Dict[str, float] = {}
        # account -> availability requests it made in the last sweep.
        self._last_load: Dict[str, int] = {}
        # (subscriber schedule_id, facility_id) -> schedule_id whose answer it
        # was given this sweep. A hit on one is re-read by the subscriber.
        self._borrowed: Dict[Tuple[str, int], str] = {}
        # Dashboard rows: which canaries each target is matched to, as of the
        # last sweep. Rebuilt every sweep, never persisted.
        self._last_matching: List[Dict[str, object]] = []
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
            if account.canary:
                # Probes only: its schedule_ids are not applications to watch.
                self.store.set_pending_discovery(account.name, False)
                self.store.prune_applications(account.name, [])
                continue
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
            if not account.auto_discover and not account.canary:
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
        now = time.monotonic()
        pool = []
        for account in self.config.accounts:
            path = self._session_path(account.name)
            sessions.append({
                "account": account.name,
                "resumed": self._session_resumed.get(account.name, False),
                "saved": path.exists(),
                "path": str(path),
            })
            until = self._cooldown_until.get(account.name, 0.0)
            pool.append({
                "account": account.name,
                "canary": account.canary,
                "available": self._pool_available(account),
                "failures": self._failures.get(account.name, 0),
                "cooldown_left": max(0, int(until - now)),
                "requests_last_sweep": self._last_load.get(account.name, 0),
            })
        return {
            "groups": groups,
            "sessions": sessions,
            "pool": pool,
            "matching": list(self._last_matching),
            "network": self.network.status() if self.network else {"enabled": False},
        }

    # -- main loop -------------------------------------------------------

    def _run(self) -> None:
        self.store.worker_status = "running"
        total_apps = sum(
            len(a.applications) if a.applications else 1
            for a in self.config.accounts if not a.canary
        )
        self.notifier.startup(
            sum(1 for a in self.config.accounts if not a.canary),
            total_apps, self.config.test_mode,
        )

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
            if account.canary:
                continue
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
        canaries = {a.name for a in self.config.accounts if a.canary}
        if any(acc["error"] for acc in snapshot["accounts"]
               if acc["name"] not in canaries):
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
        canaries = {a.name for a in self.config.accounts if a.canary}
        if any(acc["error"] for acc in snapshot["accounts"]
               if acc["name"] not in canaries):
            return False
        applications = [
            app for acc in snapshot["accounts"] for app in acc["applications"]
        ]
        if not applications:
            return False
        return all(app["state"] == "booked" for app in applications)

    # -- the sweep -------------------------------------------------------

    def _sweep(self) -> None:
        """One pass over the whole account pool, in three phases.

        1. **Prepare.** Every account that is not resting signs in (or reuses
           its session) and, unless it is a canary, resolves which of its
           applications are actionable.
        2. **Global calendar.** Everything those applications want becomes one
           set of subscriptions; the planner collapses identical calendars and
           hands each request to an account in the pool -- canaries first --
           and the requests run in parallel across accounts.
        3. **Book.** Each subscriber account reads its answers out of that
           shared calendar and books on its own session, exactly as before.
        """
        sweep = self.store.sweep_count
        accounts = self.config.accounts
        by_name = {a.name: a for a in accounts}
        clients: Dict[str, AisClient] = {}
        prepared: List[Prepared] = []
        failed: Set[str] = set()
        # Failures are only *collected* during the sweep. Whether they are the
        # accounts' fault or the egress IP's cannot be known until the sweep
        # is over -- see _settle_incidents.
        incidents: List[Incident] = []
        # Did anything reach the site and get a real answer this sweep? If so
        # the egress is fine and every failure is account-level.
        talked = False
        # Responses each session had already counted before this sweep; any
        # increase means the site itself answered something (see
        # httpclient.site_replied), which clears the egress without a probe.
        replies_before = {name: _site_replies(c) for name, c in self._clients.items()}

        if self.network is not None:
            self.network.maybe_return_to_vpn(self._stop.is_set)
            if self.network.paused() and not self.network.check_paused(
                self._stop.is_set
            ):
                self.log(
                    "network: still no working egress (probe failed, tried the "
                    f"next profile); pool paused for up to {self.network.pause_left()}s more"
                )
                return
            for cached in self._clients.values():
                session = getattr(cached, "session", None)
                if session is not None:
                    self.network.apply(session)

        # -- 1. prepare ------------------------------------------------------
        for index, account in enumerate(accounts):
            if self._stop.is_set():
                return
            self.store.register_account(account.name, account.email)
            if not self._pool_available(account):
                left = int(self._cooldown_until[account.name] - time.monotonic())
                self.log(f"[{account.name}] resting, back in the pool in {left}s")
                continue
            # Only a fresh sign-in or discovery is heavy enough to be worth
            # spacing out from the next account's.
            heavy = account.name not in self._clients or (
                not account.canary and self._discovery_stale(account)
            )
            try:
                client = self._client_for(account)
                applications = (
                    [] if account.canary
                    else self._resolve_applications(account, client)
                )
            except Exception as exc:  # noqa: BLE001 - isolate to this account
                self._incident(incidents, failed, account, exc)
            else:
                clients[account.name] = client
                talked = talked or heavy
                if not account.canary:
                    if applications:
                        prepared.append((account, client, applications))
                    else:
                        self.log(f"[{account.name}] nothing actionable on this account")

            if heavy and index < len(accounts) - 1 and self.config.account_poll_delay:
                if self._stop.wait(self.config.account_poll_delay):
                    return

        # -- 2. global calendar ---------------------------------------------
        subscriptions = self._subscriptions(prepared)
        canaries = [
            Poller(account.name, app.schedule_id, app.visa_type, canary=True)
            for account in accounts
            if account.canary and account.name in clients
            for app in account.applications
        ]
        plan = self._planner.plan(subscriptions, canaries, sweep)
        self._last_load = plan.load()
        if plan.polls:
            self.log(plan.summary())
        results, stats, errors = self._execute_polls(plan.polls, clients)
        self._planner.observe(plan, results)
        calendars = self._fan_out(results)
        self._last_matching = self._matching_rows(
            prepared, subscriptions, canaries, results
        )

        talked = talked or any(ok for ok, _ in stats.values())
        for name, exc in errors.items():
            if name not in failed:
                self._incident(incidents, failed, by_name[name], exc)
        for name, (ok, bad) in stats.items():
            if name in failed or ok or not bad:
                continue
            # Every request this account made failed: the signature of an
            # account (or an address) the site has started refusing. Counted,
            # so it ends up resting, but the session is kept --
            # re-authenticating a refused account is the worst thing to do.
            incidents.append(
                (by_name[name], None, f"all {bad} availability request(s) failed")
            )
            failed.add(name)

        # -- 3. book ---------------------------------------------------------
        for account, client, applications in prepared:
            if self._stop.is_set():
                return
            if account.name in failed:
                continue
            try:
                self._process_account(account, client, applications, calendars)
            except Exception as exc:  # noqa: BLE001 - isolate to this account
                self._incident(incidents, failed, account, exc)

        for name in clients:
            if name not in failed:
                self._record_success(by_name[name])

        if not talked:
            talked = any(
                _site_replies(c) > replies_before.get(name, 0)
                for name, c in {**self._clients, **clients}.items()
            )
        self._settle_incidents(incidents, talked)

    def _matching_rows(
        self,
        prepared: List[Prepared],
        subscriptions: List[Subscription],
        canaries: List[Poller],
        results: List[PollResult],
    ) -> List[Dict[str, object]]:
        """One dashboard row per (target application, consulate)."""
        names: Dict[str, str] = {}
        for _account, _client, applications in prepared:
            for app, _schedule in applications:
                names[app.schedule_id] = app.display_name
        answered: Dict[Tuple[str, int], Optional[Poller]] = {}
        for res in results:
            for sid in res.poll.covers:
                answered[(sid, res.poll.facility_id)] = res.poller
        rows: List[Dict[str, object]] = []
        seen: Set[Tuple[str, int]] = set()
        for sub in subscriptions:
            key = (sub.schedule_id, sub.facility_id)
            if key in seen:
                continue
            seen.add(key)
            matched = self._planner.matches(sub.schedule_id, sub.facility_id, canaries)
            if matched:
                state = "matched"
            elif self._planner.is_calibrated(*key):
                state = "no match"
            else:
                state = "checking"
            poller = answered.get(key)
            rows.append({
                "account": sub.account,
                "schedule_id": sub.schedule_id,
                "name": names.get(sub.schedule_id, sub.schedule_id),
                "consulate": consulate_name(sub.facility_id),
                "visa_type": sub.visa_type,
                "canaries": sorted({c.account for c in matched}),
                "state": state,
                "answered_by": (
                    None if poller is None
                    else poller.account if poller.canary
                    else "self" if poller.schedule_id == sub.schedule_id
                    else f"{poller.account} ({poller.schedule_id})"
                ),
            })
        rows.sort(key=lambda r: (r["account"], r["name"], r["consulate"]))
        return rows

    def _subscriptions(self, prepared: List[Prepared]) -> List[Subscription]:
        """Every (application, consulate) calendar the booking phase will read."""
        subs: List[Subscription] = []
        for account, _client, applications in prepared:
            by_id = {app.schedule_id: app for app, _ in applications}
            for app, _schedule in applications:
                if self._booked.get(app.schedule_id):
                    continue
                for facility_id in app.target.consulates:
                    subs.append(Subscription(
                        account.name, app.schedule_id, facility_id, app.visa_type,
                    ))
            for group in account.groups:
                members = [
                    m for m in group.members
                    if m in by_id and not self._booked.get(m)
                ]
                if len(members) < 2:
                    continue
                for member in members:
                    for facility_id in group.consulates:
                        subs.append(Subscription(
                            account.name, member, facility_id,
                            by_id[member].visa_type,
                        ))
        return subs

    def _execute_polls(
        self, polls: List[Poll], clients: Dict[str, AisClient],
    ) -> Tuple[List[PollResult], Dict[str, Tuple[int, int]], Dict[str, Exception]]:
        """Run every planned request, in parallel across accounts.

        Each account gets at most ``max_concurrent_requests`` in flight on its
        own session (the host is HTTP/1.1, so each is a TCP connection), and
        different accounts run side by side. With the limit at 1 an account's
        requests are strictly sequential and spaced by
        ``consulate_poll_delay``, as they always were.

        A failed request falls back to the poll's next candidate -- another
        account able to read the same calendar -- once, so one refused account
        does not leave a whole class blind for the sweep.

        Returns ``(results, stats, errors)``: ``stats`` is account ->
        ``(succeeded, failed)`` request counts, ``errors`` the first exception
        each account raised.
        """
        if not polls:
            return [], {}, {}

        limit = max(1, self.config.max_concurrent_requests)
        delay = self.config.consulate_poll_delay if limit == 1 else 0
        gates = {name: threading.BoundedSemaphore(limit) for name in clients}
        lock = threading.Lock()
        started: Set[str] = set()
        counts: Dict[str, List[int]] = {}
        errors: Dict[str, Exception] = {}

        def ask(poller: Poller, facility_id: int):
            client = clients.get(poller.account)
            with lock:
                if client is None or poller.account in errors:
                    return None, None
            with gates[poller.account]:
                if self._stop.is_set():
                    return None, None
                if delay:
                    with lock:
                        first = poller.account not in started
                        started.add(poller.account)
                    if not first and self._stop.wait(delay):
                        return None, None
                try:
                    days, etag = client.get_available_days_meta(
                        poller.schedule_id, facility_id
                    )
                except Exception as exc:  # noqa: BLE001 - reported per account
                    with lock:
                        errors.setdefault(poller.account, exc)
                    days, etag = None, None
            with lock:
                tally = counts.setdefault(poller.account, [0, 0])
                tally[0 if days is not None else 1] += 1
            return days, etag

        def run(poll: Poll) -> PollResult:
            last: Optional[Poller] = None
            attempts = 0
            for poller in poll.candidates:
                if poller.account not in clients:
                    continue
                if attempts == 2:
                    break
                attempts += 1
                last = poller
                days, etag = ask(poller, poll.facility_id)
                if days is not None:
                    if attempts > 1:
                        self.log(
                            f"[{poller.account}] answered "
                            f"{consulate_name(poll.facility_id)} after "
                            f"{poll.candidates[0].account} failed"
                        )
                    return PollResult(poll, poller, days, etag)
            return PollResult(poll, last, None, None)

        workers = min(len(polls), limit * max(1, len(clients)))
        if workers <= 1:
            results = []
            for poll in polls:
                if self._stop.is_set():
                    break
                results.append(run(poll))
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="days"
            ) as pool:
                results = [f.result() for f in [pool.submit(run, p) for p in polls]]

        stats = {name: (c[0], c[1]) for name, c in counts.items()}
        return results, stats, errors

    def _fan_out(
        self, results: List[PollResult],
    ) -> Dict[Tuple[str, int], Optional[List[date]]]:
        """``(schedule_id, facility_id) -> days`` for every subscriber covered.

        Answers a subscriber got from a canary are remembered in ``_borrowed``
        so a hit on them is confirmed by the subscriber itself before booking.
        """
        calendars: Dict[Tuple[str, int], Optional[List[date]]] = {}
        for res in results:
            for schedule_id in res.poll.covers:
                key = (schedule_id, res.poll.facility_id)
                calendars[key] = res.days
                # A canary's answer is ETag-matched, but only as of its last
                # calibration; a hit on it is re-read by the subscriber. Sharing
                # between real applications needs no second read.
                if (
                    res.poller is not None
                    and res.poller.canary
                    and res.poller.schedule_id != schedule_id
                ):
                    self._borrowed[key] = res.poller.schedule_id
                else:
                    self._borrowed.pop(key, None)
        return calendars

    def _confirm_days(
        self,
        account: Account,
        client: AisClient,
        schedule_id: str,
        facility_id: int,
        days: Optional[List[date]],
    ) -> Optional[List[date]]:
        """Re-read a borrowed calendar with the subscriber's own schedule_id.

        Called only once a borrowed answer holds a day worth booking. The
        declared ``visa_type`` is trusted for polling, so this is the one place
        a wrong declaration is caught: the subscriber's own ``days.json`` is
        what gets acted on. ``None`` means the confirming request failed.
        """
        source = self._borrowed.pop((schedule_id, facility_id), None)
        if source is None:
            return days
        own = client.get_available_days(schedule_id, facility_id)
        if own is None:
            self.log(
                f"    {consulate_name(facility_id):<12} hit via {source}, but "
                f"confirming with {schedule_id}'s own account failed"
            )
        elif (own[:1] or [None])[0] != (days[:1] or [None])[0]:
            self.log(
                f"    {consulate_name(facility_id):<12} hit via {source}, own "
                f"calendar differs (earliest {own[0] if own else 'none'}); "
                "using the subscriber's own answer"
            )
        return own

    # -- account pool ----------------------------------------------------

    def _pool_available(self, account: Account) -> bool:
        """False while the account is resting after repeated failures."""
        until = self._cooldown_until.get(account.name)
        return until is None or time.monotonic() >= until

    def _maybe_cool_down(self, account: Account, reason: str) -> None:
        """Rest an account that has failed max_consecutive_failures in a row.

        Not a permanent removal: once the rest is over the account gets one
        more try, and only a further failure rests it again.
        """
        count = self._failures.get(account.name, 0)
        rest = self.config.account_cooldown
        if rest <= 0 or count < max(1, self.config.max_consecutive_failures):
            return
        self._cooldown_until[account.name] = time.monotonic() + rest
        back = (datetime.now() + timedelta(seconds=rest)).strftime("%H:%M:%S")
        self.log(
            f"[{account.name}] {count} consecutive failures: resting it for "
            f"{rest}s (until {back}); the rest of the pool covers its calendars"
        )
        self.store.set_account_error(
            account.name, f"{reason} -- resting until {back}"
        )

    def _record_success(self, account: Account) -> None:
        if self._failures.get(account.name) or account.name in self._cooldown_until:
            if account.name in self._cooldown_until:
                self.log(f"[{account.name}] back in the pool")
            self.store.set_account_error(account.name, "")
        self._failures[account.name] = 0
        self._cooldown_until.pop(account.name, None)

    def _soft_failure(self, account: Account, reason: str) -> None:
        """Count a failure that does not invalidate the session."""
        count = self._failures.get(account.name, 0) + 1
        self._failures[account.name] = count
        self.log(
            f"[{account.name}] {reason}, keeping the session "
            f"(consecutive: {count})"
        )
        self.store.set_account_error(account.name, reason)
        if count == self.config.max_consecutive_failures:
            self.notifier.account_error(
                account.name,
                f"{reason}\n{count} consecutive failures; resting the account "
                f"for {self.config.account_cooldown}s, then retrying",
            )
        self._maybe_cool_down(account, reason)

    def _incident(
        self, incidents: List[Incident], failed: Set[str],
        account: Account, exc: Exception,
    ) -> None:
        """Note a failure; an expired session is handled on the spot.

        A lapsed session is routine and says nothing about bans, so it is
        dealt with immediately. Everything else waits for the end of the sweep
        to be classified.
        """
        failed.add(account.name)
        if isinstance(exc, SessionExpired):
            self._handle_sweep_exception(account, exc)
            return
        incidents.append((account, exc, str(exc)))

    def _settle_incidents(self, incidents: List[Incident], talked: bool) -> None:
        """Charge this sweep's failures to the accounts -- or to the egress.

        If any account got a real answer from the site, the egress works and
        every failure is the failing account's own: counted, and eventually
        rested. Only when *nothing* got through is the egress itself suspect;
        the network guard then probes it. An IP ban is not charged to any
        account (they did nothing wrong, and resting them all fixes nothing):
        the egress is rotated instead. Planned site maintenance is charged to
        no one at all.
        """
        if not incidents:
            return
        cause = "account"
        if not talked and self.network is not None:
            cause = self.network.classify_failed_sweep()

        if cause == "account":
            for account, exc, reason in incidents:
                if exc is None:
                    self._soft_failure(account, reason)
                else:
                    self._handle_sweep_exception(account, exc)
            return

        names = ", ".join(sorted({a.name for a, _, _ in incidents}))
        probe = self.network.last_probe if self.network else None
        detail = probe.detail if probe else ""
        if cause == "maintenance":
            self.log(
                f"network: the site is down for maintenance ({detail}); not "
                f"counting this sweep's failures against {names}"
            )
            for account, _, _ in incidents:
                self.store.set_account_error(account.name, "site maintenance")
            return

        # cause == "ip"
        self.log(
            f"network: the egress IP is refused by the site ({detail}). "
            f"{names} failed because of the address, not the accounts; "
            "rotating the egress instead of resting them"
        )
        for account, _, _ in incidents:
            self.store.set_account_error(account.name, "egress IP blocked")
        recovered = self.network.recover(should_stop=self._stop.is_set)
        if recovered:
            for client in self._clients.values():
                session = getattr(client, "session", None)
                if session is not None:
                    self.network.apply(session)
            for account, _, _ in incidents:
                self.store.set_account_error(account.name, "")
            outcome = f"Recovered: now routing via {self.network.mode}."
        else:
            outcome = (
                "No working egress found; the pool is paused for "
                f"{self.network.config.pause_on_ip_ban}s."
            )
        self.log(f"network: {outcome}")
        self.notifier.account_error(
            "all accounts", f"Egress IP blocked ({detail}). {outcome}"
        )

    def _handle_sweep_exception(self, account: Account, exc: Exception) -> None:
        if isinstance(exc, SessionExpired):
            # Expected periodically; not a failure worth counting or alerting.
            self.log(f"[{account.name}] {exc}")
            self._clients.pop(account.name, None)
            self._discard_saved_session(account.name)
            self._invalidate_discovery(account.name)
        elif isinstance(exc, TransientNetworkError):
            # The network blipped, not the session. Keep the cookies and try
            # again next sweep; a full traceback here is pure noise.
            count = self._failures.get(account.name, 0) + 1
            self._failures[account.name] = count
            self.log(
                f"[{account.name}] network error, keeping the session: {exc} "
                f"(consecutive: {count})"
            )
            self.store.set_account_error(account.name, f"network error: {exc}")
            if count == self.config.max_consecutive_failures:
                self.notifier.account_error(
                    account.name,
                    f"{count} consecutive network errors reaching the site. "
                    f"Still retrying every {self.config.poll_interval}s.",
                )
            self._maybe_cool_down(account, f"network error: {exc}")
        elif isinstance(exc, (LoginFailed, CapSolverError)):
            self._handle_account_failure(account, f"login failed: {exc}")
        elif isinstance(exc, AisError):
            self._handle_account_failure(account, str(exc))
        else:
            self._handle_account_failure(account, f"unexpected error: {exc}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)

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
                f"{reason}\n{count} consecutive failures; resting the account "
                f"for {self.config.account_cooldown}s, then retrying",
            )
        self._maybe_cool_down(account, reason)

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
        if self.network is not None:
            self.network.apply(client.session)
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

    def _discovery_stale(self, account: Account) -> bool:
        cached = self._discovery_cache.get(account.name)
        return cached is None or (
            time.monotonic() - cached[0] >= self.config.discovery_interval
        )

    def _process_account(
        self,
        account: Account,
        client: AisClient,
        applications: List[Tuple[Application, Schedule]],
        calendars: Dict[Tuple[str, int], Optional[List[date]]],
    ) -> None:
        """Book for one account from the sweep's shared calendar."""
        by_id = {app.schedule_id: (app, sched) for app, sched in applications}
        for group in account.groups:
            if self._stop.is_set():
                return
            self._sweep_group(account, client, group, by_id, calendars)

        checked: List[str] = []
        pending = [
            (app, sched) for app, sched in applications
            if not self._booked.get(app.schedule_id)
        ]
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
                    visa_type=account.visa_type,
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
        calendars: Optional[Dict[Tuple[str, int], Optional[List[date]]]] = None,
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
            self._sweep_group_facility(
                account, client, group, members, by_id, facility_id, label,
                calendars,
            )

    def _sweep_group_facility(
        self,
        account: Account,
        client: AisClient,
        group: Group,
        members: List[str],
        by_id: Dict[str, Tuple[Application, Schedule]],
        facility_id: int,
        label: str,
        calendars: Optional[Dict[Tuple[str, int], Optional[List[date]]]] = None,
    ) -> None:
        # Normally the sweep's shared calendar already holds every member's
        # answer. Called on its own (or for a member it missed), ask through
        # the same planner so siblings of one class still cost one request.
        wanted = [(member, facility_id) for member in members]
        if calendars is None or any(pair not in calendars for pair in wanted):
            calendars = self._fetch_calendars(account, client, wanted)

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

        # Every member lands on the same day, so that day must be earlier than
        # each member's existing appointment. A member whose current date is
        # unreadable blocks the move entirely rather than risk moving it later.
        group_ceiling: Optional[date] = None
        group_blocked = False
        for member in members:
            ok, ceiling = self._booking_ceiling(by_id[member][1] if member in by_id else None)
            if not ok:
                group_blocked = True
            elif ceiling is not None:
                group_ceiling = ceiling if group_ceiling is None else min(group_ceiling, ceiling)

        def joint(per_member: Dict[str, List[date]]) -> List[date]:
            out = sorted(set.intersection(*(set(d) for d in per_member.values())))
            out = [d for d in out if group.target.accepts(d)]
            if group_blocked:
                return []
            if group_ceiling is not None:
                out = [d for d in out if d < group_ceiling]
            return out

        common = joint(per_member_days)
        if common and any((m, facility_id) in self._borrowed for m in members):
            # A joint hit built from borrowed answers: re-read those members'
            # own calendars and decide on them.
            for member in members:
                own = self._confirm_days(
                    account, client, member, facility_id, per_member_days[member]
                )
                if own is None:
                    self._record_group_metric(
                        account, label, facility_id, group.min_slots,
                        common_days=0, earliest_common=None, slots_found=0,
                        error="confirm request failed",
                    )
                    return
                per_member_days[member] = own
            common = joint(per_member_days)

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
            app, member_schedule = by_id[member]
            candidate_times = per_member_times[member] or pool
            time_value = candidate_times[min(i, len(candidate_times) - 1)]
            slot = Slot(member, facility_id, day, time=time_value)
            self._attempt_booking(account, client, app, slot, member_schedule)

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
        """Fetch calendars for one account only, through the global planner.

        The sweep plans across the whole pool; this is the same machinery
        scoped to a single account, for callers that need calendars outside a
        sweep. Identical calendars are still asked once (ETag or declared
        ``visa_type``), and the requests still run concurrently.

        Returns ``(schedule_id, facility_id) -> days``, where ``None`` means the
        request failed and ``[]`` means it succeeded and nothing is available.
        Re-raises the first exception a request raised, as callers expect.
        """
        if not pairs:
            return {}
        types = {a.schedule_id: a.visa_type for a in account.applications}
        subs = [
            Subscription(account.name, sid, fid, types.get(sid))
            for sid, fid in pairs
        ]
        plan = self._planner.plan(subs, (), self.store.sweep_count)
        if len(plan.polls) < plan.subscriptions:
            self.log(f"[{account.name}] {plan.summary()}")
        results, _stats, errors = self._execute_polls(
            plan.polls, {account.name: client}
        )
        self._planner.observe(plan, results)
        if errors:
            raise next(iter(errors.values()))
        return self._fan_out(results)

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

        can_book, ceiling = self._booking_ceiling(schedule)
        only_earlier = ""
        if not can_book:
            only_earlier = " (reschedule, but current appointment date unreadable: will not book)"
        elif ceiling is not None:
            only_earlier = f" (currently booked {ceiling}: only earlier days)"
        self.log(
            f"[{account.name}] {app.display_name}: checking "
            f"{target.describe_consulates()} for {target.describe_window()}"
            f"{only_earlier}"
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
            if days and any(
                target.accepts(d) and can_book and (ceiling is None or d < ceiling)
                for d in days
            ):
                # A hit. If the answer came from another schedule_id (canary or
                # same-type sibling), act on this application's own calendar.
                days = self._confirm_days(
                    account, client, app.schedule_id, facility_id, days
                )
            status = ConsulateStatus(facility_id=facility_id)

            if days is None:
                status.error = "request failed"
                self.log(f"    {consulate_name(facility_id):<12} request failed")
            elif not days:
                self.log(f"    {consulate_name(facility_id):<12} no availability")
            else:
                status.total_days = len(days)
                status.earliest = days[0]
                in_window = [d for d in days if target.accepts(d)]
                acceptable = in_window
                if not can_book:
                    acceptable = []
                elif ceiling is not None:
                    acceptable = [d for d in in_window if d < ceiling]
                status.acceptable_count = len(acceptable)
                if acceptable:
                    status.earliest_acceptable = acceptable[0]
                    candidates.append(Slot(app.schedule_id, facility_id, acceptable[0]))
                if best_overall is None or days[0] < best_overall[0]:
                    best_overall = (days[0], facility_id)

                detail = f"{len(days)} day(s), earliest {days[0]}"
                if acceptable:
                    detail += f" | {len(acceptable)} acceptable, earliest {acceptable[0]}"
                elif in_window and not can_book:
                    detail += " | in window, but current appointment date unknown"
                elif in_window and ceiling is not None:
                    detail += f" | in window, but not earlier than current {ceiling}"
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
        self._attempt_booking(account, client, app, slot, schedule)

    @staticmethod
    def _booking_ceiling(schedule: Optional[Schedule]) -> Tuple[bool, Optional[date]]:
        """How far an application may be moved, given its current appointment.

        Returns ``(allowed, ceiling)``:

        * ``(True, None)`` -- no existing appointment, any acceptable day will do.
        * ``(True, d)`` -- already booked on ``d``: only days strictly before
          ``d`` count. A reschedule must never move an appointment later, and
          each one spends one of the limited reschedules the site allows.
        * ``(False, None)`` -- a reschedule whose current date could not be
          read. We cannot prove a move would be earlier, so we do not move it.
        """
        if schedule is None:
            return True, None
        current = parse_site_date(schedule.current_appointment or "")
        if current is not None:
            return True, current
        if schedule.is_reschedule:
            return False, None
        return True, None

    @staticmethod
    def _pick(candidates: List[Slot], target: Target) -> Slot:
        if target.prefer_earliest:
            order = target.consulates
            return min(candidates, key=lambda s: (s.day, order.index(s.facility_id)))
        return candidates[0]

    def _attempt_booking(
        self, account: Account, client: AisClient, app: Application, slot: Slot,
        schedule: Optional[Schedule] = None,
    ) -> None:
        # Last line of defence before anything is POSTed: whatever path got us
        # here, an existing appointment is only ever moved earlier.
        can_book, ceiling = self._booking_ceiling(schedule)
        if not can_book or (ceiling is not None and slot.day >= ceiling):
            reason = (
                "current appointment date unreadable"
                if not can_book
                else f"{slot.day} is not earlier than current {ceiling}"
            )
            self.log(f"[{account.name}] {app.display_name}: not rescheduling, {reason}")
            self.store.update_application(
                account.name, app.schedule_id, message=f"not rescheduled: {reason}"
            )
            return
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
