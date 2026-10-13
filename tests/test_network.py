"""Account ban vs IP ban: classified, and the IP case rotates the egress.

An account that fails while others get through is the account's problem and
rests. When every account fails at once the egress is probed; an IP ban is
charged to no account and rotates the OpenVPN profile instead.
"""

from datetime import date

import pytest
import requests

from app.ais_client import AisError, Schedule
from app.config import (Account, Application, Config, ConfigError,
                        DashboardConfig, Target, TelegramConfig, load_config)
from app.network import BLOCKED, MAINTENANCE, OK, NetworkConfig, NetworkGuard
from app.notifier import TelegramNotifier
from app.store import Store
from app.worker import Worker

TRT = 94
SOCKS = "socks5h://openvpn-socks:1080"
PROVIDER = "http://openvpn-provider:8080"


# ---------------------------------------------------------------------------
# A fake HTTP layer standing in for both the site and the provider
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code = status
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


SIGN_IN = '<form id="sign_in_form"><input name="user[email]"></form>'


class FakeNet:
    """profiles: name -> True if the site accepts that egress."""

    def __init__(self, profiles=None, active="", direct_ok=False, site=None):
        self.profiles = dict(profiles or {})
        self.active = active
        self.direct_ok = direct_ok
        self.site = site                 # forced site response, or None
        self.switches = []
        self.probes = []

    def get(self, url, headers=None, proxies=None, timeout=None, allow_redirects=True):
        self.probes.append(dict(proxies or {}))
        if self.site is not None:
            if isinstance(self.site, Exception):
                raise self.site
            return self.site
        accepted = self.profiles.get(self.active, False) if proxies else self.direct_ok
        return Resp(200, SIGN_IN) if accepted else Resp(403, "Forbidden")

    def request(self, method, url, json=None, timeout=None, proxies=None):
        path = url.rsplit("8080", 1)[-1]
        if path == "/status":
            return Resp(200, body={"status": "connected", "activeProfile": self.active})
        if path == "/profiles":
            return Resp(200, body={"profiles": sorted(self.profiles)})
        if path == "/switch":
            self.active = json.get("profile") or sorted(self.profiles)[0]
            self.switches.append(self.active)
            return Resp(202, body={"status": "connecting", "activeProfile": self.active})
        return Resp(404, body={"error": "nope"})


def guard(net, **cfg):
    cfg.setdefault("openvpn_provider_url", PROVIDER)
    cfg.setdefault("proxy", SOCKS)
    return NetworkGuard(NetworkConfig(**cfg), logger=lambda m: None, session=net)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site, verdict", [
    (Resp(200, SIGN_IN), OK),
    (Resp(403, "Forbidden"), BLOCKED),
    (Resp(429, "Too many"), BLOCKED),
    (Resp(503, "<h1>Doing Maintenance...</h1> down for planned maintenance"), MAINTENANCE),
    (Resp(503, "Service Unavailable"), BLOCKED),
    (requests.ConnectionError("reset"), BLOCKED),
])
def test_probe_verdicts(site, verdict):
    assert guard(FakeNet(site=site)).probe().verdict == verdict


def test_probe_goes_through_the_proxy():
    net = FakeNet(profiles={"a": True}, active="a")
    guard(net).probe()
    assert net.probes[-1] == {"http": SOCKS, "https": SOCKS}


def test_apply_points_sessions_at_the_current_egress():
    g = guard(FakeNet())
    session = requests.Session()
    g.apply(session)
    assert session.proxies == {"http": SOCKS, "https": SOCKS}


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_recover_rotates_to_the_first_accepted_profile():
    net = FakeNet(profiles={"a": False, "b": False, "c": True}, active="a")
    g = guard(net)
    assert g.recover() is True
    assert net.switches == ["b", "c"], "walks on from the active profile"
    assert g.mode == "vpn" and not g.paused()


def test_recover_pauses_the_pool_when_nothing_works():
    net = FakeNet(profiles={"a": False, "b": False}, active="a")
    g = guard(net, pause_on_ip_ban=120)
    assert g.recover() is False
    assert g.paused() and 0 < g.pause_left() <= 120


