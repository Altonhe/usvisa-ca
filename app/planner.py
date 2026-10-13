"""Global availability planner: one account pool, one calendar per class.

A sweep used to be "for each account, ask for every calendar its own
applications want". That spends one request per application per consulate on
the account that holds it, and an account that gets blocked takes its
applications' visibility down with it.

This module turns the sweep around:

1. **Requirement.** Every unbooked application, for every consulate it (or a
   group it belongs to) watches, is a :class:`Subscription`. Subscriptions that
   are the same calendar collapse into one poll.
2. **Assignment.** Each poll is handed to an account in the pool. Any account
   holding a schedule_id of the right class can answer it -- including a
   *canary*: an account whose own appointment is long settled but whose
   ``days.json`` still serves the calendar. Canaries are preferred, then the
   least-loaded subscriber, so the requests a sweep makes are spread over the
   accounts that exist instead of all landing on whoever owns the applicant.
3. **Global calendar.** The answers are fanned back out to every subscription
   they cover; booking then happens on each subscriber's own account.

What counts as "the same calendar"
----------------------------------
The server answers that itself: ``days.json`` for one (visa class, consulate)
carries one ETag whatever schedule_id asks (see :mod:`app.calendars`). Two
sources of class membership are used:

* ``visa_type`` from the config. It is what lets a canary -- which holds no
  application you are trying to book -- be matched to a requirement at all.
* ETags observed at runtime, for applications that declare no type.

A declared ``visa_type`` is authoritative: every member and every canary of
that type shares one request from the first sweep on, and ETags are never used
to second-guess it. Instead, a hit read from someone else's answer is
confirmed by the subscriber's own account (its own ``days.json``) before it is
booked -- see :meth:`app.worker.Worker._confirm_days`. A wrong ``visa_type``
can therefore never cause a wrong booking; what it can cost is visibility, if
the poller's calendar lacks days the subscriber's would have shown.

Applications with no declared type share only on ETag evidence, re-checked
every ``reverify_sweeps`` sweeps.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Poller:
    """An (account, schedule_id) able to ask the site for a calendar."""

    account: str
    schedule_id: str
    visa_type: Optional[str] = None
    canary: bool = False


@dataclass(frozen=True)
class Subscription:
    """One application wanting one consulate's calendar."""

    account: str
    schedule_id: str
    facility_id: int
    visa_type: Optional[str] = None

    @property
    def poller(self) -> Poller:
        return Poller(self.account, self.schedule_id, self.visa_type, False)


@dataclass
class Poll:
    """One request to make, who may make it, and who reads its answer."""

    facility_id: int
    # Preferred poller first. The executor falls back down the list when a
    # poller's request fails, so one blocked account does not blind a class.
    candidates: List[Poller]
    # Subscriber schedule_ids this request answers for.
    covers: Tuple[str, ...]
    # "shared" | "solo".
    kind: str = "shared"
    # Human-readable class label, for the log.
    label: str = ""

    @property
    def poller(self) -> Poller:
        return self.candidates[0]


@dataclass
class PollResult:
    poll: Poll
    poller: Optional[Poller]           # who actually answered (None: nobody tried)
    days: Optional[list]               # None means the request failed
    etag: Optional[str] = None


