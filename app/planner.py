"""Global availability planner: one account pool, one calendar per class.

A sweep used to be "for each account, ask for every calendar its own
applications want". That spends one request per application per consulate on
the account that holds it, and an account that gets blocked takes its
applications' visibility down with it.

This module turns the sweep around:

1. **Requirement.** Every unbooked application, for every consulate it (or a
   group it belongs to) watches, is a :class:`Subscription`.
2. **Matching.** Which schedule_ids see the same calendar is decided by the
   server, not the config: ``days.json`` for one calendar carries one ETag
   whatever schedule_id asks. In a *calibration round* every target that needs
   it asks for its own calendar, and every canary asks for the same consulate
   in the same sweep, so the ETags are seconds apart. Whoever returned the same
   ETag forms a *calendar class*: target 1 may end up with canaries A and B,
   target 2 with canaries C, D and E, and a target nobody matches polls itself.
   A declared ``visa_type`` is no longer trusted -- the same label can still
   hide different calendars (in Canada, PR and non-PR applicants of one class
   do not see the same days). It is only a hint: a canary is never tried for a
   target whose declared type differs from its own.
3. **Assignment.** Between calibrations each class costs one request, handed
   to one of its canaries at random (least loaded this sweep), with the
   class's own members as fallbacks.
4. **Global calendar.** The answers are fanned back out to every subscription
   they cover; booking then happens on each subscriber's own account, and a
   hit read from a canary is re-read by the subscriber before it is booked.

Each target is re-calibrated every ``reverify_sweeps`` sweeps, so a canary
whose calendar drifts away is dropped and one that starts matching is picked
up, with no manual step. Calibration needs ``days.json`` with at least one
day: every empty calendar has the same ETag, so an empty answer proves
nothing and the target polls itself until the next calibration.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


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
    # "shared": a known class; "solo": a target asking for itself (during
    # calibration, or because nothing matched it); "calibrate": a canary
    # asking only so its ETag can be compared.
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
    sweep: int = 0

    def load(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for poll in self.polls:
            out[poll.poller.account] = out.get(poll.poller.account, 0) + 1
        return out

    def summary(self) -> str:
        load = self.load()
        spread = ", ".join(f"{name}: {n}" for name, n in sorted(load.items()))
        canary = sum(1 for p in self.polls if p.poller.canary and p.covers)
        probing = sum(1 for p in self.polls if p.kind == "calibrate")
        text = (
            f"{len(self.polls)} availability request(s) for "
            f"{self.subscriptions} subscription(s)"
        )
        if canary:
            text += f", {canary} via canary"
        if probing:
            text += f", {probing} canary ETag check(s)"
        if spread:
            text += f" [{spread}]"
        return text


Key = Tuple[str, int]                  # (schedule_id, facility_id)


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
        # (schedule_id, facility_id) -> calendar class id. Targets and
        # canaries alike; equal ids at one consulate = proven same calendar.
        self._class: Dict[Key, int] = {}
        # Target (schedule_id, facility_id) -> sweep it was last calibrated.
        self._calibrated: Dict[Key, int] = {}
        self._next_id = 1
        # Picks among equally loaded pollers; injectable for tests.
        self._rng = rng or random.Random()

    # -- queries ---------------------------------------------------------

    def matches(self, schedule_id: str, facility_id: int,
                canaries: Sequence[Poller]) -> List[Poller]:
        """Canaries currently proven to serve this target's calendar."""
        cid = self._class.get((schedule_id, facility_id))
        if cid is None:
            return []
        return [c for c in canaries
                if self._class.get((c.schedule_id, facility_id)) == cid]

    def is_calibrated(self, schedule_id: str, facility_id: int) -> bool:
        """Has this target finished at least one ETag check?"""
        return (schedule_id, facility_id) in self._calibrated

    def forget(self, schedule_id: Optional[str] = None) -> None:
        """Drop learned state, entirely or for one schedule_id."""
        if schedule_id is None:
            self._class.clear()
            self._calibrated.clear()
            return
        for store in (self._class, self._calibrated):
            for key in [k for k in store if k[0] == schedule_id]:
                del store[key]

    def _due(self, key: Key, sweep: int) -> bool:
        last = self._calibrated.get(key)
        if last is None:
            return True
        return self.reverify_sweeps > 0 and sweep - last >= self.reverify_sweeps

    # -- planning --------------------------------------------------------

    def plan(
        self,
        subscriptions: Sequence[Subscription],
        canaries: Sequence[Poller] = (),
        sweep: int = 0,
    ) -> SweepPlan:
        """Turn what the sweep wants into the requests it will make."""
        subs = _dedupe(subscriptions)
        out = SweepPlan(subscriptions=len(subs), sweep=sweep)
        if not subs:
            return out

        if not self.enabled:
            out.polls = [self._solo(s) for s in subs]
            return out

        load: Dict[str, int] = {}
        by_facility: Dict[int, List[Subscription]] = {}
        for sub in subs:
            by_facility.setdefault(sub.facility_id, []).append(sub)

        for fid, members in by_facility.items():
            self._plan_facility(fid, members, canaries, sweep, load, out)
        return out

    def _plan_facility(
        self,
        fid: int,
        members: List[Subscription],
        canaries: Sequence[Poller],
        sweep: int,
        load: Dict[str, int],
        out: SweepPlan,
    ) -> None:
        due = [s for s in members if self._due((s.schedule_id, fid), sweep)]
        settled = [s for s in members if s not in due]

        # Known classes among the settled targets. A target without a class
        # (nothing matched it, or its calendar was empty) asks for itself.
        classes: Dict[int, List[Subscription]] = {}
        for sub in settled:
            cid = self._class.get((sub.schedule_id, fid))
            if cid is None:
                out.polls.append(self._solo(sub))
                self._bump(load, sub.account)
            else:
                classes.setdefault(cid, []).append(sub)

        # Calibration round: each due target asks for itself, and every
        # canary that could plausibly match asks too, in the same sweep.
        probes: Dict[Poller, Poll] = {}
        if due:
            for sub in due:
                out.polls.append(self._solo(sub, label="calibrating"))
                self._bump(load, sub.account)
            for canary in canaries:
                if not any(_compatible(canary, s) for s in members):
                    continue
                poll = Poll(facility_id=fid, candidates=[canary], covers=(),
                            kind="calibrate", label="ETag check")
                probes[canary] = poll
                out.polls.append(poll)
                self._bump(load, canary.account)

        for cid, group in classes.items():
            own = [c for c in canaries
                   if self._class.get((c.schedule_id, fid)) == cid]
            covers = tuple(s.schedule_id for s in group)
            fallbacks = self._least_loaded([s.poller for s in group], load)
            probing = [c for c in own if c in probes and not probes[c].covers]
            if probing:
                # A canary of this class is asking anyway: let its answer serve
                # the class instead of spending a second request on it.
                canary = self._rng.choice(probing)
                poll = probes[canary]
                poll.covers = covers
                poll.kind = "shared"
                poll.candidates = [canary] + fallbacks
                continue
            candidates = self._least_loaded(own, load) + fallbacks
            out.polls.append(Poll(
                facility_id=fid, candidates=candidates, covers=covers,
                kind="shared" if len(candidates) > 1 else "solo",
            ))
            self._bump(load, candidates[0].account)

    @staticmethod
    def _solo(sub: Subscription, kind: str = "solo", label: str = "") -> Poll:
        return Poll(
            facility_id=sub.facility_id, candidates=[sub.poller],
            covers=(sub.schedule_id,), kind=kind, label=label,
        )

    # -- learning --------------------------------------------------------

    def observe(self, plan: SweepPlan, results: Iterable[PollResult]) -> None:
        """Re-draw the calendar classes from this sweep's calibration answers.

        Only schedule_ids that were *calibrated* this sweep (a target asking
        for itself on a calibration round, or a canary's ETag check) change
        class. A settled class's own poll is the anchor: whoever matched its
        ETag joins it, keeping its id so canaries not asked this sweep stay in.
        """
        by_facility: Dict[int, List[PollResult]] = {}
        for res in results:
            by_facility.setdefault(res.poll.facility_id, []).append(res)
        for fid, group in by_facility.items():
            self._observe_facility(fid, group, plan)

    def _observe_facility(self, fid: int, results: List[PollResult],
                          plan: SweepPlan) -> None:
        calibrating = [r for r in results
                       if r.poll.kind == "calibrate" or r.poll.label == "calibrating"]
        if not calibrating:
            return

        buckets: Dict[str, List[Tuple[PollResult, bool]]] = {}
        for res in results:
            if res.poller is None or res.days is None:
                continue
            calibrated = res.poll.kind == "calibrate" or res.poll.label == "calibrating"
            if not res.days or not res.etag:
                # Empty calendars all share one ETag: no evidence either way.
                if calibrated:
                    for sid in _members(res):
                        self._class.pop((sid, fid), None)
                    self._mark(res, fid, plan.sweep)
                continue
            buckets.setdefault(res.etag, []).append((res, calibrated))
            if calibrated:
                self._mark(res, fid, plan.sweep)

        for etag, entries in buckets.items():
            anchor = None
            for res, calibrated in entries:
                if not calibrated:
                    anchor = self._class.get((res.poll.covers[0], fid)) \
                        if res.poll.covers else None
                    if anchor is not None:
                        break
            if anchor is None:
                anchor = self._next_id
                self._next_id += 1
            joined: List[str] = []
            for res, calibrated in entries:
                for sid in _members(res):
                    if self._class.get((sid, fid)) != anchor:
                        self._class[(sid, fid)] = anchor
                        joined.append(sid)
            canaries = sorted({r.poller.schedule_id for r, _ in entries if r.poller.canary})
            targets = sorted({sid for r, _ in entries for sid in r.poll.covers})
            if joined and targets:
                self._log(
                    f"calendar match @ facility {fid}: target(s) {', '.join(targets)} "
                    f"<- canary(s) {', '.join(canaries) or 'none'}"
                )

    def _mark(self, res: PollResult, fid: int, sweep: int) -> None:
        for sid in res.poll.covers:
            self._calibrated[(sid, fid)] = sweep

    # -- load balancing --------------------------------------------------

    def _bump(self, load: Dict[str, int], account: str) -> None:
        load[account] = load.get(account, 0) + 1

    def _least_loaded(
        self, pollers: Sequence[Poller], load: Dict[str, int],
    ) -> List[Poller]:
        """Least loaded this sweep first, random among equals.

        Equivalent to drawing pollers without replacement at random each
        sweep: one sweep's requests land on different accounts, and over many
        sweeps every matching canary gets an even share in expectation, so
        adding a canary takes load off the others. No history is kept.
        """
        shuffled = list(pollers)
        self._rng.shuffle(shuffled)
        # sort() is stable, so ties keep their random order.
        shuffled.sort(key=lambda p: load.get(p.account, 0))
        return shuffled


# -- helpers ---------------------------------------------------------------

def _compatible(canary: Poller, sub: Subscription) -> bool:
    """A declared type only ever rules a canary out, never in."""
    return not (canary.visa_type and sub.visa_type
                and canary.visa_type != sub.visa_type)


def _members(res: PollResult) -> Set[str]:
    out = set(res.poll.covers)
    if res.poller is not None:
        out.add(res.poller.schedule_id)
    return out


def _dedupe(subs: Sequence[Subscription]) -> List[Subscription]:
    seen = set()
    out: List[Subscription] = []
    for sub in subs:
        key = (sub.schedule_id, sub.facility_id)
        if key not in seen:
            seen.add(key)
            out.append(sub)
    return out
