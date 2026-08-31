"""Dashboard rendering and auth."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import (Config, DashboardConfig, TelegramConfig, load_config)
from app.consulates import parse_consulate_list
from app.dashboard import create_app
from app.store import ConsulateStatus, Store, mask_email


@pytest.fixture
def populated_store(tmp_path):
    """A store shaped like the real account: 2 watched + 1 completed application."""
    store = Store(path=tmp_path / "state.json")
    store.test_mode = True
    store.sweep_count = 3
    store.last_sweep_finished = datetime.now(timezone.utc)

    class T:
        consulates = parse_consulate_list("TRT,VAN", default_all=False)
        earliest = date(2026, 9, 1)
        latest = date(2026, 12, 30)

        def describe_window(self):
            return "2026-09-01 .. 2026-12-30"

    store.register_account("Primary", "someone@example.com")

    store.register_application("Primary", "72856817", "Applicant A", T())
    store.update_application("Primary", "72856817", kind="first_time",
                             action_label="Schedule Appointment")
    store.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=94, total_days=28, earliest=date(2028, 2, 17)))
    store.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=95, total_days=31, earliest=date(2027, 10, 7),
        earliest_acceptable=date(2027, 10, 7), acceptable_count=31))

    store.register_application("Primary", "72856546", "Applicant B", T())
    store.update_application("Primary", "72856546", kind="first_time")
    store.update_consulate("Primary", "72856546", ConsulateStatus(facility_id=94))
    store.update_consulate("Primary", "72856546", ConsulateStatus(
        facility_id=95, error="request failed"))

    store.register_application("Primary", "67078262", "Completed", T())
    store.update_application("Primary", "67078262", kind="done",
                             current_appointment="9 January, 2026")

    store.add_log("sweep #3 finished")
    return store


def build(store, auth=False):
    config = Config(
        accounts=[],
        telegram=TelegramConfig(),
        dashboard=DashboardConfig(
            username="admin" if auth else "", password="hunter2" if auth else ""
        ),
        test_mode=True,
    )
    return TestClient(create_app(config, store)), config


def test_index_renders_totals_and_targets(populated_store):
    client, _ = build(populated_store)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text

    # Counts the user asked to see.
    assert "Accounts" in body and "Applications" in body
    assert "3 application(s)" in body
    # Target consulates and target date window per application.
    assert "Toronto, Vancouver" in body
    assert "2026-09-01" in body and "2026-12-30" in body
    # Per-consulate availability.
    assert "2027-10-07" in body      # Vancouver match
    assert "2028-02-17" in body      # Toronto, outside window
    assert "request failed" in body
    assert "no availability" in body or "not polled yet" in body
    # Email must be masked, never shown in full.
    assert "someone@example.com" not in body
    assert mask_email("someone@example.com") in body
    # Application kinds.
    assert "never booked" in body
    assert "no action available" in body


def test_index_warns_when_unauthenticated(populated_store):
    client, _ = build(populated_store, auth=False)
    assert "no authentication" in client.get("/").text.lower()


def test_index_no_warning_when_authenticated(populated_store):
    client, _ = build(populated_store, auth=True)
    r = client.get("/", auth=("admin", "hunter2"))
    assert r.status_code == 200
    assert "no authentication" not in r.text.lower()


def test_auth_is_enforced(populated_store):
    client, _ = build(populated_store, auth=True)
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/", auth=("wrong", "hunter2")).status_code == 401
    assert client.get("/", auth=("admin", "hunter2")).status_code == 200


def test_api_state_shape(populated_store):
    client, _ = build(populated_store)
    data = client.get("/api/state").json()
    assert data["totals"] == {
        "accounts": 1, "applications": 3, "active": 2, "booked": 0, "matches": 1
    }
    acc = data["accounts"][0]
    assert acc["application_count"] == 3
    apps = {a["schedule_id"]: a for a in acc["applications"]}
    assert apps["72856817"]["state"] == "match"
    assert apps["72856817"]["target_consulates"] == ["Toronto", "Vancouver"]
    assert apps["72856817"]["target_latest"] == "2026-12-30"
    # One consulate failing does not condemn the whole application.
    assert apps["72856546"]["state"] == "watching"
    assert apps["67078262"]["state"] == "inactive"


def test_state_is_error_only_when_every_consulate_fails(populated_store):
    populated_store.update_consulate("Primary", "72856546", ConsulateStatus(
        facility_id=94, error="request failed"))
    client, _ = build(populated_store)
    apps = {
        a["schedule_id"]: a
        for a in client.get("/api/state").json()["accounts"][0]["applications"]
    }
    assert apps["72856546"]["state"] == "error"


def test_api_state_requires_auth(populated_store):
    client, _ = build(populated_store, auth=True)
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", auth=("admin", "hunter2")).status_code == 200


def test_consulate_preference_order_survives_to_the_api(tmp_path):
    """Order is meaningful (it breaks ties and drives strict mode), so it must

    not be sorted anywhere between config and the dashboard.
    """
    store = Store(path=tmp_path / "s.json")

    class T:
        consulates = parse_consulate_list("TRT,VAN,CAL", default_all=False)
        earliest = None
        latest = date(2028, 12, 31)

        def describe_window(self):
            return "before 2028-12-31"

    store.register_account("A", "a@example.com")
    store.register_application("A", "1", "X", T())
    client, _ = build(store)
    app = client.get("/api/state").json()["accounts"][0]["applications"][0]
    assert app["target_consulates"] == ["Toronto", "Vancouver", "Calgary"]


def example_config(tmp_path):
    """Load the shipped example with its blank secrets filled in.

    Note this also fills dashboard.password, so the resulting app has HTTP Basic
    auth enabled with admin/filled-in.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = src.read_text(encoding="utf-8").replace(
        'password: ""', 'password: "filled-in"'
    )
    dest = tmp_path / "config.yaml"
    dest.write_text(text, encoding="utf-8")
    return load_config(dest)


