"""An existing appointment may only ever be moved earlier.

Rescheduling spends one of the limited reschedules the site allows, and moving
an appointment later is never what anyone wants, so this is checked both when
candidates are chosen and once more right before anything is POSTed.
"""

from datetime import date

from app.ais_client import Schedule, Slot
from app.config import (Account, Application, Config, DashboardConfig, Group,
                        Target, TelegramConfig)
from app.notifier import TelegramNotifier
from app.store import Store
from app.worker import Worker

TRT = 94


class FakeClient:
    def __init__(self, days=None, times=None):
        self.days = days or {}
        self.times = times or {}
        self.booked = []

    def get_available_days(self, schedule_id, facility_id):
        return self.days.get((schedule_id, facility_id), [])

    def get_available_days_meta(self, schedule_id, facility_id):
        return self.get_available_days(schedule_id, facility_id), None

    def get_available_times(self, schedule_id, facility_id, day):
        return self.times.get(schedule_id, ["08:00"])

    def current_appointment(self, schedule_id):
        return None

    def book(self, slot, dry_run=True, retry_attempts=1, retry_delay=0.0):
        self.booked.append(slot)
        return False


def target(earliest=date(2026, 9, 1), latest=date(2027, 12, 31)):
    return Target(consulates=[TRT], earliest=earliest, latest=latest,
                  exclusions=[], prefer_earliest=True)


def reschedule(sid="111", current="1 March, 2027"):
    return Schedule(id=sid, action_label="Reschedule Appointment",
                    continue_url=f"/en-ca/niv/schedule/{sid}/continue",
                    current_appointment=current)


def first_time(sid="111"):
    return Schedule(id=sid, action_label="Schedule Appointment",
                    continue_url=f"/en-ca/niv/schedule/{sid}/continue")


def build(tmp_path, sids=("111",)):
    config = Config(accounts=[], telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), test_mode=True, data_dir=tmp_path)
    store = Store(path=config.store_file)
    worker = Worker(config, store,
                    TelegramNotifier(config.telegram, logger=lambda m: None))
    worker.log = lambda m: None
    account = Account(name="A", email="a@e.com", password="p", target=target())
    store.register_account("A", "a@e.com")
    for sid in sids:
        store.register_application("A", sid, sid, target())
    return worker, account


def sweep(worker, account, client, schedule, days):
    app = Application(schedule_id=schedule.id, label=schedule.id, target=target())
    worker._sweep_application(account, client, app, schedule,
                              {(schedule.id, TRT): days})


# ---------------------------------------------------------------------------
# _booking_ceiling
# ---------------------------------------------------------------------------

def test_ceiling_is_the_current_appointment():
    assert Worker._booking_ceiling(reschedule()) == (True, date(2027, 3, 1))


def test_first_time_application_has_no_ceiling():
    assert Worker._booking_ceiling(first_time()) == (True, None)


def test_reschedule_with_unreadable_date_is_blocked():
    assert Worker._booking_ceiling(reschedule(current="sometime soon")) == (False, None)
    assert Worker._booking_ceiling(reschedule(current=None)) == (False, None)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def test_later_slot_inside_the_window_is_not_booked(tmp_path):
    """The bug this guards: in-window but later than the current appointment."""
    worker, account = build(tmp_path)
    client = FakeClient()
    sweep(worker, account, client, reschedule(), [date(2027, 6, 1)])
    assert client.booked == []


def test_earlier_slot_is_booked(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    sweep(worker, account, client, reschedule(),
          [date(2027, 1, 15), date(2027, 6, 1)])
    assert [s.day for s in client.booked] == [date(2027, 1, 15)]


def test_same_day_as_current_is_not_a_move(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    sweep(worker, account, client, reschedule(), [date(2027, 3, 1)])
    assert client.booked == []


def test_unreadable_current_date_never_books(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    sweep(worker, account, client, reschedule(current=""), [date(2026, 10, 1)])
    assert client.booked == []


def test_first_time_booking_is_unchanged(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    sweep(worker, account, client, first_time(), [date(2027, 6, 1)])
    assert [s.day for s in client.booked] == [date(2027, 6, 1)]


def test_acceptable_count_reflects_only_earlier_days(tmp_path):
    worker, account = build(tmp_path)
    sweep(worker, account, FakeClient(), reschedule(),
          [date(2027, 1, 15), date(2027, 2, 1), date(2027, 6, 1)])
    cons = worker.store.snapshot()["accounts"][0]["applications"][0]["consulates"][0]
    assert cons["acceptable_count"] == 2
    assert cons["earliest_acceptable"] == "2027-01-15"


# ---------------------------------------------------------------------------
# Final guard before POST
# ---------------------------------------------------------------------------

def test_attempt_booking_refuses_a_later_slot_whatever_the_caller(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    app = Application(schedule_id="111", label="x", target=target())
    worker._attempt_booking(account, client, app,
                            Slot("111", TRT, date(2027, 6, 1)), reschedule())
    assert client.booked == []
    msg = worker.store.snapshot()["accounts"][0]["applications"][0]["message"]
    assert "not earlier than current 2027-03-01" in msg


def test_attempt_booking_allows_an_earlier_slot(tmp_path):
    worker, account = build(tmp_path)
    client = FakeClient()
    app = Application(schedule_id="111", label="x", target=target())
    worker._attempt_booking(account, client, app,
                            Slot("111", TRT, date(2027, 1, 15)), reschedule())
    assert len(client.booked) == 1


# ---------------------------------------------------------------------------
# Groups: one shared day must be earlier for every member
# ---------------------------------------------------------------------------

def _group_sweep(tmp_path, schedules, common_day):
    sids = [s.id for s in schedules]
    worker, account = build(tmp_path, sids=sids)
    client = FakeClient(days={(sid, TRT): [common_day] for sid in sids},
                        times={sid: ["08:00", "09:00"] for sid in sids})
    by_id = {s.id: (Application(schedule_id=s.id, label=s.id, target=target()), s)
             for s in schedules}
    group = Group(members=sids, consulates=[TRT], target=target(), min_slots=2)
    worker._sweep_group_facility(account, client, group, sids, by_id, TRT, "g")
    return client


def test_group_day_later_than_any_members_appointment_is_not_booked(tmp_path):
    client = _group_sweep(
        tmp_path,
        [reschedule("111", "1 March, 2027"), reschedule("222", "1 December, 2026")],
        date(2027, 1, 15),   # earlier for 111, later for 222
    )
    assert client.booked == []


def test_group_day_earlier_than_every_appointment_is_booked(tmp_path):
    client = _group_sweep(
        tmp_path,
        [reschedule("111", "1 March, 2027"), first_time("222")],
        date(2027, 1, 15),
    )
    assert sorted(s.schedule_id for s in client.booked) == ["111", "222"]


def test_group_with_an_unreadable_member_date_is_not_booked(tmp_path):
    client = _group_sweep(
        tmp_path,
        [reschedule("111", ""), first_time("222")],
        date(2026, 10, 1),
    )
    assert client.booked == []
