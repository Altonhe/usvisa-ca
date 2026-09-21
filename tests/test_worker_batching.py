"""The sweep must collapse identical calendars into one request and run them
concurrently, rather than paying one delayed request per application per post.

These assert the end-to-end effect on the worker, not just the planner: the
count of calls that actually reach the client.
"""

import threading
import time
from datetime import date

from app.config import Account, Config, DashboardConfig, TelegramConfig, Target
from app.consulates import parse_consulate_list
from app.notifier import TelegramNotifier
from app.store import Store
from app.worker import Worker

B2_TRT = 'W/"604aa903"'
OTHER_TRT = 'W/"6532a9c6"'


def make_target(consulates):
    return Target(consulates=consulates, earliest=None, latest=None,
                  exclusions=[], prefer_earliest=True)


class CountingClient:
    """Records every availability call and hands back a scripted ETag."""

    def __init__(self, etags, days=None, delay=0.0):
        self.etags = etags                      # (sid, fid) -> etag
        self.days = days or {}
        self.delay = delay
        self.calls = []
        self.concurrent_peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def get_available_days_meta(self, schedule_id, facility_id):
        with self._lock:
            self.calls.append((schedule_id, facility_id))
            self._live += 1
            self.concurrent_peak = max(self.concurrent_peak, self._live)
        try:
            if self.delay:
                # A real request takes ~200ms; without some duration here there
                # is no window in which overlap could be observed at all.
                time.sleep(self.delay)
            days = self.days.get((schedule_id, facility_id), [])
            return days, self.etags.get((schedule_id, facility_id))
        finally:
            with self._lock:
                self._live -= 1

    def get_available_days(self, schedule_id, facility_id):
        return self.get_available_days_meta(schedule_id, facility_id)[0]


def build(tmp_path, **cfg):
    config = Config(accounts=[], telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), test_mode=True,
                    data_dir=tmp_path, **cfg)
    store = Store(path=config.store_file)
    worker = Worker(config, store,
                    TelegramNotifier(config.telegram, logger=lambda m: None))
    worker.log = lambda m: None
    return worker


def test_identical_etags_collapse_to_one_request_on_the_next_sweep(tmp_path):
    """Four applications, two visa classes, one consulate -> 4 calls then 2."""
    trt = parse_consulate_list("TRT", default_all=False)
    etags = {("72856817", 94): B2_TRT, ("72856546", 94): B2_TRT,
             ("68324220", 94): OTHER_TRT, ("67078262", 94): OTHER_TRT}
    client = CountingClient(etags)
    worker = build(tmp_path, calendar_reverify_sweeps=100)

    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target(trt))
    pairs = [(sid, 94) for sid in
             ("72856817", "72856546", "68324220", "67078262")]

    worker.store.sweep_count = 1
    first = worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 4, "nothing known yet, so every pair is asked"
    assert set(first) == set(pairs), "every pair still gets an answer"

    client.calls.clear()
    worker.store.sweep_count = 2
    second = worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 2, f"should share, got {client.calls}"
    assert set(second) == set(pairs), "sharing must still answer all four"


def test_reverify_sweep_restores_the_full_request_count(tmp_path):
    etags = {("a", 94): B2_TRT, ("b", 94): B2_TRT}
    client = CountingClient(etags)
    worker = build(tmp_path, calendar_reverify_sweeps=5)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [("a", 94), ("b", 94)]

    worker.store.sweep_count = 1
    worker._fetch_calendars(account, client, pairs)
    client.calls.clear()

    worker.store.sweep_count = 2          # shares
    worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 1
    client.calls.clear()

    worker.store.sweep_count = 5          # re-verifies
    worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 2


def test_sharing_disabled_keeps_one_request_per_pair(tmp_path):
    etags = {("a", 94): B2_TRT, ("b", 94): B2_TRT}
    client = CountingClient(etags)
    worker = build(tmp_path, calendar_sharing=False)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [("a", 94), ("b", 94)]

    for sweep in (1, 2, 3):
        worker.store.sweep_count = sweep
        client.calls.clear()
        worker._fetch_calendars(account, client, pairs)
        assert len(client.calls) == 2, "sharing off means never deduplicate"


def test_requests_actually_run_concurrently(tmp_path):
    """The point of concurrency is that the calls overlap in time."""
    etags = {}
    client = CountingClient(etags, delay=0.05)
    worker = build(tmp_path, max_concurrent_requests=3, consulate_poll_delay=0)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [(str(i), 94) for i in range(6)]

    worker.store.sweep_count = 1
    worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 6
    assert client.concurrent_peak > 1, "calls never overlapped"
    assert client.concurrent_peak <= 3, "exceeded max_concurrent_requests"


def test_single_worker_stays_sequential(tmp_path):
    """max_concurrent_requests=1 must restore the old one-at-a-time behaviour."""
    client = CountingClient({}, delay=0.02)
    worker = build(tmp_path, max_concurrent_requests=1, consulate_poll_delay=0)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [(str(i), 94) for i in range(4)]

    worker.store.sweep_count = 1
    worker._fetch_calendars(account, client, pairs)
    assert len(client.calls) == 4
    assert client.concurrent_peak == 1, "should not overlap when bounded to one"


def test_a_failed_shared_poll_reports_failure_for_everyone_it_covered(tmp_path):
    """A shared request cannot half-succeed: all its readers see the error."""
    etags = {("a", 94): B2_TRT, ("b", 94): B2_TRT}
    client = CountingClient(etags)
    worker = build(tmp_path, calendar_reverify_sweeps=100)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [("a", 94), ("b", 94)]

    worker.store.sweep_count = 1
    worker._fetch_calendars(account, client, pairs)

    # Now make the shared poll fail outright.
    def failing(schedule_id, facility_id):
        return None, None
    client.get_available_days_meta = failing

    worker.store.sweep_count = 2
    out = worker._fetch_calendars(account, client, pairs)
    assert out[("a", 94)] is None and out[("b", 94)] is None


def test_days_are_fanned_out_to_every_covered_application(tmp_path):
    """The shared answer must reach the applications that did not ask."""
    etags = {("a", 94): B2_TRT, ("b", 94): B2_TRT}
    days = {("a", 94): [date(2027, 7, 29)], ("b", 94): [date(2099, 1, 1)]}
    client = CountingClient(etags, days=days)
    worker = build(tmp_path, calendar_reverify_sweeps=100)
    account = Account(name="A", email="a@e.com", password="p",
                      target=make_target([94]))
    pairs = [("a", 94), ("b", 94)]

    worker.store.sweep_count = 1
    worker._fetch_calendars(account, client, pairs)
    client.calls.clear()

    worker.store.sweep_count = 2
    out = worker._fetch_calendars(account, client, pairs)
    polled = client.calls[0][0]
    # Both read the polled application's calendar, which is the whole premise:
    # the server said these two are the same resource.
    assert out[("a", 94)] == out[("b", 94)] == days[(polled, 94)]
