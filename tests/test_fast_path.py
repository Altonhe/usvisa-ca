"""The pieces that make a match turn into a booking quickly.

Three separate concerns, all verified against behaviour observed on the live
host: conditional requests (the site answers If-None-Match with a 304), the
pre-warmed booking form (the appointment page serves #appointment-form directly
with a populated authenticity_token), and the adaptive poll interval.
"""

from datetime import date

import pytest

from app.ais_client import AisClient, PrewarmedForm, Slot
from app.config import Config, DashboardConfig, TelegramConfig
from app.notifier import TelegramNotifier
from app.store import Store
from app.worker import Worker

ETAG = 'W/"604aa90315c8ca4e3fa894aa08a678fe"'


def client():
    return AisClient("a@example.com", "p", logger=lambda m: None)


class Resp:
    def __init__(self, status, payload=None, etag=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = {"ETag": etag} if etag else {}
        self.text = text
        self.url = "https://ais.usvisa-info.com/x"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ---------------------------------------------------------------------------
# Conditional requests
# ---------------------------------------------------------------------------

def test_first_call_sends_no_conditional_header_and_caches_the_etag(monkeypatch):
    c = client()
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(headers or {})
        return Resp(200, [{"date": "2027-07-29", "business_day": True}], etag=ETAG)

    monkeypatch.setattr(c.session, "get", fake_get)
    days, etag = c.get_available_days_meta("111", 94)

    assert "If-None-Match" not in seen[0]
    assert days == [date(2027, 7, 29)]
    assert etag == ETAG


def test_second_call_revalidates_and_a_304_replays_the_cached_body(monkeypatch):
    """A 304 carries no body, so the previous parse has to stand in for it."""
    c = client()
    seen = []
    responses = [
        Resp(200, [{"date": "2027-07-29", "business_day": True}], etag=ETAG),
        Resp(304, etag=ETAG),
    ]

    def fake_get(url, headers=None, timeout=None):
        seen.append(headers or {})
        return responses.pop(0)

    monkeypatch.setattr(c.session, "get", fake_get)
    first, _ = c.get_available_days_meta("111", 94)
    second, etag = c.get_available_days_meta("111", 94)

    assert seen[1]["If-None-Match"] == ETAG, "must revalidate once an ETag is known"
    assert second == first == [date(2027, 7, 29)], "304 must not look like no data"
    assert etag == ETAG


def test_a_304_with_nothing_cached_is_not_invented_as_data(monkeypatch):
    c = client()
    monkeypatch.setattr(c.session, "get",
                        lambda url, headers=None, timeout=None: Resp(304))
    days, etag = c.get_available_days_meta("111", 94)
    assert days is None and etag is None


def test_empty_calendar_is_data_not_an_error(monkeypatch):
    """Toronto returning [] is a successful poll; only failures give None."""
    c = client()
    monkeypatch.setattr(c.session, "get",
                        lambda url, headers=None, timeout=None: Resp(200, [], etag=ETAG))
    days, etag = c.get_available_days_meta("111", 91)
    assert days == [] and etag == ETAG


def test_non_200_is_reported_as_failure(monkeypatch):
    c = client()
    monkeypatch.setattr(c.session, "get",
                        lambda url, headers=None, timeout=None: Resp(503))
    days, etag = c.get_available_days_meta("111", 94)
    assert days is None and etag is None


# ---------------------------------------------------------------------------
# Pre-warmed booking
# ---------------------------------------------------------------------------

def _warm(known_times=None):
    import time as _t
    return PrewarmedForm(
        schedule_id="111", action="https://ais.usvisa-info.com/x",
        payload={"authenticity_token": "tok", "confirmed_limit_message": "1"},
        referer="https://ais.usvisa-info.com/ref", csrf_token="tok",
        fetched_at=_t.monotonic(), known_times=known_times or {},
    )


def test_prewarmed_form_expires_on_its_ttl():
    import time as _t
    stale = _warm()
    stale.fetched_at = _t.monotonic() - 500
    assert not stale.is_fresh(120)
    assert _warm().is_fresh(120)


def test_peek_returns_nothing_once_stale():
    import time as _t
    c = client()
    warm = _warm()
    c._prewarmed["111"] = warm
    assert c.peek_prewarmed("111", 120) is warm
    warm.fetched_at = _t.monotonic() - 500
    assert c.peek_prewarmed("111", 120) is None


def test_dry_run_prewarmed_booking_sends_nothing(monkeypatch):
    c = client()
    posted = []
    monkeypatch.setattr(c, "get_available_times", lambda s, f, d: ["07:15"])
    monkeypatch.setattr(c.session, "post",
                        lambda *a, **k: posted.append(a) or Resp(200))
    slot = Slot("111", 94, date(2027, 7, 29))

    assert c.book_prewarmed(slot, _warm(), dry_run=True) is False
    assert posted == [], "test_mode must never submit"
    assert slot.time == "07:15", "but it should still resolve the slot it would take"


def test_prewarmed_booking_posts_the_expected_payload(monkeypatch):
    c = client()
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return Resp(200, text="successfully scheduled")

    monkeypatch.setattr(c, "get_available_times", lambda s, f, d: ["07:15", "08:00"])
    monkeypatch.setattr(c.session, "post", fake_post)

    slot = Slot("111", 94, date(2027, 7, 29))
    assert c.book_prewarmed(slot, _warm(), dry_run=False) is True

    data = captured["data"]
    assert data["appointments[consulate_appointment][facility_id]"] == "94"
    assert data["appointments[consulate_appointment][date]"] == "2027-07-29"
    assert data["appointments[consulate_appointment][time]"] == "07:15"
    assert data["authenticity_token"] == "tok"
    assert captured["headers"]["X-CSRF-Token"] == "tok"
    # A consumed token must not be reused.
    assert c.peek_prewarmed("111", 120) is None


def test_prewarmed_booking_gives_up_when_the_day_has_no_times(monkeypatch):
    c = client()
    monkeypatch.setattr(c, "get_available_times", lambda s, f, d: [])
    monkeypatch.setattr(c.session, "post",
                        lambda *a, **k: pytest.fail("must not POST without a time"))
    slot = Slot("111", 94, date(2027, 7, 29))
    assert c.book_prewarmed(slot, _warm(), dry_run=False) is False


def test_speculative_skips_times_json_when_a_roster_is_remembered(monkeypatch):
    """A remembered time lets the POST go out without waiting for times.json."""
    c = client()
    calls = {"times": 0, "posts": []}

    def no_times(s, f, d):
        calls["times"] += 1
        return ["07:15"]

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        calls["posts"].append(data["appointments[consulate_appointment][time]"])
        return Resp(200, text="successfully scheduled")

    monkeypatch.setattr(c, "get_available_times", no_times)
    monkeypatch.setattr(c.session, "post", fake_post)

    slot = Slot("111", 94, date(2027, 7, 29))
    ok = c.book_prewarmed(slot, _warm({94: ["07:15"]}),
                          dry_run=False, speculative=True)
    assert ok is True
    assert calls["times"] == 0, "the whole point is to skip that round trip"
    assert calls["posts"] == ["07:15"]


def test_speculative_falls_back_when_the_guess_is_stale(monkeypatch):
    c = client()
    calls = {"times": 0, "posts": []}

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        t = data["appointments[consulate_appointment][time]"]
        calls["posts"].append(t)
        return Resp(200, text="successfully scheduled" if t == "09:30" else "no luck")

    monkeypatch.setattr(c, "current_appointment", lambda s: None)
    monkeypatch.setattr(c, "get_available_times",
                        lambda s, f, d: (calls.__setitem__("times", calls["times"] + 1),
                                         ["09:30"])[1])
    monkeypatch.setattr(c.session, "post", fake_post)

    slot = Slot("111", 94, date(2027, 7, 29))
    ok = c.book_prewarmed(slot, _warm({94: ["07:15"]}),
                          dry_run=False, speculative=True)
    assert ok is True
    assert calls["posts"] == ["07:15", "09:30"], "guess first, then the real time"
    assert calls["times"] == 1


def test_speculative_is_inert_without_a_remembered_roster(monkeypatch):
    c = client()
    calls = {"times": 0}
    monkeypatch.setattr(c, "get_available_times",
                        lambda s, f, d: (calls.__setitem__("times", 1), ["07:15"])[1])
    monkeypatch.setattr(c.session, "post",
                        lambda *a, **k: Resp(200, text="successfully scheduled"))
    slot = Slot("111", 94, date(2027, 7, 29))
    c.book_prewarmed(slot, _warm(), dry_run=False, speculative=True)
    assert calls["times"] == 1, "no roster means no guess, so times.json is required"


def test_remember_times_records_the_roster_for_later():
    c = client()
    c._prewarmed["111"] = _warm()
    c.remember_times("111", 94, ["07:15", "08:00"])
    assert c._prewarmed["111"].known_times[94] == ["07:15", "08:00"]
    # An empty roster teaches nothing and must not erase what is known.
    c.remember_times("111", 94, [])
    assert c._prewarmed["111"].known_times[94] == ["07:15", "08:00"]


# ---------------------------------------------------------------------------
# Adaptive interval, keyed on consulate-local time
# ---------------------------------------------------------------------------

TRT, VAN = 94, 95


def _worker(tmp_path, consulates=(TRT,), **cfg):
    """A worker watching `consulates`, so local-hour lookups have something to read."""
    from app.config import Account, Target

    target = Target(consulates=list(consulates), earliest=None, latest=None,
                    exclusions=[], prefer_earliest=True)
    accounts = [Account(name="A", email="a@e.com", password="p", target=target)]
    config = Config(accounts=accounts, telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), data_dir=tmp_path, **cfg)
    store = Store(path=config.store_file)
    return Worker(config, store,
                  TelegramNotifier(config.telegram, logger=lambda m: None))


