"""Worker decision logic (no network)."""

from datetime import date

import pytest

from app.ais_client import AisError, Schedule, SessionExpired, Slot
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



# ---------------------------------------------------------------------------
# Session lifetime: one login, reused across sweeps
# ---------------------------------------------------------------------------

class FakeClient:
    """Counts logins so session reuse can be asserted."""

    logins = 0

    def __init__(self, *a, **kw):
        self.landing_url = "https://ais.usvisa-info.com/en-ca/niv/groups/1"

    def login(self):
        FakeClient.logins += 1

    def discover_schedules(self, only_ids=None):
        return [Schedule(id="111", action_label="Schedule Appointment",
                         continue_url="/en-ca/niv/schedule/111/continue")]

    def get_available_days(self, schedule_id, facility_id):
        return []

    def current_appointment(self, schedule_id):
        return None


def test_login_happens_once_across_many_sweeps(tmp_path, monkeypatch):
    """Re-authenticating every sweep is exactly the pattern that arms the

    captcha, so the session must be cached between sweeps.
    """
    from app.config import Account, Target

    FakeClient.logins = 0
    monkeypatch.setattr("app.worker.AisClient", FakeClient)

    worker, store = build_worker(tmp_path)
    worker.config.accounts = [Account(
        name="A", email="a@example.com", password="p",
        target=make_target(consulates=[94]),
    )]
    worker._seed_state()
    for _ in range(5):
        worker._sweep()
    assert FakeClient.logins == 1


def test_failure_forces_a_fresh_login_next_sweep(tmp_path, monkeypatch):
    from app.config import Account

    class Flaky(FakeClient):
        calls = 0

        def discover_schedules(self, only_ids=None):
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise AisError("transient")
            return super().discover_schedules(only_ids)

    FakeClient.logins = 0
    Flaky.calls = 0
    monkeypatch.setattr("app.worker.AisClient", Flaky)

    worker, store = build_worker(tmp_path)
    worker.config.accounts = [Account(
        name="A", email="a@example.com", password="p",
        target=make_target(consulates=[94]),
    )]
    worker._seed_state()
    worker._sweep()          # login #1
    worker._sweep()          # fails, drops the session
    worker._sweep()          # login #2
    assert FakeClient.logins == 2


def test_expired_session_is_reported_as_such_not_as_a_layout_change(tmp_path, monkeypatch):
    """A lapsed session serves the sign-in page with HTTP 200.

    Reporting that as "the landing page layout may have changed" sends you
    hunting for a parser bug that does not exist.
    """
    from app.config import Account

    class Expired(FakeClient):
        def discover_schedules(self, only_ids=None):
            raise SessionExpired("the AIS session has lapsed; re-authenticating")

    FakeClient.logins = 0
    monkeypatch.setattr("app.worker.AisClient", Expired)

    worker, store = build_worker(tmp_path)
    worker.config.accounts = [Account(
        name="A", email="a@example.com", password="p",
        target=make_target(consulates=[94]),
    )]
    logged = []
    worker.log = logged.append
    worker._seed_state()
    worker._sweep()

    joined = "\n".join(logged)
    assert "lapsed" in joined
    assert "layout" not in joined
    # An expected re-auth must not count towards the failure budget or alert.
    assert worker._failures.get("A", 0) == 0
    assert store.snapshot()["accounts"][0]["error"] == ""
    # ...but the session is dropped so the next sweep signs in again.
    worker._sweep()
    assert FakeClient.logins == 2


def test_session_expiry_detected_from_the_sign_in_form():
    """The client-side half of the same behaviour, without the network."""
    from app.ais_client import SCHEDULE_LINK_RE
    from app.htmlutil import Page

    signed_out = Page(
        '<html><body><form id="sign_in_form" action="/en-ca/niv/users/sign_in" '
        'method="post"><input name="user[email]"></form></body></html>',
        base_url="https://ais.usvisa-info.com/en-ca/niv",
    )
    assert signed_out.form_by_id("sign_in_form") is not None
    assert not [l for l in signed_out.links if SCHEDULE_LINK_RE.search(l.href)]



# ---------------------------------------------------------------------------
# A network blip must not cost us the session
# ---------------------------------------------------------------------------

