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