def test_direct_fallback_is_opt_in():
    net = FakeNet(profiles={"a": False}, active="a", direct_ok=True)
    assert guard(net).recover() is False

    net = FakeNet(profiles={"a": False}, active="a", direct_ok=True)
    g = guard(net, direct_fallback=True)
    assert g.recover() is True
    assert g.mode == "direct" and g.proxies() == {}


def test_without_a_provider_an_ip_ban_pauses():
    g = guard(FakeNet(), openvpn_provider_url="", proxy="")
    assert g.recover() is False
    assert g.paused()


# ---------------------------------------------------------------------------
# Worker: who gets blamed
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, sid, fail=False):
        self.sid = sid
        self.fail = fail
        self.session = requests.Session()

    def get_available_days_meta(self, schedule_id, facility_id):
        return (None, None) if self.fail else ([], 'W/"x"')

    def get_available_days(self, schedule_id, facility_id):
        return self.get_available_days_meta(schedule_id, facility_id)[0]

    def discover_schedules(self, only_ids=None):
        return [Schedule(id=self.sid, action_label="Schedule Appointment",
                         continue_url=f"/en-ca/niv/schedule/{self.sid}/continue")]

    def current_appointment(self, schedule_id):
        return None


def target():
    return Target(consulates=[TRT], earliest=date(2026, 9, 1),
                  latest=date(2026, 12, 31), exclusions=[], prefer_earliest=True)


def account(name, sid):
    return Account(name=name, email=f"{name}@e.com", password="x", target=target(),
                   applications=[Application(schedule_id=sid, label=sid, target=target())])


def warm(w, clients):
    """One clean sweep: signs in and caches discovery, then turns failures on.

    A sweep that signs in or discovers has already heard from the site, which
    is itself proof the egress works -- so classification is only exercised
    from the next sweep on, as in steady state.
    """
    failing = {name: c.fail for name, c in clients.items()}
    for c in clients.values():
        c.fail = False
    w.store.sweep_count = 1
    w._sweep()
    for name, c in clients.items():
        c.fail = failing[name]


def build(tmp_path, clients, network=None, **cfg):
    cfg.setdefault("account_poll_delay", 0)
    accounts = [account(name, c.sid) for name, c in clients.items()]
    config = Config(accounts=accounts, telegram=TelegramConfig(),
                    dashboard=DashboardConfig(), test_mode=True,
                    data_dir=tmp_path, **cfg)
    store = Store(path=config.store_file)
    w = Worker(config, store, TelegramNotifier(config.telegram, logger=lambda m: None),
               network=network)
    w.log = lambda m: None
    w.notifier.account_error = lambda name, reason: None

    def client_for(acc):
        w._clients[acc.name] = clients[acc.name]
        return clients[acc.name]
    w._client_for = client_for
    w._seed_state()
    return w


class StubGuard:
    """Just enough NetworkGuard for the worker, with a scripted verdict."""

    def __init__(self, verdict, recovers=True):
        self.verdict = verdict
        self.recovers = recovers
        self.classified = 0
        self.recovered = 0
        self.last_probe = None
        self.mode = "vpn"
        self.config = NetworkConfig(pause_on_ip_ban=300)

    def paused(self):
        return False

    def apply(self, session):
        pass

    def classify_failed_sweep(self):
        self.classified += 1
        return self.verdict

    def recover(self, should_stop=None):
        self.recovered += 1
        return self.recovers

    def status(self):
        return {"enabled": True}


def test_one_account_failing_while_another_works_is_an_account_ban(tmp_path):
    g = StubGuard("ip")
    w = build(tmp_path, {"A": Client("1"), "B": Client("2", fail=True)}, network=g)
    w.store.sweep_count = 1
    w._sweep()
    assert g.classified == 0, "someone got through, so the egress is fine"
    assert w._failures["B"] == 1 and w._failures.get("A", 0) == 0


def test_everyone_failing_with_a_refused_ip_blames_no_account(tmp_path):
    g = StubGuard("ip")
    clients = {"A": Client("1", fail=True), "B": Client("2", fail=True)}
    w = build(tmp_path, clients, network=g, max_consecutive_failures=2)
    warm(w, clients)
    for n in range(2, 5):
        w.store.sweep_count = n
        w._sweep()
    assert g.classified == 3 and g.recovered == 3
    assert w._failures.get("A", 0) == 0 and w._failures.get("B", 0) == 0
    assert all(w._pool_available(a) for a in w.config.accounts), \
        "nobody is rested for an IP ban"


