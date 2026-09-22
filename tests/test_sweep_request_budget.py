"""How many availability requests one sweep actually costs.

The target is one request per *distinct calendar*, which on the real
configuration means three: Toronto, Montreal, Vancouver.

Crucially that is not the same as one request per consulate. Measured against
the live host, Toronto serves two different calendars depending on the visa
class of the asking application:

    72856817 + 72856546  @ TRT -> W/"604aa903..."  21 days, earliest 2028-03-16
    68324220 + 67078262  @ TRT -> W/"6532a9c6..."  58 days, earliest 2027-07-29

So deduplicating on facility_id alone would hand one class the other's
calendar. These tests pin both the saving and that correctness boundary.
"""

import threading
from datetime import date

from app.config import (Account, Application, Config, DashboardConfig, Group,
                        Target, TelegramConfig)
from app.notifier import TelegramNotifier
from app.store import Store
from app.worker import Worker

TRT, VAN, MTL = 94, 95, 91

B2_TRT = 'W/"604aa903"'
B2_VAN = 'W/"61be0be1"'
B2_MTL = 'W/"4f53cda1"'
OTHER_TRT = 'W/"6532a9c6"'     # same consulate, different visa class

# Real calendars, per (schedule_id class, facility).
B2 = {TRT: [date(2028, 3, 16)], VAN: [date(2027, 10, 26)], MTL: []}
OTHER = {TRT: [date(2027, 7, 29)]}


class Recorder:
    """Counts availability calls and serves class-correct ETags."""

    def __init__(self, classes):
        # classes: schedule_id -> "b2" | "other"
        self.classes = classes
        self.calls = []
        self._lock = threading.Lock()

    def _cal(self, schedule_id, facility_id):
        if self.classes.get(schedule_id) == "other":
            return OTHER.get(facility_id, []), OTHER_TRT
        etag = {TRT: B2_TRT, VAN: B2_VAN, MTL: B2_MTL}.get(facility_id)
        return B2.get(facility_id, []), etag

    def get_available_days_meta(self, schedule_id, facility_id):
        with self._lock:
            self.calls.append((schedule_id, facility_id))
        return self._cal(schedule_id, facility_id)

    def get_available_days(self, schedule_id, facility_id):
        return self.get_available_days_meta(schedule_id, facility_id)[0]

    def get_available_times(self, schedule_id, facility_id, day):
        return []

    def current_appointment(self, schedule_id):
        return None

    def discover_schedules(self, only_ids=None):
        from app.ais_client import Schedule
        return [Schedule(id=sid, action_label="Schedule Appointment",
                         continue_url=f"/en-ca/niv/schedule/{sid}/continue")
                for sid in (only_ids or self.classes or [])]

    # session / prewarm surface the worker may touch
    def load_cookies(self, path): return False
    def resume_session(self): return False
    def save_cookies(self, path): pass
    def login(self): pass
    def prewarm_booking(self, schedule_id, force=False, ttl=120.0): return None
    def peek_prewarmed(self, schedule_id, ttl): return None
    def invalidate_prewarm(self, schedule_id=None): pass

    @property
    def facilities_touched(self):
        return sorted({f for _, f in self.calls})


def target(consulates):
    return Target(consulates=list(consulates), earliest=None, latest=None,
                  exclusions=[], prefer_earliest=True)


def build(tmp_path, accounts, client, **cfg):
    cfg.setdefault("calendar_reverify_sweeps", 1000)   # keep re-verify out of the way
    config = Config(accounts=accounts, telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), test_mode=True,
                    data_dir=tmp_path, **cfg)
    store = Store(path=config.store_file)
    worker = Worker(config, store,
                   TelegramNotifier(config.telegram, logger=lambda m: None))
    worker.log = lambda m: None
    worker._client_for = lambda account: client
    return worker


