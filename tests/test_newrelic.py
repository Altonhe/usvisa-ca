"""New Relic Metric API client. No network: requests.post is stubbed."""

import gzip
import json
from datetime import date

import pytest

from app.consulates import parse_consulate_list
from app.metrics import Sample, collect, to_newrelic
from app.newrelic import (ENDPOINTS, MAX_PAYLOAD_BYTES, NewRelicClient,
                          NewRelicError)
from app.store import ConsulateStatus, Store


class FakeResponse:
    def __init__(self, status_code=202, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def captured(monkeypatch):
    """Capture outgoing posts instead of sending them."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers or {}})
        return FakeResponse(202)

    monkeypatch.setattr("app.newrelic.requests.post", fake_post)
    return calls


def body_of(call):
    data = call["data"]
    if call["headers"].get("Content-Encoding") == "gzip":
        data = gzip.decompress(data)
    return json.loads(data)


class T:
    consulates = parse_consulate_list("TRT,VAN", default_all=False)
    earliest = date(2026, 9, 1)
    latest = date(2028, 12, 31)

    def describe_window(self):
        return "x"


def populated_store():
    s = Store()
    s.test_mode = True
    s.register_account("Primary", "someone@example.com")
    s.register_application("Primary", "72856817", "Chuyue Zhao", T())
    s.update_application("Primary", "72856817", kind="first_time")
    s.update_consulate("Primary", "72856817", ConsulateStatus(
        facility_id=94, total_days=27, earliest=date(2028, 2, 18),
        earliest_acceptable=date(2028, 2, 18), acceptable_count=27))
    return s


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_payload_matches_the_metric_api_contract():
    samples = [Sample("earliest_slot_days", 535, "help",
                      {"account": "Primary", "consulate": "Toronto"})]
    payload = to_newrelic(samples, {"service.name": "usvisa-watcher"}, 1700000000000)

    assert isinstance(payload, list) and len(payload) == 1
    block = payload[0]
    assert block["common"]["timestamp"] == 1700000000000
    assert block["common"]["attributes"] == {"service.name": "usvisa-watcher"}

    metric = block["metrics"][0]
    # Dotted name for New Relic, underscored for Prometheus.
    assert metric["name"] == "usvisa.earliest_slot_days"
    assert metric["type"] == "gauge"
    assert metric["value"] == 535
    assert metric["attributes"] == {"account": "Primary", "consulate": "Toronto"}


def test_headers_and_endpoint(captured):
    NewRelicClient("KEY123", region="us").send(
        [Sample("up", 1, "h", {})]
    )
    call = captured[0]
    assert call["url"] == ENDPOINTS["us"]
    assert call["headers"]["Api-Key"] == "KEY123"
    assert call["headers"]["Content-Type"] == "application/json"


@pytest.mark.parametrize("region", ["us", "eu", "jp"])
def test_regional_endpoints(region, captured):
    NewRelicClient("K", region=region).send([Sample("up", 1, "h", {})])
    assert captured[0]["url"] == ENDPOINTS[region]


def test_unknown_region_is_rejected():
    with pytest.raises(NewRelicError, match="region"):
        NewRelicClient("K", region="mars")


def test_real_store_produces_expected_series(captured):
    client = NewRelicClient("K", common_attributes={"environment": "prod"})
    samples = collect(populated_store().snapshot(), today=date(2026, 8, 31))
    assert client.send(samples) is True

    metrics = body_of(captured[0])[0]["metrics"]
    by_name = {m["name"]: m for m in metrics}
    assert by_name["usvisa.earliest_slot_days"]["value"] == 536  # 2028-02-18
    attrs = by_name["usvisa.earliest_slot_days"]["attributes"]
    assert attrs["account"] == "Primary"
    assert attrs["application"] == "Chuyue Zhao"
    assert attrs["consulate"] == "Toronto"
    assert attrs["schedule_id"] == "72856817"
    assert body_of(captured[0])[0]["common"]["attributes"]["environment"] == "prod"


def test_no_credentials_or_email_in_payload(captured):
    NewRelicClient("K").send(collect(populated_store().snapshot()))
    raw = json.dumps(body_of(captured[0]))
    assert "someone@example.com" not in raw
    assert "K" not in json.loads(raw)[0].get("common", {}).get("attributes", {}).values()


# ---------------------------------------------------------------------------
# Transport behaviour
# ---------------------------------------------------------------------------

def test_large_payloads_are_gzipped(captured):
    many = [
        Sample("earliest_slot_days", i, "h",
               {"account": f"acct{i}", "consulate": "Toronto", "pad": "x" * 40})
        for i in range(200)
    ]
    NewRelicClient("K").send(many)
    assert captured[0]["headers"].get("Content-Encoding") == "gzip"
    # Still valid JSON after decompression.
    assert body_of(captured[0])[0]["metrics"]


def test_oversized_payloads_are_split(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda url, data=None, headers=None, timeout=None: (
            calls.append(data) or FakeResponse(202)
        ),
    )
    # Force splitting with a tiny ceiling.
    monkeypatch.setattr("app.newrelic.MAX_PAYLOAD_BYTES", 1500)
    many = [
        Sample("earliest_slot_days", i, "h", {"account": f"acct{i}"})
        for i in range(60)
    ]
    assert NewRelicClient("K").send(many) is True
    assert len(calls) > 1


def test_empty_sample_list_sends_nothing(captured):
    assert NewRelicClient("K").send([]) is False
    assert captured == []


def test_disabled_client_sends_nothing(captured):
    client = NewRelicClient("")
    assert client.enabled is False
    assert client.send([Sample("up", 1, "h", {})]) is False
    assert captured == []


@pytest.mark.parametrize(
    "status,needle",
    [(403, "authentication failed"), (429, "rate limited"), (500, "HTTP 500")],
)
def test_error_statuses_are_reported_not_raised(monkeypatch, status, needle):
    logged = []
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: FakeResponse(status, "boom"),
    )
    client = NewRelicClient("K", logger=logged.append)
    assert client.send([Sample("up", 1, "h", {})]) is False
    assert any(needle in line for line in logged)


def test_network_failure_is_swallowed(monkeypatch):
    import requests as real_requests
    logged = []

    def boom(*a, **k):
        raise real_requests.ConnectionError("no route to host")

    monkeypatch.setattr("app.newrelic.requests.post", boom)
    client = NewRelicClient("K", logger=logged.append)
    assert client.send([Sample("up", 1, "h", {})]) is False
    assert any("request failed" in line for line in logged)


def test_repeated_failures_stop_flooding_the_log(monkeypatch):
    logged = []
    monkeypatch.setattr("app.newrelic.requests.post",
                        lambda *a, **k: FakeResponse(500, "err"))
    client = NewRelicClient("K", logger=logged.append)
    for _ in range(15):
        client.send([Sample("up", 1, "h", {})])
    # First three are logged, then it goes quiet until the 20th.
    assert len(logged) == 3


def test_verify_sends_a_probe(captured):
    logged = []
    client = NewRelicClient("K", logger=logged.append)
    assert client.verify() is True
    assert body_of(captured[0])[0]["metrics"][0]["name"] == "usvisa.up"
    assert any("new relic ready" in line for line in logged)


def test_verify_reports_when_unconfigured():
    logged = []
    assert NewRelicClient("", logger=logged.append).verify() is False
    assert any("not configured" in line for line in logged)


# ---------------------------------------------------------------------------
# Worker integration
# ---------------------------------------------------------------------------

def test_worker_pushes_after_a_sweep(captured):
    from app.config import Config, DashboardConfig, TelegramConfig
    from app.notifier import TelegramNotifier
    from app.worker import Worker

    config = Config(accounts=[], telegram=TelegramConfig(),
                    dashboard=DashboardConfig())
    store = populated_store()
    client = NewRelicClient("K")
    worker = Worker(config, store,
                    TelegramNotifier(config.telegram, logger=lambda m: None),
                    newrelic=client)
    worker._emit_metrics()
    assert captured, "worker did not push metrics"
    assert any(m["name"] == "usvisa.earliest_slot_days"
               for m in body_of(captured[0])[0]["metrics"])


def test_worker_survives_a_metrics_outage(monkeypatch):
    from app.config import Config, DashboardConfig, TelegramConfig
    from app.notifier import TelegramNotifier
    from app.worker import Worker

    monkeypatch.setattr("app.newrelic.requests.post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    config = Config(accounts=[], telegram=TelegramConfig(),
                    dashboard=DashboardConfig())
    logged = []
    worker = Worker(config, populated_store(),
                    TelegramNotifier(config.telegram, logger=lambda m: None),
                    newrelic=NewRelicClient("K", logger=lambda m: None))
    worker.log = logged.append
    worker._emit_metrics()          # must not raise
    assert any("metrics push failed" in line for line in logged)


def test_worker_without_newrelic_is_a_noop(captured):
    from app.config import Config, DashboardConfig, TelegramConfig
    from app.notifier import TelegramNotifier
    from app.worker import Worker

    config = Config(accounts=[], telegram=TelegramConfig(),
                    dashboard=DashboardConfig())
    worker = Worker(config, populated_store(),
                    TelegramNotifier(config.telegram, logger=lambda m: None))
    worker._emit_metrics()
    assert captured == []



# ---------------------------------------------------------------------------
# DNS blackhole diagnosis
# ---------------------------------------------------------------------------

def test_blackholed_dns_is_explained(monkeypatch):
    """An ad blocker resolving newrelic.com to 0.0.0.0 reads as "connection

    refused", which looks like a New Relic outage. Say what it really is.
    """
    import requests as real_requests

    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("[Errno 111] Connection refused")
        ),
    )
    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    logged = []
    client = NewRelicClient("K", logger=logged.append)
    assert client.send([Sample("up", 1, "h", {})]) is False
    message = "\n".join(logged)
    assert "0.0.0.0" in message
    assert "AdGuard" in message
    assert "allowlist" in message.lower()


def test_healthy_dns_adds_no_hint(monkeypatch):
    import requests as real_requests

    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("timed out")
        ),
    )
    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("162.247.241.10", 443))],
    )
    logged = []
    NewRelicClient("K", logger=logged.append).send([Sample("up", 1, "h", {})])
    assert "AdGuard" not in "\n".join(logged)


def test_unresolvable_host_is_explained(monkeypatch):
    import requests as real_requests

    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(real_requests.ConnectionError("nope")),
    )

    def boom(*a, **k):
        raise OSError("Name or service not known")

    monkeypatch.setattr("app.newrelic.socket.getaddrinfo", boom)
    logged = []
    NewRelicClient("K", logger=logged.append).send([Sample("up", 1, "h", {})])
    assert "does not resolve at all" in "\n".join(logged)



# ---------------------------------------------------------------------------
# DNS-over-HTTPS fallback
# ---------------------------------------------------------------------------

class FakePool:
    """Stands in for urllib3.HTTPSConnectionPool."""

    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakePool.created.append(kwargs)
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path,
                              "body": body, "headers": headers})
        FakePool.created[-1]["_request"] = self.requests[-1]
        return type("R", (), {"status": 202, "data": b"{}"})()


@pytest.fixture
def doh_env(monkeypatch):
    """System DNS blackholed, DoH answering, urllib3 stubbed."""
    FakePool.created = []
    import requests as real_requests

    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("[Errno 111] Connection refused")
        ),
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        assert params["name"] == "metric-api.newrelic.com"
        return type("R", (), {
            "json": lambda self=None: {
                "Answer": [{"type": 1, "data": "162.247.241.10"}]
            }
        })()

    monkeypatch.setattr("app.newrelic.requests.get", fake_get)
    monkeypatch.setattr("app.newrelic.urllib3.HTTPSConnectionPool", FakePool)
    return FakePool


def test_auto_mode_falls_back_to_doh_and_succeeds(doh_env):
    logged = []
    client = NewRelicClient("K", dns_mode="auto", logger=logged.append)
    assert client.send([Sample("up", 1, "h", {})]) is True
    assert client._pinned_ip == "162.247.241.10"
    joined = "\n".join(logged)
    assert "blackholed" in joined
    assert "over DoH" in joined


def test_pinned_request_keeps_hostname_for_tls_and_host_header(doh_env):
    NewRelicClient("K", dns_mode="auto").send([Sample("up", 1, "h", {})])
    pool = doh_env.created[-1]
    # Connect to the IP...
    assert pool["host"] == "162.247.241.10"
    # ...but verify TLS against the real hostname.
    assert pool["server_hostname"] == "metric-api.newrelic.com"
    assert pool["assert_hostname"] == "metric-api.newrelic.com"
    assert pool["cert_reqs"] == "CERT_REQUIRED"
    assert pool["ca_certs"]
    # And send the right Host header so the CDN routes it.
    assert pool["_request"]["headers"]["Host"] == "metric-api.newrelic.com"
    assert pool["_request"]["path"] == "/metric/v1"
    assert pool["_request"]["headers"]["Api-Key"] == "K"


def test_pin_is_reused_without_re_resolving(doh_env, monkeypatch):
    calls = []
    original = doh_env  # noqa: F841 - fixture already patched requests.get

    def counting_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return type("R", (), {
            "json": lambda self=None: {
                "Answer": [{"type": 1, "data": "162.247.241.10"}]
            }
        })()

    monkeypatch.setattr("app.newrelic.requests.get", counting_get)
    client = NewRelicClient("K", dns_mode="auto")
    client.send([Sample("up", 1, "h", {})])
    client.send([Sample("up", 1, "h", {})])
    client.send([Sample("up", 1, "h", {})])
    assert len(calls) == 1, "should resolve once and reuse the pin"


def test_expired_pin_is_refreshed(doh_env, monkeypatch):
    client = NewRelicClient("K", dns_mode="auto")
    client.send([Sample("up", 1, "h", {})])
    assert client._pin_is_fresh()
    monkeypatch.setattr("app.newrelic.DOH_CACHE_TTL_SECONDS", 0)
    assert client._pin_is_fresh() is False


def test_doh_mode_skips_system_dns(monkeypatch):
    FakePool.created = []
    resolved = []

    def should_not_be_called(*a, **k):
        raise AssertionError("system DNS must not be used in doh mode")

    monkeypatch.setattr("app.newrelic.requests.post", should_not_be_called)
    monkeypatch.setattr(
        "app.newrelic.requests.get",
        lambda url, params=None, headers=None, timeout=None: (
            resolved.append(url) or type("R", (), {
                "json": lambda self=None: {
                    "Answer": [{"type": 1, "data": "1.2.3.4"}]}
            })()
        ),
    )
    monkeypatch.setattr("app.newrelic.urllib3.HTTPSConnectionPool", FakePool)
    client = NewRelicClient("K", dns_mode="doh")
    assert client.send([Sample("up", 1, "h", {})]) is True
    assert resolved, "doh mode should resolve over HTTPS"
    assert FakePool.created[-1]["host"] == "1.2.3.4"


def test_system_mode_never_uses_doh(monkeypatch):
    import requests as real_requests
    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("refused")),
    )

    def should_not_be_called(*a, **k):
        raise AssertionError("system mode must not fall back to DoH")

    monkeypatch.setattr("app.newrelic.requests.get", should_not_be_called)
    client = NewRelicClient("K", dns_mode="system", logger=lambda m: None)
    assert client.send([Sample("up", 1, "h", {})]) is False


def test_doh_provider_failover(monkeypatch):
    FakePool.created = []
    import requests as real_requests
    attempts = []

    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("refused")),
    )

    def flaky_get(url, params=None, headers=None, timeout=None):
        attempts.append(url)
        if "cloudflare" in url:
            raise real_requests.ConnectionError("cloudflare blocked too")
        return type("R", (), {
            "json": lambda self=None: {
                "Answer": [{"type": 1, "data": "8.8.4.4"}]}
        })()

    monkeypatch.setattr("app.newrelic.requests.get", flaky_get)
    monkeypatch.setattr("app.newrelic.urllib3.HTTPSConnectionPool", FakePool)
    client = NewRelicClient("K", dns_mode="auto")
    assert client.send([Sample("up", 1, "h", {})]) is True
    assert len(attempts) == 2, "should try the second provider"
    assert client._pinned_ip == "8.8.4.4"


def test_all_doh_providers_blocked_reports_clearly(monkeypatch):
    import requests as real_requests
    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("refused")),
    )
    monkeypatch.setattr(
        "app.newrelic.requests.get",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("doh blocked")),
    )
    logged = []
    client = NewRelicClient("K", dns_mode="auto", logger=logged.append)
    assert client.send([Sample("up", 1, "h", {})]) is False
    joined = "\n".join(logged)
    assert "DNS-over-HTTPS resolution also failed" in joined
    assert "allowlist" in joined.lower()


def test_doh_answers_that_are_themselves_blackholed_are_ignored(monkeypatch):
    """A blocker that answers DoH must not poison the pin."""
    import requests as real_requests
    monkeypatch.setattr(
        "app.newrelic.socket.getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("0.0.0.0", 443))],
    )
    monkeypatch.setattr(
        "app.newrelic.requests.post",
        lambda *a, **k: (_ for _ in ()).throw(
            real_requests.ConnectionError("refused")),
    )
    monkeypatch.setattr(
        "app.newrelic.requests.get",
        lambda url, params=None, headers=None, timeout=None: type("R", (), {
            "json": lambda self=None: {"Answer": [{"type": 1, "data": "0.0.0.0"}]}
        })(),
    )
    client = NewRelicClient("K", dns_mode="auto", logger=lambda m: None)
    assert client.send([Sample("up", 1, "h", {})]) is False
    assert client._pinned_ip is None


def test_pinned_failure_clears_the_pin_so_it_re_resolves(doh_env, monkeypatch):
    client = NewRelicClient("K", dns_mode="auto", logger=lambda m: None)
    client.send([Sample("up", 1, "h", {})])
    assert client._pinned_ip

    class BrokenPool:
        def __init__(self, **kw):
            pass

        def request(self, *a, **k):
            raise OSError("stale address")

    monkeypatch.setattr("app.newrelic.urllib3.HTTPSConnectionPool", BrokenPool)
    assert client.send([Sample("up", 1, "h", {})]) is False
    assert client._pinned_ip is None


def test_invalid_dns_mode_is_rejected():
    with pytest.raises(NewRelicError, match="dns_mode"):
        NewRelicClient("K", dns_mode="carrier-pigeon")