EXAMPLE_AUTH = ("admin", "filled-in")


def test_targets_are_visible_before_any_login(tmp_path):
    """The dashboard must show configured targets even when the login fails.

    Regression: applications used to be registered only during post-login
    discovery, so a bad password left the page empty -- precisely when you need
    to read it.
    """
    from app.notifier import TelegramNotifier
    from app.worker import Worker

    config = example_config(tmp_path)
    config.data_dir = tmp_path
    store = Store(path=config.store_file)
    worker = Worker(config, store, TelegramNotifier(config.telegram, logger=lambda m: None))

    # Seeding only reads config; no network involved.
    worker._seed_state()
    store.set_account_error(config.accounts[0].name, "login failed: sign in rejected")

    client = TestClient(create_app(config, store, worker))
    data = client.get("/api/state", auth=EXAMPLE_AUTH).json()
    assert data["totals"]["accounts"] == 1
    assert data["totals"]["applications"] == 2

    body = client.get("/", auth=EXAMPLE_AUTH).text
    assert "72856817" in body and "72856546" in body
    assert "Toronto" in body
    assert "login failed" in body
    # And the raw e-mail still must not appear.
    assert "you@example.com" not in body


def test_healthz_is_open_and_minimal(populated_store):
    client, _ = build(populated_store, auth=True)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Must not leak account data.
    assert set(body) == {"ok", "worker_running", "worker_status", "sweeps"}


def test_store_round_trip_restores_everything(tmp_path):
    """A restart must show the last known state, not an empty page."""
    path = tmp_path / "store.json"
    store = Store(path=path)

    class T:
        consulates = parse_consulate_list("TRT,VAN", default_all=False)
        earliest = None
        latest = date(2026, 12, 30)

        def describe_window(self):
            return "before 2026-12-30"

    store.sweep_count = 9
    store.register_account("A", "someone@example.com")
    store.register_application("A", "111", "X", T())
    store.update_application("A", "111", kind="first_time",
                             booked_for=date(2026, 12, 1),
                             booked_time="11:15", booked_consulate="Toronto")
    store.register_application("A", "222", "Y", T())
    store.update_consulate("A", "222", ConsulateStatus(
        facility_id=95, total_days=31, earliest=date(2027, 10, 7),
        earliest_acceptable=date(2027, 10, 7), acceptable_count=31))
    store.add_log("sweep #9 finished")
    assert store.save() is True

    reloaded = Store(path=path)
    assert reloaded.load() is True
    snap = reloaded.snapshot()

    assert snap["sweep_count"] == 9
    assert snap["totals"] == {
        "accounts": 1, "applications": 2, "active": 2, "booked": 1, "matches": 1
    }
    apps = {a["schedule_id"]: a for a in snap["accounts"][0]["applications"]}
    assert apps["111"]["booked_for"] == "2026-12-01"
    assert apps["111"]["booked_time"] == "11:15"
    assert apps["111"]["state"] == "booked"
    assert apps["222"]["consulates"][0]["earliest_acceptable"] == "2027-10-07"
    assert apps["222"]["state"] == "match"
    assert apps["111"]["target_consulates"] == ["Toronto", "Vancouver"]
    assert reloaded.booked_schedule_ids() == ["111"]
    assert any("sweep #9" in line for line in snap["log"])
    # The masked address survives; the raw one was never written.
    assert snap["accounts"][0]["email_masked"] == mask_email("someone@example.com")
    assert "someone@example.com" not in path.read_text(encoding="utf-8")


def test_store_ignores_missing_and_corrupt_files(tmp_path):
    assert Store(path=tmp_path / "nope.json").load() is False

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert Store(path=bad).load() is False

    wrong_version = tmp_path / "old.json"
    wrong_version.write_text('{"version": 999, "accounts": []}', encoding="utf-8")
    store = Store(path=wrong_version)
    assert store.load() is False
    assert store.snapshot()["totals"]["accounts"] == 0


def test_store_without_a_path_is_in_memory_only():
    store = Store(path=None)
    store.register_account("A", "a@example.com")
    assert store.save() is False
    assert store.load() is False
    assert store.snapshot()["totals"]["accounts"] == 1


def test_mask_email():
    assert mask_email("someone@example.com") == "s*****e@example.com"
    assert mask_email("ab@x.com") == "a***@x.com"
    assert mask_email("notanemail") == "***"


def test_example_config_drives_dashboard(tmp_path):
    """End-to-end: shipped example config -> app boots -> dashboard responds."""
    config = example_config(tmp_path)
    store = Store(path=tmp_path / "store.json")
    store.register_account(config.accounts[0].name, config.accounts[0].email)
    for app_cfg in config.accounts[0].applications:
        store.register_application(
            config.accounts[0].name, app_cfg.schedule_id, app_cfg.label, app_cfg.target
        )
    client = TestClient(create_app(config, store))
    r = client.get("/", auth=EXAMPLE_AUTH)
    assert r.status_code == 200
    assert "72856817" in r.text
    assert "Toronto" in r.text