@dataclass
class SweepPlan:
    polls: List[Poll] = field(default_factory=list)
    subscriptions: int = 0

    def load(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for poll in self.polls:
            out[poll.poller.account] = out.get(poll.poller.account, 0) + 1
        return out

    def summary(self) -> str:
        load = self.load()
        spread = ", ".join(f"{name}: {n}" for name, n in sorted(load.items()))
        canary = sum(1 for p in self.polls if p.poller.canary)
        text = (
            f"{len(self.polls)} availability request(s) for "
            f"{self.subscriptions} subscription(s)"
        )
        if canary:
            text += f", {canary} via canary"
        if spread:
            text += f" [{spread}]"
        return text


class CalendarPlanner:
    """Decides which account asks for which calendar, and learns from answers."""

    def __init__(
        self,
        enabled: bool = True,
        reverify_sweeps: int = 10,
        logger: Optional[Callable[[str], None]] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.enabled = enabled
        self.reverify_sweeps = reverify_sweeps
        self._log = logger or (lambda m: None)
        # (schedule_id, facility_id) -> last ETag seen for that calendar.
        # Only the untyped path decides anything from it.
        self._etags: Dict[Tuple[str, int], str] = {}
        # Picks among equally loaded pollers; injectable for tests.
        self._rng = rng or random.Random()

    # -- queries ---------------------------------------------------------

    def is_reverify_sweep(self, sweep: int) -> bool:
        if not self.enabled or self.reverify_sweeps <= 0:
            return False
        return sweep % self.reverify_sweeps == 0

    def known_etag(self, schedule_id: str, facility_id: int) -> Optional[str]:
        return self._etags.get((schedule_id, facility_id))

    def forget(self, schedule_id: Optional[str] = None) -> None:
        """Drop learned state, entirely or for one schedule_id."""
        if schedule_id is None:
            self._etags.clear()
            return
        for key in [k for k in self._etags if k[0] == schedule_id]:
            del self._etags[key]

    # -- planning --------------------------------------------------------

    def plan(
        self,
        subscriptions: Sequence[Subscription],
        canaries: Sequence[Poller] = (),
        sweep: int = 0,
    ) -> SweepPlan:
        """Turn what the sweep wants into the requests it will make."""
        subs = _dedupe(subscriptions)
        out = SweepPlan(subscriptions=len(subs))
        if not subs:
            return out

        if not self.enabled:
            out.polls = [self._solo(s) for s in subs]
            return out

        reverify = self.is_reverify_sweep(sweep)

        load: Dict[str, int] = {}
        by_facility: Dict[int, List[Subscription]] = {}
        for sub in subs:
            by_facility.setdefault(sub.facility_id, []).append(sub)

        for fid, members in by_facility.items():
            typed: Dict[str, List[Subscription]] = {}
            rest: List[Subscription] = []
            for sub in members:
                if sub.visa_type:
                    typed.setdefault(sub.visa_type, []).append(sub)
                else:
                    rest.append(sub)

            for visa_type, group in typed.items():
                self._plan_typed(visa_type, fid, group, canaries, load, out)

            self._plan_by_etag(fid, rest, canaries, reverify, load, out)

        return out

    def _plan_typed(
        self,
        visa_type: str,
        fid: int,
        group: List[Subscription],
        canaries: Sequence[Poller],
        load: Dict[str, int],
        out: SweepPlan,
    ) -> None:
        """One declared class at one consulate: one request, trusted as is.

        Same-type canaries are asked first, then the members themselves as
        fallbacks. No ETag comparison: the config is the source of truth, and
        a hit is confirmed by the subscriber's own account before booking.
        """
        same_type = [c for c in canaries if c.visa_type == visa_type]
        candidates = (
            self._least_loaded(same_type, load)
            + self._least_loaded([s.poller for s in group], load)
        )
        out.polls.append(Poll(
            facility_id=fid, candidates=candidates,
            covers=tuple(s.schedule_id for s in group),
            kind="shared" if len(candidates) > 1 else "solo",
            label=visa_type,
        ))
        self._bump(load, candidates[0].account)

    def _plan_by_etag(
        self,
        fid: int,
        subs: List[Subscription],
        canaries: Sequence[Poller],
        reverify: bool,
        load: Dict[str, int],
        out: SweepPlan,
    ) -> None:
        """Untyped subscriptions: share only on ETag evidence."""
        if reverify:
            for sub in subs:
                out.polls.append(self._solo(sub))
                self._bump(load, sub.account)
            return

        grouped: Dict[str, List[Subscription]] = {}
        unknown: List[Subscription] = []
        for sub in subs:
            etag = self._etags.get((sub.schedule_id, fid))
            if etag is None:
                unknown.append(sub)
            else:
                grouped.setdefault(etag, []).append(sub)

        for etag, members in grouped.items():
            # A canary that has already been seen serving this exact calendar
            # is as good a poller as any member, whatever its declared type.
            matching = [
                c for c in canaries
                if self._etags.get((c.schedule_id, fid)) == etag
            ]
            candidates = (
                self._least_loaded(matching, load)
                + self._least_loaded([s.poller for s in members], load)
            )
            out.polls.append(Poll(
                facility_id=fid, candidates=candidates,
                covers=tuple(s.schedule_id for s in members),
                kind="shared" if len(candidates) > 1 else "solo",
            ))
            self._bump(load, candidates[0].account)

        for sub in unknown:
            out.polls.append(self._solo(sub))
            self._bump(load, sub.account)

    @staticmethod
    def _solo(sub: Subscription, kind: str = "solo", label: str = "") -> Poll:
        return Poll(
            facility_id=sub.facility_id, candidates=[sub.poller],
            covers=(sub.schedule_id,), kind=kind, label=label,
        )

    # -- learning --------------------------------------------------------

    def observe(self, plan: SweepPlan, results: Iterable[PollResult]) -> None:
        """Learn ETags from this sweep (only the untyped path uses them)."""
        results = list(results)
        for res in results:
            if res.poller is None or res.days is None or not res.etag:
                continue
            fid = res.poll.facility_id
            self._etags[(res.poller.schedule_id, fid)] = res.etag
            # Riders are by construction the same calendar as the poller, so
            # they inherit its new ETag. Without this a changed calendar would
            # split a shared class apart for one sweep for no reason.
            for sid in res.poll.covers:
                self._etags[(sid, fid)] = res.etag

    # -- load balancing --------------------------------------------------

    def _bump(self, load: Dict[str, int], account: str) -> None:
        load[account] = load.get(account, 0) + 1

    def _least_loaded(
        self, pollers: Sequence[Poller], load: Dict[str, int],
    ) -> List[Poller]:
        """Least loaded this sweep first, random among equals.

        Equivalent to drawing pollers without replacement at random each
        sweep: one sweep's requests land on different accounts, and over many
        sweeps every canary of a type gets an even share in expectation, so
        adding a canary takes load off the others. No history is kept.
        """
        shuffled = list(pollers)
        self._rng.shuffle(shuffled)
        # sort() is stable, so ties keep their random order.
        shuffled.sort(key=lambda p: load.get(p.account, 0))
        return shuffled


# -- helpers ---------------------------------------------------------------

def _dedupe(subs: Sequence[Subscription]) -> List[Subscription]:
    seen = set()
    out: List[Subscription] = []
    for sub in subs:
        key = (sub.schedule_id, sub.facility_id)
        if key not in seen:
            seen.add(key)
            out.append(sub)
    return out

