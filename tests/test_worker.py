"""Worker decision logic (no network)."""

from datetime import date

import pytest

from app.ais_client import Schedule, Slot
from app.config import (Config, DashboardConfig, Target, TelegramConfig)
from app.consulates import parse_consulate_list
from app.notifier import TelegramNotifier
from app.store import ConsulateStatus, Store
from app.worker import Worker


def build_worker(tmp_path):
    config = Config(
        accounts=[],
        telegram=TelegramConfig(),
        dashboard=DashboardConfig(),
        test_mode=True,
        data_dir=tmp_path,
    )
    store = Store(path=config.store_file)
    return Worker(config, store, TelegramNotifier(config.telegram, logger=lambda m: None)), store


def make_target(**kw):
    return Target(
        consulates=kw.get("consulates", parse_consulate_list("TRT,VAN", default_all=False)),
        earliest=kw.get("earliest"),
        latest=kw.get("latest"),
        exclusions=kw.get("exclusions", []),
        prefer_earliest=kw.get("prefer_earliest", True),
    )


# ---------------------------------------------------------------------------
# _all_done: a failed login must never look like "everything is booked"
# ---------------------------------------------------------------------------

def test_all_done_false_with_no_accounts(tmp_path):
    worker, _ = build_worker(tmp_path)
    assert worker._all_done() is False


def test_all_done_false_when_account_registered_but_login_failed(tmp_path):
    """Regression: a login failure registers the account with zero applications.

    Treating that as "done" stopped polling after one transient failure.
    """
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    store.set_account_error("A", "login failed: sign in rejected")
    assert worker._all_done() is False


def test_all_done_false_when_account_has_no_applications_yet(tmp_path):
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    assert worker._all_done() is False


def test_all_done_false_while_watching(tmp_path):
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    store.register_application("A", "111", "X", make_target())
    store.update_application("A", "111", kind="first_time")
    assert worker._all_done() is False


def test_all_done_true_when_everything_booked(tmp_path):
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    store.register_application("A", "111", "X", make_target())
    store.update_application("A", "111", kind="first_time", booked_for=date(2026, 12, 1))
    assert worker._all_done() is True


def test_all_done_true_when_nothing_actionable(tmp_path):
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    store.register_application("A", "111", "X", make_target())
    store.update_application("A", "111", kind="done")
    assert worker._all_done() is True


def test_all_done_false_if_any_account_errored(tmp_path):
    worker, store = build_worker(tmp_path)
    store.register_account("A", "a@example.com")
    store.register_application("A", "111", "X", make_target())
    store.update_application("A", "111", booked_for=date(2026, 12, 1))
    store.register_account("B", "b@example.com")
    store.set_account_error("B", "login failed")
    assert worker._all_done() is False


# ---------------------------------------------------------------------------
# _pick
# ---------------------------------------------------------------------------

def test_pick_prefers_earliest_across_consulates():
    target = make_target(prefer_earliest=True)          # [Toronto(94), Vancouver(95)]
    candidates = [
        Slot("1", 94, date(2028, 2, 17)),
        Slot("1", 95, date(2027, 10, 7)),
    ]
    # Vancouver is later in the preference list but much earlier in time.
    assert Worker._pick(candidates, target).facility_id == 95


def test_pick_breaks_date_ties_by_preference_order():
    target = make_target(prefer_earliest=True)
    candidates = [
        Slot("1", 95, date(2027, 5, 1)),
        Slot("1", 94, date(2027, 5, 1)),
    ]
    assert Worker._pick(candidates, target).facility_id == 94


def test_pick_honours_strict_consulate_order():
    target = make_target(prefer_earliest=False)
    candidates = [
        Slot("1", 94, date(2028, 2, 17)),
        Slot("1", 95, date(2027, 10, 7)),
    ]
    # First acceptable in list order wins, even though it is later.
    assert Worker._pick(candidates, target).facility_id == 94


# ---------------------------------------------------------------------------
# Date acceptance -- the exclusion bug the original code had
# ---------------------------------------------------------------------------

def test_exclusion_actually_blocks_a_date():
    """The old code used `continue` inside a for loop, which only advanced the

    inner loop and then booked the excluded date anyway.
    """
    target = make_target(
        earliest=date(2026, 1, 1),
        latest=date(2026, 12, 31),
        exclusions=[(date(2026, 6, 1), date(2026, 6, 30))],
    )
    assert target.accepts(date(2026, 5, 31))
    assert not target.accepts(date(2026, 6, 15))
    assert target.excluded_by(date(2026, 6, 15)) == (date(2026, 6, 1), date(2026, 6, 30))
    assert target.accepts(date(2026, 7, 1))


def test_window_boundaries_are_inclusive():
    target = make_target(earliest=date(2026, 1, 1), latest=date(2026, 12, 31))
    assert target.accepts(date(2026, 1, 1))
    assert target.accepts(date(2026, 12, 31))
    assert not target.accepts(date(2025, 12, 31))
    assert not target.accepts(date(2027, 1, 1))


# ---------------------------------------------------------------------------
# Schedule classification, mirroring the live account
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,url,expected_kind",
    [
        ("Schedule Appointment", "/en-ca/niv/schedule/72856817/continue", "first_time"),
        ("Reschedule Appointment", "/en-ca/niv/schedule/67078262/continue", "reschedule"),
        ("", "", "done"),
    ],
)
def test_schedule_kind(label, url, expected_kind):
    assert Schedule(id="x", action_label=label, continue_url=url).kind == expected_kind


def test_completed_application_is_not_actionable():
    # 67078262 / 68324220 on the live account offer no scheduling link at all.
    sched = Schedule(id="67078262", current_appointment="9 January, 2026")
    assert sched.actionable is False
    assert sched.kind == "done"
    assert "no scheduling action" in sched.describe()


def test_consulate_status_summaries():
    assert ConsulateStatus(facility_id=94, error="boom").summary == "error"
    assert ConsulateStatus(facility_id=94).summary == "none"
    assert ConsulateStatus(
        facility_id=94, total_days=5, acceptable_count=2
    ).summary == "match"
    assert ConsulateStatus(
        facility_id=94, total_days=5, acceptable_count=0
    ).summary == "out-of-window"