def _account():
    from app.config import Account
    return Account(name="A", email="a@example.com", password="p",
                   target=make_target(consulates=[94]))


def test_network_error_keeps_the_session(tmp_path, monkeypatch):
    """Regression: a read timeout on one page used to discard a healthy session,

    forcing a re-login next sweep -- the exact pattern that arms the captcha.
    """
    from app.ais_client import TransientNetworkError

    class Blippy(FakeClient):
        calls = 0

        def discover_schedules(self, only_ids=None):
            Blippy.calls += 1
            if Blippy.calls == 2:
                raise TransientNetworkError("ReadTimeout after 2 attempts")
            return super().discover_schedules(only_ids)

    FakeClient.logins = 0
    Blippy.calls = 0
    monkeypatch.setattr("app.worker.AisClient", Blippy)

    worker, store = build_worker(tmp_path)
    worker.config.accounts = [_account()]
    logged = []
    worker.log = logged.append
    worker._seed_state()

    worker._sweep()      # ok
    worker._sweep()      # network blip
    worker._sweep()      # must NOT have re-authenticated

    assert FakeClient.logins == 1, "session was discarded over a network blip"
    joined = "\n".join(logged)
    assert "keeping the session" in joined


def test_network_error_still_counts_towards_the_alert_budget(tmp_path, monkeypatch):
    """A sustained outage must eventually notify, even though we keep the session."""
    from app.ais_client import TransientNetworkError

    class Down(FakeClient):
        def discover_schedules(self, only_ids=None):
            raise TransientNetworkError("ConnectionError after 2 attempts")

    FakeClient.logins = 0
    monkeypatch.setattr("app.worker.AisClient", Down)

    worker, store = build_worker(tmp_path)
    worker.config.accounts = [_account()]
    worker.log = lambda m: None
    sent = []
    worker.notifier.account_error = lambda name, reason: sent.append(reason)
    worker._seed_state()

    for _ in range(worker.config.max_consecutive_failures):
        worker._sweep()

    assert FakeClient.logins == 1
    assert len(sent) == 1
    assert "network errors" in sent[0]
    assert "network error" in store.snapshot()["accounts"][0]["error"]


def test_get_page_retries_once_then_raises_transient(monkeypatch):
    """The retry is what absorbs the timeout before the worker ever sees it."""
    import requests

    from app.ais_client import AisClient, TransientNetworkError

    client = AisClient("a@example.com", "p", logger=lambda m: None)
    attempts = []

    def always_timeout(url, headers=None, timeout=None):
        attempts.append(url)
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(client.session, "get", always_timeout)
    monkeypatch.setattr("app.ais_client.time.sleep", lambda s: None)

    with pytest.raises(TransientNetworkError, match="after 2 attempts"):
        client._get_page("https://ais.usvisa-info.com/en-ca/niv")
    assert len(attempts) == 2


def test_get_page_succeeds_on_the_retry(monkeypatch):
    import requests

    from app.ais_client import AisClient

    client = AisClient("a@example.com", "p", logger=lambda m: None)
    calls = []

    class OK:
        status_code = 200
        text = "<html><body><a href='/x'>x</a></body></html>"
        url = "https://ais.usvisa-info.com/en-ca/niv"

        def raise_for_status(self):
            return None

    def flaky(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("reset by peer")
        return OK()

    monkeypatch.setattr(client.session, "get", flaky)
    monkeypatch.setattr("app.ais_client.time.sleep", lambda s: None)

    page = client._get_page("https://ais.usvisa-info.com/en-ca/niv")
    assert len(calls) == 2
    assert page.links[0].href.endswith("/x")


def test_http_errors_are_not_treated_as_transient(monkeypatch):
    """A 500 is a real problem; do not silently retry it as a blip."""
    import requests

    from app.ais_client import AisClient

    client = AisClient("a@example.com", "p", logger=lambda m: None)

    class ServerError:
        status_code = 500
        text = "boom"
        url = "https://ais.usvisa-info.com/en-ca/niv"

        def raise_for_status(self):
            raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(client.session, "get", lambda *a, **k: ServerError())
    with pytest.raises(requests.HTTPError):
        client._get_page("https://ais.usvisa-info.com/en-ca/niv")
