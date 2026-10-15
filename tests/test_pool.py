"""The global account pool: canaries, shared calendars, verification, rest.

Every account -- canary or subscriber -- is one pool. A calendar is asked once
per (visa class, consulate), preferably by a canary, and spread over whoever
can answer it. A declared visa_type is trusted as is; a hit read from another
schedule_id is confirmed on the subscriber's own account before booking. A
failing account rests and the rest of the pool covers for it.
"""

import threading
from datetime import date

import pytest

from app.ais_client import AisError, Schedule
from app.config import (Account, Application, Config, ConfigError,
                        DashboardConfig, Target, TelegramConfig, load_config)
from app.notifier import TelegramNotifier
from app.planner import CalendarPlanner, Poller, Subscription
from app.store import Store
from app.worker import Worker

TRT, VAN = 94, 95
B2 = 'W/"b2"'
F1 = 'W/"f1"'
DAY = date(2026, 10, 20)
DAY_LATE = date(2027, 6, 1)        # outside every test window


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def subs(*items):
    """items: (account, sid, fid, visa_type)."""
    return [Subscription(a, s, f, t) for a, s, f, t in items]


def answer(planner, plan, etags, days=None):
    """Feed the planner the ETags each polled schedule_id 'returned'.

    Every answer carries one day unless ``days`` overrides it: an empty
    calendar is no ETag evidence, so tests about matching need a day.
    """
    from app.planner import PollResult
    days = days or {}
    results = [PollResult(p, p.poller, days.get(p.poller.schedule_id, [DAY]),
                          etags.get(p.poller.schedule_id))
               for p in plan.polls]
    planner.observe(plan, results)
    return results


def calibrate(planner, wanted, canaries, etags, sweep=1):
    plan = planner.plan(wanted, canaries, sweep)
    answer(planner, plan, etags)
    return plan


def test_first_sweep_calibrates_every_target_against_every_canary():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("C1", "901", None, True), Poller("C2", "902", None, True)]
    wanted = subs(("A", "1", TRT, None))
    plan = planner.plan(wanted, canaries, 1)
    kinds = sorted((p.poller.schedule_id, p.kind) for p in plan.polls)
    assert kinds == [("1", "solo"), ("901", "calibrate"), ("902", "calibrate")]


def test_each_target_gets_only_the_canaries_whose_etag_matched():
    """Target 1 <- canaries A, B; target 2 <- canaries C, D, E."""
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller(n, s, "B1/B2", True) for n, s in
                (("CA", "91"), ("CB", "92"), ("CC", "93"), ("CD", "94"), ("CE", "95"))]
    wanted = subs(("A", "1", TRT, "B1/B2"), ("B", "2", TRT, "B1/B2"))
    pr, non_pr = 'W/"pr"', 'W/"nonpr"'
    calibrate(planner, wanted, canaries, {
        "1": pr, "91": pr, "92": pr,
        "2": non_pr, "93": non_pr, "94": non_pr, "95": non_pr,
    })

    assert {c.account for c in planner.matches("1", TRT, canaries)} == {"CA", "CB"}
    assert {c.account for c in planner.matches("2", TRT, canaries)} == {"CC", "CD", "CE"}

    plan = planner.plan(wanted, canaries, 2)
    assert len(plan.polls) == 2, "one request per calendar class"
    for poll in plan.polls:
        assert poll.poller.canary
        allowed = {"1": {"CA", "CB"}, "2": {"CC", "CD", "CE"}}[poll.covers[0]]
        assert poll.poller.account in allowed, "never a canary of the other class"
        assert {c.account for c in poll.candidates if c.canary} == allowed


def test_a_target_nobody_matches_polls_itself():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("C", "900", None, True)]
    wanted = subs(("A", "1", TRT, None))
    calibrate(planner, wanted, canaries, {"1": 'W/"x"', "900": 'W/"y"'})
    plan = planner.plan(wanted, canaries, 2)
    assert [(p.poller.schedule_id, p.covers) for p in plan.polls] == [("1", ("1",))]


def test_a_declared_type_only_rules_a_canary_out():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("CF", "901", "F1", True), Poller("CU", "902", None, True)]
    plan = planner.plan(subs(("A", "1", TRT, "B1/B2")), canaries, 1)
    probed = {p.poller.account for p in plan.polls if p.kind == "calibrate"}
    assert probed == {"CU"}, "F1 canary is not tried for a B1/B2 target"


def test_an_empty_calendar_proves_nothing():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("C", "900", None, True)]
    wanted = subs(("A", "1", TRT, None))
    plan = planner.plan(wanted, canaries, 1)
    empty = 'W/"empty"'
    answer(planner, plan, {"1": empty, "900": empty}, days={"1": [], "900": []})
    assert planner.matches("1", TRT, canaries) == []
    assert not planner.plan(wanted, canaries, 2).polls[0].poller.canary