def real_config_accounts():
    """Primary: two B2 applicants at TRT plus a joint MTL group.
    Secondary: one B2 applicant at VAN."""
    primary = Account(
        name="Primary", email="p@e.com", password="x", target=target([TRT]),
        applications=[Application(schedule_id=s, label=s, target=target([TRT]))
                      for s in ("72856817", "72856546")])
    primary.groups = [Group(members=["72856817", "72856546"], consulates=[MTL],
                            target=target([MTL]), min_slots=2)]
    secondary = Account(
        name="Secondary", email="s@e.com", password="x", target=target([VAN]),
        applications=[Application(schedule_id="76138658", label="d",
                                  target=target([VAN]))])
    return [primary, secondary]


def test_steady_state_costs_one_request_per_distinct_calendar(tmp_path):
    """The goal: TRT once, MTL once, VAN once."""
    client = Recorder({})
    worker = build(tmp_path, real_config_accounts(), client)

    worker.store.sweep_count = 1
    worker._sweep()                      # learns the ETags
    first = len(client.calls)

    client.calls.clear()
    worker.store.sweep_count = 2
    worker._sweep()                      # now deduplicated

    assert client.facilities_touched == [MTL, TRT, VAN]
    assert len(client.calls) == 3, (
        f"expected one request per consulate, got {client.calls}")
    assert first > 3, "first sweep must actually probe before it can share"


def test_the_group_no_longer_asks_once_per_member(tmp_path):
    """Regression: the joint-constraint path used to bypass the cache."""
    client = Recorder({})
    worker = build(tmp_path, real_config_accounts(), client)
    worker.store.sweep_count = 1
    worker._sweep()
    client.calls.clear()
    worker.store.sweep_count = 2
    worker._sweep()

    mtl = [c for c in client.calls if c[1] == MTL]
    assert len(mtl) == 1, f"Montreal should be asked once, got {mtl}"


def test_two_visa_classes_at_one_consulate_stay_separate(tmp_path):
    """The correctness boundary: same consulate, different calendars.

    Deduplicating on facility_id would collapse these and silently give one
    class the other's availability.
    """
    account = Account(
        name="A", email="a@e.com", password="x", target=target([TRT]),
        applications=[
            Application(schedule_id="72856817", label="b2a", target=target([TRT])),
            Application(schedule_id="72856546", label="b2b", target=target([TRT])),
            Application(schedule_id="68324220", label="o1", target=target([TRT])),
            Application(schedule_id="67078262", label="o2", target=target([TRT])),
        ])
    client = Recorder({"68324220": "other", "67078262": "other"})
    worker = build(tmp_path, [account], client)

    worker.store.sweep_count = 1
    worker._sweep()
    client.calls.clear()
    worker.store.sweep_count = 2
    worker._sweep()

    assert len(client.calls) == 2, (
        "four applications, one consulate, but TWO calendars -> two requests")

    # And each application must end up with its own class's availability.
    snap = worker.store.snapshot()
    by_label = {a["label"]: a for acc in snap["accounts"] for a in acc["applications"]}
    b2_earliest = by_label["b2a"]["consulates"][0]["earliest"]
    other_earliest = by_label["o1"]["consulates"][0]["earliest"]
    assert b2_earliest == "2028-03-16"
    assert other_earliest == "2027-07-29"
    assert b2_earliest != other_earliest, "classes must not be cross-contaminated"


def test_siblings_in_one_class_all_receive_the_shared_answer(tmp_path):
    account = Account(
        name="A", email="a@e.com", password="x", target=target([TRT]),
        applications=[
            Application(schedule_id="72856817", label="b2a", target=target([TRT])),
            Application(schedule_id="72856546", label="b2b", target=target([TRT])),
        ])
    client = Recorder({})
    worker = build(tmp_path, [account], client)
    worker.store.sweep_count = 1
    worker._sweep()
    client.calls.clear()
    worker.store.sweep_count = 2
    worker._sweep()

    assert len(client.calls) == 1
    snap = worker.store.snapshot()
    earliest = {a["label"]: a["consulates"][0]["earliest"]
                for acc in snap["accounts"] for a in acc["applications"]}
    assert earliest == {"b2a": "2028-03-16", "b2b": "2028-03-16"}
