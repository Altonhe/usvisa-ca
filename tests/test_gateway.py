"""The HTTP relay gateway: requests, cookies and redirects go through it."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from app import httpclient as http
from app.config import ConfigError, load_config
from app.network import NetworkConfig, NetworkGuard

KEY = "test-key"


def _fake_site(target, method, headers, body):
    """What the relayed AIS would answer: (status, header pairs, body)."""
    path = urlsplit(target).path
    headers = {k.lower(): v for k, v in headers.items()}
    cookie = headers.get("cookie", "")
    if path == "/sign_in" and method == "POST":
        assert body == "user%5Bemail%5D=a%40e.com&commit=Sign+In"
        assert headers["content-type"] == "application/x-www-form-urlencoded"
        return 302, [("Location", "/home"), ("Set-Cookie", "_yatri_session=s1; Path=/")], ""
    if path == "/home":
        assert method == "GET", "a 302 after a POST is followed with GET"
        if "_yatri_session=s1" not in cookie:
            return 401, [], "no session"
        return 200, [("Content-Type", "text/html")], "welcome"
    if path == "/days.json":
        return 200, [("Content-Type", "application/json"), ("ETag", 'W/"e1"')], '[{"date": "2027-01-05"}]'
    if path == "/forbidden":
        return 403, [], "Forbidden"
    return 404, [], "nope"


@pytest.fixture
def gateway_server():
    """A gateway like the real Lambda: the target's reply passed through."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - stdlib name
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("X-Starlink-Key") != KEY:
                return self._error(401, "unauthorized", marked=True)
            req = json.loads(raw)
            calls.append(req)
            if req["target"].endswith("/gateway-broken"):
                # errorResponse() as it is today: no marker header.
                return self._error(502, "upstream request failed", marked=False)
            status, headers, body = _fake_site(
                req["target"], req["method"], req["headers"], req.get("body"))
            self._reply(status, [("Server", "nginx")] + headers, body.encode())

        def _error(self, status, message, marked):
            headers = [("content-type", "application/json"), ("x-amzn-RequestId", "r1")]
            if marked:
                headers.append(("X-Starlink-Error", "1"))
            self._reply(status, headers, json.dumps({"error": message}).encode())

        def _reply(self, status, headers, body):
            # send_response_only: no automatic Server header, so a reply only
            # carries what the gateway (or the passed-through site) set.
            self.send_response_only(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield http.Gateway(f"http://127.0.0.1:{server.server_port}/relay", KEY), calls
    server.shutdown()
    server.server_close()


SITE = "https://ais.usvisa-info.com"


class Relayed(http.Session):
    """Every request explicitly relayed, with no fallback: the relay mechanics."""

    def __init__(self, gw):
        super().__init__()
        self._gw = gw

    def request(self, method, *args, **kwargs):
        kwargs.setdefault("gateway", self._gw)
        return super().request(method, *args, **kwargs)


def session(gw):
    return Relayed(gw)


def test_sign_in_keeps_the_cookie_across_the_redirect(gateway_server):
    gw, calls = gateway_server
    s = session(gw)
    try:
        resp = s.post(SITE + "/sign_in", data={"user[email]": "a@e.com", "commit": "Sign In"},
                      timeout=5)
        assert resp.status_code == 200 and resp.text == "welcome"
        assert resp.url == SITE + "/home"
        assert [c["method"] for c in calls] == ["POST", "GET"]
        assert s.cookies.get("_yatri_session") == "s1"
        # The next request carries the cookie to the site through the gateway.
        assert s.get(SITE + "/home", timeout=5).status_code == 200
        assert "_yatri_session=s1" in calls[-1]["headers"]["Cookie"]
    finally:
        s.close()


def test_json_headers_and_params_are_relayed(gateway_server):
    gw, calls = gateway_server
    s = session(gw)
    s.headers["User-Agent"] = "UA-TEST/1.0"
    try:
        resp = s.get(SITE + "/days.json", params={"x": "1"},
                     headers={"X-Requested-With": "XMLHttpRequest"}, timeout=5)
        assert resp.json() == [{"date": "2027-01-05"}]
        assert resp.headers.get("ETag") == 'W/"e1"'
        assert calls[-1]["target"] == SITE + "/days.json?x=1"
        assert calls[-1]["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert calls[-1]["headers"]["User-Agent"] == "UA-TEST/1.0", "session defaults are relayed"
        assert s.site_replies == 0, "a relayed reply proves nothing about our own route"
    finally:
        s.close()


def test_head_is_sent_as_get(gateway_server):
    # The gateway only speaks GET and POST; the probe's HEAD must not reach it.
    gw, calls = gateway_server
    s = session(gw)
    try:
        resp = s.head(SITE + "/days.json", timeout=5)
        assert resp.status_code == 200 and resp.content == b""
        assert calls[-1]["method"] == "GET" and "body" not in calls[-1]
    finally:
        s.close()


def test_a_refusing_site_is_retried_like_any_other(gateway_server):
    gw, calls = gateway_server
    s = session(gw)
    try:
        resp = s.get(SITE + "/forbidden", timeout=5)
        assert resp.status_code == 403
        assert len(calls) == http.DEFAULT_ATTEMPTS and s.site_replies == 0
    finally:
        s.close()


def test_gateway_errors_are_egress_failures_not_site_replies(gateway_server):
    gw, calls = gateway_server
    s = session(gw)
    s.attempts = 1
    try:
        with pytest.raises(http.ProxyError, match="upstream request failed"):
            s.get(SITE + "/gateway-broken", timeout=5)
        bad = session(http.Gateway(gw.url, "wrong"))
        bad.attempts = 1
        try:
            # A 401 alone would read as AIS's "session expired"; the marker
            # header is what tells the gateway's own 401 apart.
            with pytest.raises(http.ProxyError, match="unauthorized"):
                bad.get(SITE + "/home", timeout=5)
        finally:
            bad.close()
        assert s.site_replies == 0
    finally:
        s.close()


def test_the_sites_own_json_error_passes_through(gateway_server):
    gw, _ = gateway_server
    s = session(gw)
    try:
        assert s.get(SITE + "/home", timeout=5).status_code == 401
    finally:
        s.close()


def test_unreachable_gateway_raises_a_connection_error():
    s = session(http.Gateway("http://127.0.0.1:9/relay", KEY))
    s.attempts = 1
    try:
        with pytest.raises(http.RequestException):
            s.get(SITE + "/home", timeout=2)
    finally:
        s.close()


def test_the_key_is_never_in_the_repr():
    assert KEY not in repr(http.Gateway("http://g", KEY))
    assert KEY not in repr(NetworkConfig(gateway_url="http://g", gateway_key=KEY))


# -- network guard -----------------------------------------------------------

def test_guard_gives_every_session_the_poll_relay():
    g = NetworkGuard(NetworkConfig(gateway_url="http://g", gateway_key=KEY),
                     logger=lambda m: None)
    s = http.Session()
    try:
        g.apply(s)
        assert s.poll_gateway == http.Gateway("http://g", KEY)
        assert g.mode == "direct" and s.proxies == {}, "the gateway is not the main route"
    finally:
        s.close()


# -- polls only ----------------------------------------------------------------

class Recording(http.Session):
    """Records which route each attempt took; the direct route answers 200."""

    def __init__(self, relay_reply=None):
        super().__init__(attempts=1)
        self.routes = []
        self.relay_reply = relay_reply

    def _attempts(self, method, args, kwargs, relay):
        self.routes.append("relay" if relay else "direct")
        if relay is None:
            return type("R", (), {"status_code": 200})()
        if isinstance(self.relay_reply, Exception):
            raise self.relay_reply
        return self.relay_reply


def test_only_polls_use_the_relay():
    s = Recording(relay_reply=type("R", (), {"status_code": 200})())
    s.poll_gateway = http.Gateway("http://g", KEY)
    try:
        s.get(SITE + "/sign_in")
        s.get(SITE + "/days.json", poll=True)
        with http.urgent():
            s.get(SITE + "/days.json", poll=True)   # confirming a hit: direct
        assert s.routes == ["direct", "relay", "direct"]
    finally:
        s.close()


@pytest.mark.parametrize("reply", [
    http.ProxyError("gateway HTTP 502: Regional proxy invocation failed"),
    type("R", (), {"status_code": 403})(),
])
def test_a_poll_the_relay_cannot_answer_goes_direct(reply):
    s = Recording(relay_reply=reply)
    s.poll_gateway = http.Gateway("http://g", KEY)
    try:
        assert s.get(SITE + "/days.json", poll=True).status_code == 200
        assert s.routes == ["relay", "direct"]
        assert s.gateway_fallbacks == 1 and s.last_gateway_failure
    finally:
        s.close()


def test_an_expired_session_via_the_relay_is_not_retried_direct():
    s = Recording(relay_reply=type("R", (), {"status_code": 401})())
    s.poll_gateway = http.Gateway("http://g", KEY)
    try:
        assert s.get(SITE + "/days.json", poll=True).status_code == 401
        assert s.routes == ["relay"], "401 is AIS itself: sign in again, do not re-poll"
    finally:
        s.close()

# -- config ------------------------------------------------------------------

BASE = """
test_mode: true
accounts:
  - name: A
    email: a@e.com
    password: x
    applications:
      - schedule_id: "1"
        consulates: TRT
        earliest_acceptable_date: 2026-01-01
        latest_acceptable_date: 2027-12-31
"""


def write(tmp_path, network):
    path = tmp_path / "config.yaml"
    path.write_text(BASE + network, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("AIS_PROXY", "OPENVPN_PROVIDER_URL",
                 "STARLINK_GATEWAY_URL", "STARLINK_GATEWAY_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_config_reads_the_gateway_from_the_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("STARLINK_GATEWAY_URL", "https://g.example/relay")
    monkeypatch.setenv("STARLINK_GATEWAY_KEY", "k")
    cfg = load_config(write(tmp_path, "network: {use_gateway: true}\n"))
    assert cfg.network.gateway == http.Gateway("https://g.example/relay", "k")
    off = load_config(write(tmp_path, ""))
    assert off.network.gateway is None, "the switch, not the secret, turns it on"


@pytest.mark.parametrize("network, env, match", [
    ("network: {use_gateway: true}\n", {"STARLINK_GATEWAY_URL": "https://g"},
     "STARLINK_GATEWAY_KEY is not set"),
    ("network: {use_gateway: true}\n",
     {"STARLINK_GATEWAY_URL": "g.example", "STARLINK_GATEWAY_KEY": "k"}, "must start with"),
    ("network: {gateway_key: k}\n", {}, "store it in Infisical as STARLINK_GATEWAY_KEY"),
])
def test_config_rejects_bad_gateway_settings(tmp_path, monkeypatch, network, env, match):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, network))


# -- other service secrets ----------------------------------------------------

def test_capsolver_and_newrelic_are_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", "CAP-1")
    monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "NR-1")
    cfg = load_config(write(tmp_path, "capsolver: {enabled: true}\n"
                                      "newrelic: {enabled: true, region: eu}\n"))
    assert cfg.capsolver_api_key == "CAP-1" and cfg.capsolver_enabled
    assert cfg.newrelic.license_key == "NR-1" and cfg.newrelic.enabled
    off = load_config(write(tmp_path, ""))
    assert not off.capsolver_enabled and not off.newrelic.enabled


@pytest.mark.parametrize("text, secret", [
    ("capsolver_api_key: CAP-1\n", "CAPSOLVER_API_KEY"),
    ("newrelic: {license_key: NR-1}\n", "NEW_RELIC_LICENSE_KEY"),
])
def test_inline_service_secrets_are_refused(tmp_path, text, secret):
    with pytest.raises(ConfigError, match=f"store it in Infisical as {secret}"):
        load_config(write(tmp_path, text))


def test_enabled_without_the_secret_names_it(tmp_path):
    with pytest.raises(ConfigError, match="capsolver.enabled is on but CAPSOLVER_API_KEY"):
        load_config(write(tmp_path, "capsolver: {enabled: true}\n"))


def test_one_infisical_fetch_serves_accounts_and_services(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(self):
        calls.append(1)
        return {"B_EMAIL": "b@e.com", "B_PASSWORD": "pw",
                "AIS_PROXY": "socks5h://vault:1080", "CAPSOLVER_API_KEY": "CAP-V"}

    monkeypatch.setattr("app.secrets.InfisicalClient.fetch_all", fake_fetch)
    monkeypatch.setenv("AIS_PROXY", "socks5h://env:1080")
    cfg = load_config(write(tmp_path, """
secrets: {provider: infisical, project_id: p}
capsolver: {enabled: true}
network: {use_proxy: true}
""" + BASE.replace("test_mode: true\naccounts:", "accounts:\n  - name: B\n    canary: true\n"
                     "    applications: [{schedule_id: '2'}]", 1)))
    assert cfg.network.proxy == "socks5h://vault:1080", "Infisical wins over the environment"
    assert cfg.capsolver_api_key == "CAP-V"
    assert [a.email for a in cfg.accounts if a.name == "B"] == ["b@e.com"]
    assert calls == [1]


# -- the starlink regional proxy's own replies ----------------------------------

from curl_cffi.requests import Headers  # noqa: E402

from app.gateway import _gateway_error  # noqa: E402


class R:
    def __init__(self, status, body, headers=(("content-type", "application/json"),)):
        self.status_code = status
        self.content = body.encode()
        self.headers = Headers(list(headers))


@pytest.mark.parametrize("reply, expected", [
    # front gateway errorResponse
    (R(502, '{"error":"Regional proxy invocation failed"}'), "Regional proxy invocation failed"),
    (R(504, '{"error":"Regional proxy invocation timed out"}'), "Regional proxy invocation timed out"),
    # regional proxyErrorResponse: the target could not be fetched
    (R(500, '{"error":"Proxy request failed","target":"https://x","timestamp":1,'
            '"details":"dial tcp: i/o timeout"}'), "Proxy request failed: dial tcp: i/o timeout"),
    (R(500, '{"error":"Proxy request failed","target":"https://x","timestamp":1}'),
     "Proxy request failed"),
    # regional textResponse
    (R(400, "Invalid target URL", (("content-type", "text/plain; charset=utf-8"),)),
     "Invalid target URL"),
    # AIS's own 401 XHR answer: a site reply, not a gateway failure
    (R(401, '{"error":"You need to sign in or sign up before continuing."}'), None),
    # a passed-through AIS reply carries the site's own headers
    (R(500, '{"error":"x","target":"y"}', (("content-type", "application/json"),
                                           ("server", "nginx"))), None),
])
def test_regional_proxy_errors_are_recognised(reply, expected):
    assert _gateway_error(reply) == expected
