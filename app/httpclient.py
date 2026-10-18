"""The one HTTP stack every outbound call goes through: ``curl_cffi``.

``curl_cffi`` drives libcurl with a real browser's TLS and HTTP/2 fingerprint
(JA3/JA4, ALPN, header order), so AIS sees the handshake Chrome would make
instead of Python's. Its API mirrors ``requests``, which is why the rest of the
code imports this module as ``http`` and calls ``http.Session()``,
``http.post(...)`` and catches ``http.RequestException`` exactly as before.

Keeping the import in one place means the impersonation target is chosen once
and tests have a single seam to stub.

Differences from ``requests`` that callers must respect:

* ``Session.close()`` is final -- a closed session refuses further requests.
  Never close a session just to drop pooled connections; libcurl already keys
  connection reuse on the proxy, so changing ``session.proxies`` is enough.
* There is no ``requests.utils`` cookie-jar helper: use
  ``session.cookies.get_dict()`` and ``session.cookies.update(mapping)``.
* SOCKS (``socks5h://``) is native to libcurl; no PySocks needed.
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as _curl
from curl_cffi.requests import Response  # noqa: F401 - re-exported
from curl_cffi.requests.exceptions import (  # noqa: F401 - re-exported
    ConnectionError,
    HTTPError,
    ProxyError,
    RequestException,
    Timeout,
)

# "chrome" tracks the newest Chrome fingerprint the installed curl_cffi ships.
IMPERSONATE = "chrome"


def site_replied(status: int) -> bool:
    """Did the *application* answer, as opposed to a WAF, proxy or outage?

    401, 404, a redirect, a JSON error: the request crossed the egress and
    reached the site, so the address is not banned. 403/429 are what a ban
    looks like, 407 is the proxy, and 5xx says nothing about the address
    (maintenance is told apart by the network guard's own probe).
    """
    return status < 500 and status not in (403, 407, 429)


# Attempts per idempotent request before it counts as one failure. With a
# rotating proxy pool every attempt runs on a fresh connection, i.e. (usually)
# a fresh exit IP, so a refused address is retried from another one at once.
DEFAULT_ATTEMPTS = 3

# Only these are retried: replaying a POST could submit a booking twice.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD"})


class Session(_curl.Session):
    """A curl_cffi session that impersonates Chrome unless told otherwise.

    ``site_replies`` counts responses for which :func:`site_replied` holds.
    The worker reads it to learn that the egress works from requests it made
    anyway, so a sweep that failed for account reasons (an expired session,
    a locked account) needs no extra egress probe.

    GET/HEAD requests are tried up to ``attempts`` times. An attempt is
    retried when it raised (connect failure, timeout, proxy error) or got a
    reply that is not the application's (403/407/429/5xx). Each retry drops
    this thread's libcurl handle first, so it opens a new connection and a
    rotating proxy hands out a new exit IP instead of reusing the refused one
    over keep-alive. The caller sees only the last outcome: the final
    response, or the final exception.
    """

    def __init__(self, attempts: int = DEFAULT_ATTEMPTS, **kwargs: Any) -> None:
        kwargs.setdefault("impersonate", IMPERSONATE)
        super().__init__(**kwargs)
        self.site_replies = 0
        self.attempts = max(1, int(attempts))
        self.retries = 0  # retried attempts, for logs and tests

    def request(self, method: str, *args: Any, **kwargs: Any) -> Response:  # type: ignore[override]
        tries = self.attempts if str(method).upper() in _RETRYABLE_METHODS else 1
        for attempt in range(1, tries + 1):
            if attempt > 1:
                self.retries += 1
                self.drop_connection()
            try:
                resp = super().request(method, *args, **kwargs)
            except RequestException:
                if attempt == tries:
                    raise
                continue
            if site_replied(resp.status_code):
                self.site_replies += 1
                return resp
            if attempt == tries:
                return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def drop_connection(self) -> None:
        """Forget this thread's pooled connections; the next request reconnects.

        curl_cffi keeps one libcurl handle per thread, and the handle owns the
        connection cache. Closing it touches no other thread's requests, and
        cookies live on the session, not the handle, so the login survives.
        """
        local = getattr(self, "_local", None)
        curl = getattr(local, "curl", None) if local is not None else None
        if curl is not None:
            try:
                curl.close()
            finally:
                local.curl = None


def request(method: str, url: str, **kwargs: Any) -> Response:
    kwargs.setdefault("impersonate", IMPERSONATE)
    return _curl.request(method, url, **kwargs)


def get(url: str, **kwargs: Any) -> Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return request("POST", url, **kwargs)
