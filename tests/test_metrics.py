"""Prometheus exposition output."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Config, DashboardConfig, TelegramConfig
from app.consulates import parse_consulate_list
from app.dashboard import create_app
from app.metrics import render_metrics
from app.store import ConsulateStatus, Store

TODAY = date(2026, 8, 31)


class T:
    consulates = parse_consulate_list("TRT,VAN", default_all=False)
    earliest = date(2026, 9, 1)
    latest = date(2026, 12, 31)

    def describe_window(self):
        return "2026-09-01 .. 2026-12-31"


@pytest.fixture
def store():
    s = Store()
    s.test_mode = True
    s.worker_status = "running"
    s.sweep_count = 4
    s.last_sweep_finished = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    s.register_account("Primary", "someone@example.com")
    s.register_application("Primary", "72856817", "Chuyue Zhao", T())
    s.update_application("Primary", "72856817", kind="first_time")
    # Toronto: availability exists but lies outside the window.
    s.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=94, total_days=28, earliest=date(2028, 2, 17), acceptable_count=0))
    # Vancouver: a match.
    s.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=95, total_days=31, earliest=date(2027, 10, 7),
        earliest_acceptable=date(2027, 10, 7), acceptable_count=31))
    return s


def parse(text):
    """Exposition text -> {(name, frozenset(labels)): float}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        if "{" in head:
            name, _, rest = head.partition("{")
            labels = frozenset(
                (k, v.strip('"'))
                for part in rest.rstrip("}").split(",") if part
                for k, _, v in [part.partition("=")]
            )
        else:
            name, labels = head, frozenset()
        out[(name, labels)] = float(value)
    return out


def find(samples, name, **labels):
    wanted = set(labels.items())
    hits = [v for (n, lb), v in samples.items() if n == name and wanted <= set(lb)]
    assert len(hits) == 1, f"expected 1 sample for {name}{labels}, got {len(hits)}"
    return hits[0]


def test_earliest_slot_days_is_days_from_today(store):
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    # 2027-10-07 is 402 days after 2026-08-31
    assert find(s, "usvisa_earliest_slot_days", consulate="Vancouver") == 402
    # 2028-02-17 is 535 days after 2026-08-31
    assert find(s, "usvisa_earliest_slot_days", consulate="Toronto") == 535
    assert (date(2028, 2, 17) - TODAY).days == 535  # guard the arithmetic itself


def test_day_counts_decay_without_a_new_poll(store):
    """The value is computed at scrape time, so it drops by one each day."""
    snap = store.snapshot()
    a = parse(render_metrics(snap, today=TODAY))
    b = parse(render_metrics(snap, today=date(2026, 9, 30)))
    assert find(a, "usvisa_earliest_slot_days", consulate="Vancouver") == 402
    assert find(b, "usvisa_earliest_slot_days", consulate="Vancouver") == 402 - 30


def test_dimensions_are_account_application_consulate(store):
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    key = next(
        lb for (n, lb) in s if n == "usvisa_earliest_slot_days"
        and ("consulate", "Vancouver") in lb
    )
    labels = dict(key)
    assert labels["account"] == "Primary"
    assert labels["application"] == "Chuyue Zhao"
    assert labels["schedule_id"] == "72856817"
    assert labels["consulate"] == "Vancouver"


def test_acceptable_variant_only_emitted_when_in_window(store):
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    assert find(s, "usvisa_earliest_acceptable_slot_days", consulate="Vancouver") == 402
    # Toronto has slots but none acceptable, so the acceptable series is absent.
    assert not [
        1 for (n, lb) in s
        if n == "usvisa_earliest_acceptable_slot_days" and ("consulate", "Toronto") in lb
    ]


def test_no_availability_emits_no_day_series_rather_than_zero(store):
    """Zero would read as "a slot is free today", which is the opposite meaning."""
    store.update_consulate("Primary", "72856817", ConsulateStatus(facility_id=94))
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    assert not [
        1 for (n, lb) in s
        if n == "usvisa_earliest_slot_days" and ("consulate", "Toronto") in lb
    ]
    assert find(s, "usvisa_available_days", consulate="Toronto") == 0
    assert find(s, "usvisa_consulate_status", consulate="Toronto", status="none") == 1
    assert find(s, "usvisa_consulate_poll_ok", consulate="Toronto") == 1


def test_failed_poll_reports_error_and_no_measurements(store):
    store.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=94, error="request failed"))
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    assert find(s, "usvisa_consulate_poll_ok", consulate="Toronto") == 0
    assert find(s, "usvisa_consulate_status", consulate="Toronto", status="error") == 1
    for absent in ("usvisa_available_days", "usvisa_earliest_slot_days"):
        assert not [1 for (n, lb) in s if n == absent and ("consulate", "Toronto") in lb]


def test_target_deadline_days_can_go_negative(store):
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    # 2026-12-31 is 122 days after 2026-08-31
    assert find(s, "usvisa_target_deadline_days", schedule_id="72856817") == 122
    s2 = parse(render_metrics(store.snapshot(), today=date(2027, 3, 1)))
    assert find(s2, "usvisa_target_deadline_days", schedule_id="72856817") < 0


def test_state_carries_the_current_status_as_an_attribute(store):
    """One sample per application, not one per possible state."""
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    states = [
        dict(lb)["state"] for (n, lb) in s if n == "usvisa_application_state"
    ]
    assert states == ["match"]


def test_process_level_series(store):
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    assert s[("usvisa_up", frozenset())] == 1
    assert s[("usvisa_test_mode", frozenset())] == 1
    assert s[("usvisa_worker_running", frozenset())] == 1
    assert s[("usvisa_sweeps_total", frozenset())] == 4
    assert s[("usvisa_accounts_count", frozenset())] == 1
    assert find(s, "usvisa_account_login_ok", account="Primary") == 1


def test_account_error_flips_login_ok(store):
    store.set_account_error("Primary", "login failed")
    s = parse(render_metrics(store.snapshot(), today=TODAY))
    assert find(s, "usvisa_account_login_ok", account="Primary") == 0


def test_help_and_type_headers_are_emitted_once(store):
    text = render_metrics(store.snapshot(), today=TODAY)
    assert text.count("# TYPE usvisa_earliest_slot_days gauge") == 1
    assert text.count("# HELP usvisa_earliest_slot_days") == 1
    assert text.endswith("\n")


def test_label_values_are_escaped():
    s = Store()
    s.register_account('Acc "quoted" \\ odd', "a@example.com")
    s.register_application('Acc "quoted" \\ odd', "1", 'lab"el\\', T())
    text = render_metrics(s.snapshot(), today=TODAY)
    assert '\\"quoted\\"' in text
    assert "\\\\" in text
    # Still parseable: no stray unescaped quotes breaking the line count.
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert line.rpartition(" ")[2].replace("-", "").replace(".", "").isdigit()


def test_no_credentials_in_output():
    s = Store()
    s.register_account("Primary", "someone@example.com")
    text = render_metrics(s.snapshot(), today=TODAY)
    assert "someone@example.com" not in text


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def build(store, auth=False):
    config = Config(
        accounts=[],
        telegram=TelegramConfig(),
        dashboard=DashboardConfig(
            username="admin" if auth else "",
            password="hunter2" if auth else "",
        ),
    )
    return TestClient(create_app(config, store))


def test_metrics_endpoint_content_type_and_body(store):
    r = build(store).get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "usvisa_earliest_slot_days" in r.text


def test_metrics_follows_dashboard_auth(store):
    client = build(store, auth=True)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", auth=("admin", "hunter2")).status_code == 200