def _at(monkeypatch, hours):
    """Pin the local hour reported for each facility id."""
    import app.worker as mod
    monkeypatch.setattr(mod, "consulate_local_hour", lambda fid: hours[fid])


def test_interval_is_unchanged_when_no_active_hours_are_set(tmp_path):
    w = _worker(tmp_path, poll_interval=300, fast_poll_interval=10)
    assert w._current_interval() == 300, "fast interval needs a window to apply in"


def test_interval_is_unchanged_when_fast_interval_is_zero(tmp_path):
    w = _worker(tmp_path, poll_interval=300, fast_poll_interval=0,
                active_hours=[(9, 17)])
    assert w._current_interval() == 300, "0 means the feature is off"


def test_fast_interval_applies_inside_an_active_window(tmp_path, monkeypatch):
    _at(monkeypatch, {TRT: 10})
    w = _worker(tmp_path, poll_interval=300, fast_poll_interval=15,
                active_hours=[(9, 17)])
    assert w._current_interval() == 15


def test_slow_interval_applies_outside_the_window(tmp_path, monkeypatch):
    _at(monkeypatch, {TRT: 3})
    w = _worker(tmp_path, poll_interval=300, fast_poll_interval=15,
                active_hours=[(9, 17)])
    assert w._current_interval() == 300


