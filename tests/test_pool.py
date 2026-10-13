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


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def subs(*items):
    """items: (account, sid, fid, visa_type)."""
    return [Subscription(a, s, f, t) for a, s, f, t in items]


def answer(planner, plan, etags):
    """Feed the planner the ETags each polled schedule_id 'returned'."""
    from app.planner import PollResult
    results = [PollResult(p, p.poller, [], etags.get(p.poller.schedule_id))
               for p in plan.polls]
    planner.observe(plan, results)
    return results


def test_declared_class_is_shared_from_the_first_sweep():
    planner = CalendarPlanner(reverify_sweeps=100)
    canary = Poller("C", "900", "B2", canary=True)
    wanted = subs(("A", "1", TRT, "B2"), ("B", "2", TRT, "B2"))

    plan = planner.plan(wanted, [canary], sweep=1)
    assert len(plan.polls) == 1, "no verification round: the config is trusted"
    poll = plan.polls[0]
    assert poll.poller.canary, "a same-type canary is preferred"
    assert set(poll.covers) == {"1", "2"}
    assert [c.account for c in poll.candidates] == ["C", "A", "B"], \
        "subscribers stay behind the canary as fallbacks"


def test_etag_disagreement_never_splits_a_declared_class():
    planner = CalendarPlanner(reverify_sweeps=5)
    canary = Poller("C", "900", "B2", canary=True)
    wanted = subs(("A", "1", TRT, "B2"), ("B", "2", TRT, "B2"))
    for n in range(1, 11):                     # includes re-verify sweeps
        plan = planner.plan(wanted, [canary], n)
        assert len(plan.polls) == 1
        assert plan.polls[0].poller.canary
        answer(planner, plan, {"900": F1})     # the canary "is really F1"


def test_requests_spread_over_canaries():
    planner = CalendarPlanner(reverify_sweeps=100)
    canaries = [Poller("C1", "901", "B2", True), Poller("C2", "902", "B2", True)]
    wanted = subs(("A", "1", TRT, "B2"), ("A", "1", VAN, "B2"))
    plan = planner.plan(wanted, canaries, 1)
    assert sorted(p.poller.account for p in plan.polls) == ["C1", "C2"]


def test_untyped_applications_still_share_on_etag_evidence():
    planner = CalendarPlanner(reverify_sweeps=100)
    wanted = subs(("A", "1", TRT, None), ("B", "2", TRT, None))
    first = planner.plan(wanted, [], 1)
    assert len(first.polls) == 2
    answer(planner, first, {"1": B2, "2": B2})

    second = planner.plan(wanted, [], 2)
    assert len(second.polls) == 1, "same ETag across accounts -> one request"


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
        return ([DAY] if etag == B2 else []), etag

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
    assert can.calls == [("900", TRT)], "the canary polls from the first sweep"
    assert sub.calls == [("111", TRT)], "the subscriber asks only to confirm the hit"
    assert [s.schedule_id for s in sub.booked] == ["111"], \
        "booking still happens on the subscriber's own session"
    assert sub.booked[0].day == DAY


def test_quiet_canary_costs_the_subscriber_nothing(tmp_path):
    sub = PoolClient(["111"], {"111": B2})
    can = PoolClient(["900"], {"900": F1})    # answers with no days
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
    sweep(worker, 2)
    assert sub.calls == [], "nothing to confirm, so the subscriber never polls"
    assert len(can.calls) == 2


def test_canary_hit_the_subscriber_cannot_see_is_not_booked(tmp_path):
    sub = PoolClient(["111"], {"111": F1})    # its own calendar: nothing
    can = PoolClient(["900"], {"900": B2})    # canary reports DAY
    worker = build(tmp_path, [subscriber("A", "111"), canary("C", "900")],
                   {"A": sub, "C": can})
    sweep(worker, 1)
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


def test_canary_needs_a_visa_type(tmp_path):
    text = CFG.replace("  visa_type: b1 / b2\n", "")
    with pytest.raises(ConfigError, match="visa_type"):
        load_config(write(tmp_path, text))


def test_all_canaries_is_rejected(tmp_path):
    text = """
defaults: {consulates: [TRT], visa_type: B2}
accounts:
  - {name: Old, email: o@e.com, password: x, canary: true,
     applications: [{schedule_id: "900"}]}
"""
    with pytest.raises(ConfigError, match="nothing to book"):
        load_config(write(tmp_path, text))
