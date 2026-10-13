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

A declared type is a claim, not evidence, so it is checked. A typed class is
**verified** before it is ever shared and again every ``reverify_sweeps``
sweeps: each member and each canary asks on its own in the same sweep and the
ETags are compared. Anything that disagrees with the majority of the members
is not reported or escalated -- it is simply taken out of the class and polled
by its own account from then on, until the next verification says it agrees
again. A wrong ``visa_type`` therefore costs requests, never a missed slot.
"""

from __future__ import annotations

from collections import Counter
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
    # "shared" | "solo" | "verify" (a verification probe answers no one).
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
class _Verification:
    """A typed class being checked this sweep."""

    visa_type: str
    facility_id: int
    members: List[str]
    canaries: List[str]


@dataclass
class SweepPlan:
    polls: List[Poll] = field(default_factory=list)
    verifications: List[_Verification] = field(default_factory=list)
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
    ) -> None:
        self.enabled = enabled
        self.reverify_sweeps = reverify_sweeps
        self._log = logger or (lambda m: None)
        # (schedule_id, facility_id) -> last ETag seen for that calendar.
        self._etags: Dict[Tuple[str, int], str] = {}
        # Typed subscriptions whose calendar disagreed with their declared
        # class at the last verification. Polled on their own until the next.
        self._diverged: Set[Tuple[str, int]] = set()
        # (canary schedule_id, facility_id) -> True agrees / False disagrees.
        # Missing means "not verified yet", which is never shared on.
        self._canary_ok: Dict[Tuple[str, int], bool] = {}
        # (visa_type, facility_id) whose members were verified this epoch.
        self._verified: Set[Tuple[str, int]] = set()

    # -- queries ---------------------------------------------------------

    def is_reverify_sweep(self, sweep: int) -> bool:
        if not self.enabled or self.reverify_sweeps <= 0:
            return False
        return sweep % self.reverify_sweeps == 0

    def known_etag(self, schedule_id: str, facility_id: int) -> Optional[str]:
        return self._etags.get((schedule_id, facility_id))

    def is_diverged(self, schedule_id: str, facility_id: int) -> bool:
        return (schedule_id, facility_id) in self._diverged

    def canary_state(self, schedule_id: str, facility_id: int) -> Optional[bool]:
        return self._canary_ok.get((schedule_id, facility_id))

    def forget(self, schedule_id: Optional[str] = None) -> None:
        """Drop learned state, entirely or for one schedule_id."""
        if schedule_id is None:
            self._etags.clear()
            self._diverged.clear()
            self._canary_ok.clear()
            self._verified.clear()
            return
        for store in (self._etags, self._canary_ok):
            for key in [k for k in store if k[0] == schedule_id]:
                del store[key]
        self._diverged = {k for k in self._diverged if k[0] != schedule_id}

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
        if reverify:
            # A new epoch: every class must earn its sharing again.
            self._verified.clear()

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
                rest.extend(self._plan_typed(
                    visa_type, fid, group, canaries, reverify, load, out,
                ))

            self._plan_by_etag(fid, rest, canaries, reverify, load, out)

        return out

    def _plan_typed(
        self,
        visa_type: str,
        fid: int,
        group: List[Subscription],
        canaries: Sequence[Poller],
        reverify: bool,
        load: Dict[str, int],
        out: SweepPlan,
    ) -> List[Subscription]:
        """Plan one declared class at one consulate.

        Returns the members that are NOT covered here (diverged ones), for
        the ETag-based path to handle.
        """
        same_type = [c for c in canaries if c.visa_type == visa_type]
        key = (visa_type, fid)
        unverified_canary = any(
            (c.schedule_id, fid) not in self._canary_ok for c in same_type
        )
        worth_verifying = len(group) > 1 or bool(same_type)
        if worth_verifying and (
            reverify or key not in self._verified or unverified_canary
        ):
            # Everyone answers for themselves this sweep, and the canaries are
            # asked alongside so their ETag is compared at the same moment.
            for sub in group:
                out.polls.append(self._solo(sub, kind="solo", label=visa_type))
                _bump(load, sub.account)
            for canary in _least_loaded(same_type, load):
                out.polls.append(Poll(
                    facility_id=fid, candidates=[canary], covers=(),
                    kind="verify", label=visa_type,
                ))
                _bump(load, canary.account)
            out.verifications.append(_Verification(
                visa_type=visa_type, facility_id=fid,
                members=[s.schedule_id for s in group],
                canaries=[c.schedule_id for c in same_type],
            ))
            return []

        agreeing = [s for s in group if (s.schedule_id, fid) not in self._diverged]
        diverged = [s for s in group if (s.schedule_id, fid) in self._diverged]
        if not agreeing:
            return diverged

        good_canaries = [
            c for c in same_type if self._canary_ok.get((c.schedule_id, fid))
        ]
        candidates = (
            _least_loaded(good_canaries, load)
            + _least_loaded([s.poller for s in agreeing], load)
        )
        out.polls.append(Poll(
            facility_id=fid, candidates=candidates,
            covers=tuple(s.schedule_id for s in agreeing),
            kind="shared" if len(agreeing) > 1 or good_canaries else "solo",
            label=visa_type,
        ))
        _bump(load, candidates[0].account)
        return diverged

    def _plan_by_etag(
        self,
        fid: int,
        subs: List[Subscription],
        canaries: Sequence[Poller],
        reverify: bool,
        load: Dict[str, int],
        out: SweepPlan,
    ) -> None:
        """Untyped (or diverged) subscriptions: share only on ETag evidence."""
        if reverify:
            for sub in subs:
                out.polls.append(self._solo(sub))
                _bump(load, sub.account)
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
                and self._canary_ok.get((c.schedule_id, fid), True)
            ]
            candidates = (
                _least_loaded(matching, load)
                + _least_loaded([s.poller for s in members], load)
            )
            out.polls.append(Poll(
                facility_id=fid, candidates=candidates,
                covers=tuple(s.schedule_id for s in members),
                kind="shared" if len(candidates) > 1 else "solo",
            ))
            _bump(load, candidates[0].account)

        for sub in unknown:
            out.polls.append(self._solo(sub))
            _bump(load, sub.account)

    @staticmethod
    def _solo(sub: Subscription, kind: str = "solo", label: str = "") -> Poll:
        return Poll(
            facility_id=sub.facility_id, candidates=[sub.poller],
            covers=(sub.schedule_id,), kind=kind, label=label,
        )

    # -- learning --------------------------------------------------------

    def observe(self, plan: SweepPlan, results: Iterable[PollResult]) -> None:
        """Learn ETags from this sweep and settle any verifications."""
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

        for check in plan.verifications:
            self._settle(check)

    def _settle(self, check: _Verification) -> None:
        fid = check.facility_id
        seen = {
            sid: self._etags.get((sid, fid)) for sid in check.members
        }
        votes = Counter(e for e in seen.values() if e)
        if not votes:
            return  # every member failed: nothing learned, retry next sweep

        top = votes.most_common()
        reference = top[0][0]
        if len(top) > 1 and top[0][1] == top[1][1]:
            # A tie among members: let a canary that matches one side decide.
            tied = {e for e, n in top if n == top[0][1]}
            for csid in check.canaries:
                etag = self._etags.get((csid, fid))
                if etag in tied:
                    reference = etag
                    break

        for sid, etag in seen.items():
            if etag is None:
                continue
            if etag != reference:
                if (sid, fid) not in self._diverged:
                    self._log(
                        f"{sid} @ facility {fid}: calendar differs from the rest "
                        f"of {check.visa_type}; it will be polled by its own "
                        "account until the next re-verify"
                    )
                self._diverged.add((sid, fid))
            else:
                self._diverged.discard((sid, fid))

        for csid in check.canaries:
            etag = self._etags.get((csid, fid))
            if etag is None:
                continue  # its request failed: stays unverified
            ok = etag == reference
            if self._canary_ok.get((csid, fid)) is not ok:
                self._log(
                    f"canary {csid} @ facility {fid}: "
                    + ("matches " if ok else "does NOT match ")
                    + f"{check.visa_type}"
                    + ("" if ok else "; not used for it until the next re-verify")
                )
            self._canary_ok[(csid, fid)] = ok

        self._verified.add((check.visa_type, fid))


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


def _bump(load: Dict[str, int], account: str) -> None:
    load[account] = load.get(account, 0) + 1


def _least_loaded(pollers: Sequence[Poller], load: Dict[str, int]) -> List[Poller]:
    """Stable order, least-loaded account first."""
    indexed = list(enumerate(pollers))
    indexed.sort(key=lambda item: (load.get(item[1].account, 0), item[0]))
    return [p for _, p in indexed]