def test_everyone_failing_on_a_good_ip_is_still_charged_to_the_accounts(tmp_path):
    g = StubGuard("account")
    clients = {"A": Client("1", fail=True)}
    w = build(tmp_path, clients, network=g)
    warm(w, clients)
    w.store.sweep_count = 2
    w._sweep()
    assert g.classified == 1 and g.recovered == 0
    assert w._failures["A"] == 1


def test_maintenance_is_charged_to_nobody(tmp_path):
    g = StubGuard("maintenance")
    clients = {"A": Client("1", fail=True)}
    w = build(tmp_path, clients, network=g)
    warm(w, clients)
    w.store.sweep_count = 2
    w._sweep()
    assert g.recovered == 0
    assert w._failures.get("A", 0) == 0
    assert w.store.snapshot()["accounts"][0]["error"] == "site maintenance"


def test_login_failures_everywhere_on_a_blocked_ip_keep_the_sessions(tmp_path):
    class Refused(Client):
        def discover_schedules(self, only_ids=None):
            raise AisError("HTTP 403")
    g = StubGuard("ip")
    clients = {"A": Refused("1"), "B": Refused("2")}
    w = build(tmp_path, clients, network=g)
    w.config.discovery_interval = 0
    session_file = w._session_path("A")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}", encoding="utf-8")

    w.store.sweep_count = 1
    w._sweep()
    assert g.recovered == 1
    assert session_file.exists(), "an IP ban is no reason to throw a session away"


def test_paused_pool_skips_the_sweep(tmp_path):
    g = StubGuard("ip")
    g.paused = lambda: True
    g.pause_left = lambda: 42
    client = Client("1")
    client.get_available_days_meta = lambda *a: pytest.fail("polled while paused")
    w = build(tmp_path, {"A": client}, network=g)
    w.store.sweep_count = 1
    w._sweep()


def test_no_guard_keeps_the_old_behaviour(tmp_path):
    w = build(tmp_path, {"A": Client("1", fail=True)})
    w.store.sweep_count = 1
    w._sweep()
    assert w._failures["A"] == 1


# ---------------------------------------------------------------------------
# Config and dashboard
# ---------------------------------------------------------------------------

BASE = """
defaults: {consulates: [TRT]}
accounts:
  - {name: A, email: a@e.com, password: x}
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_network_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.network.proxy == "" and not cfg.network.provider_enabled
    assert cfg.network.ip_ban_detection and not cfg.network.direct_fallback


def test_network_parsed(tmp_path):
    cfg = load_config(write(tmp_path, BASE + f"""
network:
  openvpn_provider_url: {PROVIDER}
  proxy: {SOCKS}
  direct_fallback: true
  pause_on_ip_ban: 60
"""))
    assert cfg.network.provider_enabled and cfg.network.proxy == SOCKS
    assert cfg.network.direct_fallback and cfg.network.pause_on_ip_ban == 60


def test_provider_without_proxy_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="proxy"):
        load_config(write(tmp_path, BASE + f"network: {{openvpn_provider_url: {PROVIDER}}}\n"))


def test_bad_proxy_scheme_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="network.proxy"):
        load_config(write(tmp_path, BASE + "network: {proxy: ftp://x:1}\n"))


def test_dashboard_switch_needs_the_xhr_header(tmp_path):
    from fastapi.testclient import TestClient
    from app.dashboard import create_app

    net = FakeNet(profiles={"a": True, "b": True}, active="a")
    w = build(tmp_path, {"A": Client("1")}, network=guard(net))
    app = create_app(w.config, w.store, w)
    http = TestClient(app)

    assert http.post("/api/network/switch", json={"profile": "b"}).status_code == 403
    ok = http.post("/api/network/switch", json={"profile": "b"},
                   headers={"X-Requested-With": "usvisa-dashboard"})
    assert ok.status_code == 202 and net.active == "b"
    bad = http.post("/api/network/switch", json={"profile": "zzz"},
                    headers={"X-Requested-With": "usvisa-dashboard"})
    assert bad.status_code == 400

    page = http.get("/")
    assert page.status_code == 200
    assert "Network egress" in page.text and "Account pool" in page.text