def test_recalibration_drops_a_canary_that_drifted_and_adds_one_that_matches():
    planner = CalendarPlanner(reverify_sweeps=5)
    canaries = [Poller("C1", "901", None, True), Poller("C2", "902", None, True)]
    wanted = subs(("A", "1", TRT, None))
    calibrate(planner, wanted, canaries, {"1": B2, "901": B2, "902": F1}, sweep=1)
    assert [c.account for c in planner.matches("1", TRT, canaries)] == ["C1"]

    for n in range(2, 6):            # settled: no ETag checks in between
        plan = planner.plan(wanted, canaries, n)
        assert len(plan.polls) == 1 and plan.polls[0].poller.account == "C1"
        answer(planner, plan, {"901": B2})

    plan = planner.plan(wanted, canaries, 6)
    assert any(p.kind == "calibrate" for p in plan.polls), "re-verify after 5 sweeps"
    answer(planner, plan, {"1": B2, "901": F1, "902": B2})
    assert [c.account for c in planner.matches("1", TRT, canaries)] == ["C2"]


def test_new_target_joins_a_settled_class_without_splitting_it():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("C1", "901", None, True), Poller("C2", "902", None, True)]
    calibrate(planner, subs(("A", "1", TRT, None)), canaries,
              {"1": B2, "901": B2, "902": B2})

    wanted = subs(("A", "1", TRT, None), ("B", "2", TRT, None))
    plan = planner.plan(wanted, canaries, 2)
    # Target 2 calibrates; target 1's class rides on one canary's check.
    assert len(plan.polls) == 3, [(p.poller.schedule_id, p.covers) for p in plan.polls]
    answer(planner, plan, {"1": B2, "2": B2, "901": B2, "902": B2})
    assert {c.account for c in planner.matches("2", TRT, canaries)} == {"C1", "C2"}
    plan = planner.plan(wanted, canaries, 3)
    assert len(plan.polls) == 1 and set(plan.polls[0].covers) == {"1", "2"}


def test_work_rotates_over_every_matching_canary_across_sweeps():
    """3 calendars, 4 matching canaries: none idle, none carries more."""
    import random
    from collections import Counter
    planner = CalendarPlanner(reverify_sweeps=100000, rng=random.Random(7))
    canaries = [Poller(f"C{i}", f"90{i}", None, True) for i in range(4)]
    wanted = subs(("A", "1", TRT, None), ("A", "1", 91, None),
                  ("B", "2", VAN, None))
    calibrate(planner, wanted, canaries, {s: B2 for s in ("1", "2", "900", "901", "902", "903")})

    used = Counter()
    for n in range(2, 402):
        plan = planner.plan(wanted, canaries, n)
        accounts = [p.poller.account for p in plan.polls]
        assert len(set(accounts)) == 3, "one sweep still spreads over 3 canaries"
        used.update(accounts)
    assert set(used) == {"C0", "C1", "C2", "C3"}
    assert all(240 <= n <= 360 for n in used.values()), used


def test_siblings_with_the_same_etag_share_without_a_canary():
    planner = CalendarPlanner(reverify_sweeps=100)
    wanted = subs(("A", "1", TRT, None), ("B", "2", TRT, None))
    calibrate(planner, wanted, [], {"1": B2, "2": B2})
    assert len(planner.plan(wanted, [], 2).polls) == 1


def test_sharing_disabled_polls_everyone():
    planner = CalendarPlanner(enabled=False)
    wanted = subs(("A", "1", TRT, "B2"), ("A", "2", TRT, "B2"))
    assert len(planner.plan(wanted, [Poller("C", "9", "B2", True)], 3).polls) == 2


# ---------------------------------------------------------------------------
# Worker, end to end
# ---------------------------------------------------------------------------

class PoolClient:
    """One fake session per account; days depend on the class of the sid."""

    def __init__(self, sids, classes, fail_days=False):
        self.sids = list(sids)
        self.classes = classes            # sid -> etag
        self.fail_days = fail_days
        self.calls = []
        self.booked = []
        self.logins = 0
        self._lock = threading.Lock()

    def get_available_days_meta(self, schedule_id, facility_id):
        with self._lock:
            self.calls.append((schedule_id, facility_id))
        if self.fail_days:
            return None, None
        etag = self.classes[schedule_id]
        return ([DAY] if etag == B2 else [DAY_LATE]), etag

    def get_available_days(self, schedule_id, facility_id):
        return self.get_available_days_meta(schedule_id, facility_id)[0]

    def get_available_times(self, schedule_id, facility_id, day):
        return ["08:00"]

    def current_appointment(self, schedule_id):
        return None

    def discover_schedules(self, only_ids=None):
        return [Schedule(id=s, action_label="Schedule Appointment",
                         continue_url=f"/en-ca/niv/schedule/{s}/continue")
                for s in (only_ids or self.sids)]

    def book(self, slot, dry_run=True, retry_attempts=1, retry_delay=0.0):
        self.booked.append(slot)
        return False

    def peek_prewarmed(self, schedule_id, ttl):
        return None

    def prewarm_booking(self, schedule_id, force=False, ttl=120.0):
        return None


