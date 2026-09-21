"""Deduplicating planner for availability requests.

``days.json`` for a given (visa class, facility) is the *same resource* no
matter which sibling ``schedule_id`` asks for it. The server says so itself: it
returns one identical ETag for all of them. Verified against the live host --
two B2 applications on one account both answered
``W/"604aa90315c8ca4e3fa894aa08a678fe"`` at Toronto, while two applications of a
different class on the *same* account answered a different ETag entirely.

So N applications watching one consulate need one request, not N. On the real
configuration that is the difference between 8 availability requests per sweep
and 2.

The grouping is never guessed from configuration, because configuration does
not know it -- visa class is not something the config file states. It is
**observed** from the ETags the server returns, and re-observed every
``reverify_sweeps`` sweeps so that a class which stops being identical splits
itself back apart without anyone having to notice.

Two deliberate properties:

* A pair whose class is unknown is always polled on its own. Sharing only ever
  happens on evidence already in hand.
* A failed poll teaches nothing, so it leaves the known classes untouched
  rather than tearing them down on every transient blip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PollPlan:
    """One request to make, plus who else gets to read its answer."""

    schedule_id: str                      # the application we actually ask
    facility_id: int
    fan_out_to: Tuple[str, ...] = ()      # others reusing it; excludes schedule_id

    @property
    def covers(self) -> Tuple[str, ...]:
        """Every schedule_id this single request answers for."""
        return (self.schedule_id,) + self.fan_out_to

    @property
    def saved_requests(self) -> int:
        return len(self.fan_out_to)

    @property
    def shared(self) -> bool:
        return bool(self.fan_out_to)


class CalendarCache:
    """Remembers which (schedule_id, facility) pairs are the same calendar."""

    def __init__(self, enabled: bool = True, reverify_sweeps: int = 10) -> None:
        self.enabled = enabled
        self.reverify_sweeps = reverify_sweeps
        # (schedule_id, facility_id) -> last ETag the server gave for it
        self._classes: Dict[Tuple[str, int], str] = {}

    # -- observation -----------------------------------------------------

    def observe(self, schedule_id: str, facility_id: int,
                etag: Optional[str]) -> None:
        """Record the ETag a pair answered with.

        ``None`` means the request failed or the server sent no ETag; either
        way we learned nothing, so existing knowledge is left alone.
        """
        if not etag:
            return
        self._classes[(schedule_id, facility_id)] = etag

    def forget(self, schedule_id: Optional[str] = None) -> None:
        """Drop learned groupings, entirely or for one application."""
        if schedule_id is None:
            self._classes.clear()
            return
        for key in [k for k in self._classes if k[0] == schedule_id]:
            del self._classes[key]

    def known_etag(self, schedule_id: str, facility_id: int) -> Optional[str]:
        return self._classes.get((schedule_id, facility_id))

    # -- planning --------------------------------------------------------

    def is_reverify_sweep(self, sweep: int) -> bool:
        """True when this sweep must re-observe every pair individually."""
        if not self.enabled or self.reverify_sweeps <= 0:
            return False
        return sweep % self.reverify_sweeps == 0

    def plan(self, pairs: Sequence[Tuple[str, int]], sweep: int = 0) -> List[PollPlan]:
        """Turn the pairs we want into the smaller set we actually request.

        Order is preserved so the log and the request pattern stay predictable.
        On a re-verify sweep every pair is polled on its own, which costs the
        full request count once every ``reverify_sweeps`` sweeps and is what
        keeps the sharing honest.
        """
        ordered: List[Tuple[str, int]] = []
        seen = set()
        for sid, fid in pairs:
            if (sid, fid) not in seen:
                seen.add((sid, fid))
                ordered.append((sid, fid))

        if not self.enabled:
            return [PollPlan(sid, fid) for sid, fid in ordered]

        if self.is_reverify_sweep(sweep):
            return [PollPlan(sid, fid) for sid, fid in ordered]

        by_facility: Dict[int, List[str]] = {}
        for sid, fid in ordered:
            by_facility.setdefault(fid, []).append(sid)

        plans: List[PollPlan] = []
        for fid, sids in by_facility.items():
            grouped: Dict[str, List[str]] = {}
            unknown: List[str] = []
            for sid in sids:
                etag = self._classes.get((sid, fid))
                if etag is None:
                    unknown.append(sid)
                else:
                    grouped.setdefault(etag, []).append(sid)
            for members in grouped.values():
                plans.append(PollPlan(members[0], fid, tuple(members[1:])))
            plans.extend(PollPlan(sid, fid) for sid in unknown)
        return plans

    # -- reporting -------------------------------------------------------

    @staticmethod
    def summarise(plans: Sequence[PollPlan], wanted: int) -> str:
        """One-line description of what a plan saved, for the sweep log."""
        saved = sum(p.saved_requests for p in plans)
        if not saved:
            return f"{wanted} availability request(s)"
        return (
            f"{len(plans)} availability request(s) instead of {wanted} "
            f"({saved} shared via identical ETag)"
        )
