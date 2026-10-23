"""curl_cffi as the HTTP stack: the behaviours the rest of the code relies on."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import httpclient as http
from app.ais_client import AisClient
from app.config import NetworkConfig
from app.network import NetworkGuard


@pytest.fixture
def local_server():
    """A loopback HTTP server that records headers and sets a cookie."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 keep-alive: an HTTP/1.0 handler closes after each response,
        # which libcurl on Windows intermittently sees as a connection reset.
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - stdlib name
            seen.append(dict(self.headers))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "_yatri_session=abc; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()


def test_sessions_impersonate_chrome():
    assert http.Session().impersonate == http.IMPERSONATE


def test_exceptions_keep_the_requests_hierarchy():
    assert issubclass(http.Timeout, http.RequestException)
    assert issubclass(http.ConnectionError, http.RequestException)
    assert issubclass(http.HTTPError, http.RequestException)


def test_ais_session_sends_headers_and_keeps_cookies(local_server, tmp_path):
    url, seen = local_server
    client = AisClient("a@e.com", "x", user_agent="UA-TEST/1.0")
    resp = client.session.get(url + "/x", timeout=5)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert seen[-1]["User-Agent"] == "UA-TEST/1.0"
    assert client.session.cookies.get("_yatri_session") == "abc"

    path = tmp_path / "s.json"
    client.save_cookies(path)
    fresh = AisClient("a@e.com", "x")
    assert fresh.load_cookies(path) is True
    fresh.session.get(url + "/y", timeout=5)
    assert "_yatri_session=abc" in seen[-1].get("Cookie", "")


def test_changing_egress_leaves_the_session_usable(local_server):
    """curl_cffi's close() is final, so apply() must never call it."""
    url, _ = local_server
    session = http.Session()
    guard = NetworkGuard(NetworkConfig(proxy="socks5h://127.0.0.1:9"),
                         logger=lambda m: None)
    guard.apply(session)                  # -> proxied
    guard._mode = "direct"
    guard.apply(session)                  # -> back to direct
    assert session.proxies == {}
    assert session.get(url, timeout=5).status_code == 200

@pytest.mark.parametrize("status, replied", [
    (200, True), (302, True), (304, True), (401, True), (404, True), (422, True),
    (403, False), (407, False), (429, False), (500, False), (503, False),
])
def test_site_replied_separates_the_app_from_a_ban(status, replied):
    assert http.site_replied(status) is replied


def test_session_counts_site_replies(local_server):
    url, _ = local_server
    session = http.Session()
    assert session.site_replies == 0
    session.get(url + "/x", timeout=5)
    assert session.site_replies == 1



@pytest.fixture
def flaky_server():
    """Answers the first ``fails`` GETs with 403, then 200; records client ports."""
    state = {"fails": 0, "ports": [], "posts": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code):
            body = b"{}"
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            state["ports"].append(self.client_address[1])
            if state["fails"] > 0:
                state["fails"] -= 1
                self._reply(403)
            else:
                self._reply(200)

        def do_POST(self):  # noqa: N802
            state["posts"] += 1
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._reply(403)

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()


def test_refused_get_is_retried_on_a_fresh_connection(flaky_server):
    url, state = flaky_server
    state["fails"] = 2
    session = http.Session()
    try:
        resp = session.get(url + "/days.json", timeout=5)
    finally:
        session.close()
    assert resp.status_code == 200
    assert session.retries == 2 and session.site_replies == 1
    ports = state["ports"]
    assert len(ports) == 3 and len(set(ports)) == 3, "each retry must reconnect"


def test_three_refusals_return_the_last_reply(flaky_server):
    url, state = flaky_server
    state["fails"] = 10
    session = http.Session()
    try:
        resp = session.get(url + "/days.json", timeout=5)
    finally:
        session.close()
    assert resp.status_code == 403
    assert len(state["ports"]) == http.DEFAULT_ATTEMPTS == 3
    assert session.site_replies == 0


def test_post_is_never_retried(flaky_server):
    url, state = flaky_server
    session = http.Session()
    try:
        resp = session.post(url + "/appointment", data={"a": "1"}, timeout=5)
    finally:
        session.close()
    assert resp.status_code == 403 and state["posts"] == 1 and session.retries == 0


def test_connect_failure_is_retried_then_raised():
    session = http.Session()
    try:
        with pytest.raises(http.RequestException):
            session.get("http://127.0.0.1:9/", timeout=2)
    finally:
        session.close()
    assert session.retries == 2


def test_retry_keeps_the_login_cookie(local_server):
    url, seen = local_server
    session = http.Session()
    try:
        session.get(url + "/x", timeout=5)          # sets _yatri_session
        session.drop_connection()
        session.get(url + "/y", timeout=5)
    finally:
        session.close()
    assert "_yatri_session=abc" in seen[-1].get("Cookie", "")



# -- rate limit ------------------------------------------------------------

def test_rate_limiter_spaces_requests_across_threads():
    limiter = http.RateLimiter(rate=20)  # one slot every 50 ms
    stamps = []
    lock = threading.Lock()

    def worker():
        limiter.acquire()
        with lock:
            stamps.append(__import__("time").monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.04, gaps


def test_urgent_requests_skip_the_rate_limit():
    limiter = http.RateLimiter(rate=1)
    limiter.acquire()  # takes the current slot
    with http.urgent():
        assert limiter.acquire() == 0.0  # would otherwise wait ~1 s
    assert getattr(http._urgent, "depth", 0) == 0


def test_rate_limit_zero_is_off():
    limiter = http.RateLimiter(rate=0)
    assert all(limiter.acquire() == 0.0 for _ in range(50))


def test_retries_go_through_the_rate_limit(flaky_server, monkeypatch):
    url, state = flaky_server
    state["fails"] = 2
    calls = []
    monkeypatch.setattr(http.limiter, "acquire", lambda: calls.append(1) or 0.0)
    s = http.Session(attempts=3)
    try:
        assert s.get(url + "/days.json", timeout=5).status_code == 200
    finally:
        s.close()
    assert len(calls) == 3, "every attempt, retries included, takes a slot"