def target():
    return Target(consulates=[TRT], earliest=date(2026, 9, 1),
                  latest=date(2026, 12, 31), exclusions=[], prefer_earliest=True)


def build(tmp_path, accounts, clients, **cfg):
    cfg.setdefault("calendar_reverify_sweeps", 1000)
    cfg.setdefault("account_poll_delay", 0)
    config = Config(accounts=accounts, telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), test_mode=True,
                    data_dir=tmp_path, **cfg)
    store = Store(path=config.store_file)
    worker = Worker(config, store,
                    TelegramNotifier(config.telegram, logger=lambda m: None))
    worker.log = lambda m: None

    def client_for(account):
        if getattr(clients[account.name], "login_error", None):
            raise clients[account.name].login_error
        worker._clients[account.name] = clients[account.name]
        return clients[account.name]

    worker._client_for = client_for
    worker._seed_state()
    return worker


def subscriber(name, sid, visa="B2"):
    return Account(name=name, email=f"{name}@e.com", password="x",
                   target=target(), visa_type=visa,
                   applications=[Application(schedule_id=sid, label=sid,
                                             target=target(), visa_type=visa)])


def canary(name, sid, visa="B2"):
    return Account(name=name, email=f"{name}@e.com", password="x",
                   target=target(), canary=True, visa_type=visa,
                   applications=[Application(schedule_id=sid, target=target(),
                                             visa_type=visa)])


def sweep(worker, n):
    worker.store.sweep_count = n
    worker._sweep()


def test_canary_carries_the_polling_and_the_subscriber_books(tmp_path):
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": B2})
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})

    sweep(worker, 1)
    assert sub.calls == [("111", TRT)] and can.calls == [("900", TRT)], \
        "first sweep: target and canary each ask once, to compare ETags"
    sub.calls.clear(); can.calls.clear(); sub.booked.clear()

    sweep(worker, 2)
    assert can.calls == [("900", TRT)], "matched: the canary polls"
    assert sub.calls == [("111", TRT)], "the subscriber asks only to confirm the hit"
    assert [s.schedule_id for s in sub.booked] == ["111"], \
        "booking still happens on the subscriber's own session"
    assert sub.booked[0].day == DAY


def test_matched_canary_without_a_hit_costs_the_subscriber_nothing(tmp_path):
    sub = PoolClient(["111"], {"111": F1})    # days, but outside the window
    can = PoolClient(["900"], {"900": F1})
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
    sub.calls.clear()
    sweep(worker, 2)
    sweep(worker, 3)
    assert sub.calls == [], "nothing to confirm, so the subscriber never polls"
    assert len(can.calls) == 3


def test_mismatched_canary_is_never_used_for_the_target(tmp_path):
    """Same declared visa_type, different calendar (e.g. PR vs non-PR)."""
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": F1})
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
    sub.calls.clear(); can.calls.clear()
    sweep(worker, 2)
    assert can.calls == [], "the canary's ETag did not match"
    assert sub.calls == [("111", TRT)], "the target polls itself"


def test_canary_hit_the_subscriber_cannot_see_is_not_booked(tmp_path):
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": B2})
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
    sub.calls.clear(); sub.booked.clear()
    sub.classes["111"] = F1                   # drifted since calibration
    sweep(worker, 2)
    assert sub.calls == [("111", TRT)], "the hit was re-read on the own account"
    assert sub.booked == [], "and the subscriber's own answer decided"

def test_canary_is_not_shown_as_an_application(tmp_path):
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": PoolClient(["111"], {"111": B2}),
                    "C": PoolClient(["900"], {"900": B2})})
    sweep(worker, 1)
    snap = {a["name"]: a for a in worker.store.snapshot()["accounts"]}
    assert snap["C"]["applications"] == []
    assert [a["schedule_id"] for a in snap["A"]["applications"]] == ["111"]


def test_failed_canary_request_fails_over_to_another_account(tmp_path):
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": B2})
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
    sub.calls.clear(); sub.booked.clear()
    can.fail_days = True

    sweep(worker, 2)
    assert sub.calls == [("111", TRT)], "the subscriber answered instead"
    assert len(sub.booked) == 1, "and the slot was still acted on"