def test_a_window_that_wraps_midnight_is_handled(tmp_path, monkeypatch):
    for hour, expected in ((23, 15), (2, 15), (12, 300)):
        _at(monkeypatch, {TRT: hour})
        w = _worker(tmp_path, poll_interval=300, fast_poll_interval=15,
                    active_hours=[(22, 6)])
        assert w._current_interval() == expected, f"hour {hour}"


def test_hours_are_read_at_the_consulate_not_on_this_machine(tmp_path, monkeypatch):
    """The whole point: Toronto and Vancouver are three hours apart.

    At 08:00 Eastern it is 05:00 Pacific. A watcher on Vancouver must not be
    told the window is open just because it is open in Toronto.
    """
    # Vancouver only: 05:00 local, outside a 08-18 window.
    _at(monkeypatch, {TRT: 8, VAN: 5})
    w = _worker(tmp_path, consulates=(VAN,), poll_interval=300,
                fast_poll_interval=15, active_hours=[(8, 18)])
    assert w._current_interval() == 300, "05:00 Pacific is not business hours"

    # Toronto only, same instant: 08:00 local, inside the window.
    w = _worker(tmp_path, consulates=(TRT,), poll_interval=300,
                fast_poll_interval=15, active_hours=[(8, 18)])
    assert w._current_interval() == 15


def test_any_watched_consulate_inside_the_window_opens_it(tmp_path, monkeypatch):
    """A sweep is global, so it cannot speed up for one post alone."""
    _at(monkeypatch, {TRT: 8, VAN: 5})
    w = _worker(tmp_path, consulates=(VAN, TRT), poll_interval=300,
                fast_poll_interval=15, active_hours=[(8, 18)])
    assert w._current_interval() == 15, "Toronto is open, so the sweep goes fast"


def test_watched_consulates_covers_applications_and_groups(tmp_path):
    from app.config import Account, Application, Group, Target

    def target(cons):
        return Target(consulates=list(cons), earliest=None, latest=None,
                      exclusions=[], prefer_earliest=True)

    account = Account(name="A", email="a@e.com", password="p", target=target([TRT]),
                      applications=[Application(schedule_id="1", label="x",
                                                target=target([VAN]))])
    account.groups = [Group(members=["1"], consulates=[91], target=target([91]),
                            min_slots=2)]
    config = Config(accounts=[account], telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), data_dir=tmp_path)
    store = Store(path=config.store_file)
    w = Worker(config, store,
               TelegramNotifier(config.telegram, logger=lambda m: None))

    found = w._watched_consulates()
    assert set(found) == {TRT, VAN, 91}
    assert len(found) == len(set(found)), "must not repeat a facility"