def test_failing_account_rests_then_is_retried(tmp_path, monkeypatch):
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": B2})
    can.login_error = AisError("blocked")
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can}, max_consecutive_failures=5,
                   account_cooldown=300)

    clock = [1000.0]
    monkeypatch.setattr("app.worker.time.monotonic", lambda: clock[0])

    attempts = []
    original = worker._client_for

    def counting(account):
        attempts.append(account.name)
        return original(account)
    worker._client_for = counting

    for n in range(1, 6):
        sweep(worker, n)
    assert attempts.count("C") == 5
    assert not worker._pool_available(canary("C", "900"))

    sweep(worker, 6)
    assert attempts.count("C") == 5, "resting: not even a login attempt"
    assert sub.calls, "the rest of the pool kept polling"

    clock[0] += 301
    can.login_error = None
    sweep(worker, 7)
    assert attempts.count("C") == 6, "retried once the rest is over"
    assert worker._pool_available(canary("C", "900"))
    assert worker._failures["C"] == 0


def test_account_whose_requests_all_fail_rests_without_losing_its_session(tmp_path, monkeypatch):
    sub = PoolClient(["111"], {"111": B2}, fail_days=True)
    worker = build(tmp_path, [subscriber("A", "111")], {"A": sub},
                   max_consecutive_failures=3, account_cooldown=300)
    monkeypatch.setattr("app.worker.time.monotonic", lambda: 50.0)

    for n in range(1, 4):
        sweep(worker, n)
    assert worker._failures["A"] == 3
    assert "A" in worker._clients, "the session is kept"
    assert not worker._pool_available(worker.config.accounts[0])
    assert "resting until" in worker.store.snapshot()["accounts"][0]["error"]


def test_dashboard_context_reports_the_pool(tmp_path):
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": PoolClient(["111"], {"111": B2}),
                    "C": PoolClient(["900"], {"900": B2})})
    sweep(worker, 1)
    sweep(worker, 2)
    pool = {p["account"]: p for p in worker.dashboard_context()["pool"]}
    assert pool["C"]["canary"] and pool["C"]["available"]
    assert pool["C"]["requests_last_sweep"] == 1
    assert pool["A"]["requests_last_sweep"] == 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CFG = """
defaults:
  consulates: [TRT]
  visa_type: b1 / b2
accounts:
  - name: Primary
    email: a@example.com
    password: secret
    applications:
      - schedule_id: "111"
      - schedule_id: "222"
        visa_type: F1
  - name: Old
    email: old@example.com
    password: secret
    canary: true
    applications:
      - schedule_id: "900"
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_visa_type_cascades_and_is_normalised(tmp_path):
    cfg = load_config(write(tmp_path, CFG))
    primary, old = cfg.accounts
    assert [a.visa_type for a in primary.applications] == ["B1/B2", "F1"]
    assert old.canary and old.applications[0].visa_type == "B1/B2"
    assert not old.auto_discover
    assert cfg.account_cooldown == 300
    assert cfg.total_applications() == 2


def test_canary_needs_a_schedule_id(tmp_path):
    text = CFG.replace('    applications:\n      - schedule_id: "900"\n', "")
    with pytest.raises(ConfigError, match="canary"):
        load_config(write(tmp_path, text))


def test_canary_visa_type_is_optional(tmp_path):
    text = CFG.replace("  visa_type: b1 / b2\n", "")
    cfg = load_config(write(tmp_path, text))
    assert cfg.accounts[1].canary and cfg.accounts[1].applications[0].visa_type is None

def test_all_canaries_is_rejected(tmp_path):
    text = """
defaults: {consulates: [TRT], visa_type: B2}
accounts:
  - {name: Old, email: o@e.com, password: x, canary: true,
     applications: [{schedule_id: "900"}]}
"""
    with pytest.raises(ConfigError, match="nothing to book"):
        load_config(write(tmp_path, text))

def test_dashboard_shows_which_canaries_each_applicant_uses(tmp_path):
    worker = build(tmp_path, [subscriber("A", "111"), canary("C1", "901"),
                              canary("C2", "902"), canary("C3", "903")],
                   {"A": PoolClient(["111"], {"111": B2}),
                    "C1": PoolClient(["901"], {"901": B2}),
                    "C2": PoolClient(["902"], {"902": B2}),
                    "C3": PoolClient(["903"], {"903": F1})})
    sweep(worker, 1)
    sweep(worker, 2)
    rows = worker.dashboard_context()["matching"]
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule_id"] == "111" and row["state"] == "matched"
    assert row["canaries"] == ["C1", "C2"], "C3 returned another ETag"
    assert row["answered_by"] in {"C1", "C2"}
