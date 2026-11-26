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

import contextlib
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlsplit

from curl_cffi import CurlInfo, CurlOpt
from curl_cffi import requests as _curl
from curl_cffi.requests import Cookies, Response  # noqa: F401 - re-exported
from curl_cffi.requests.exceptions import (  # noqa: F401 - re-exported
    ConnectionError,
    HTTPError,
    ProxyError,
    RequestException,
    Timeout,
)

from . import gateway as _gateway
from .gateway import Gateway  # noqa: F401 - re-exported

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


class RateLimiter:
    """Process-wide cap on AIS requests per second, shared by every account.

    Every account leaves through the same egress, so the site sees their
    combined rate: the cap is global, not per session. Each caller reserves
    the next free slot under a short lock and sleeps outside it, so parallel
    pollers queue up one ``1/rate`` apart instead of firing together. Retries
    take a slot too, which is what spaces them out.

    Requests made inside :func:`urgent` skip the queue: once a bookable day
    is seen, the confirm, times.json and booking requests race other people
    for the slot. ``rate <= 0`` turns the limiter off (the default, so tests
    and one-off scripts are not slowed down; ``main`` sets the configured rate).
    """

    def __init__(self, rate: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._next = 0.0
        self.rate = 0.0
        self.configure(rate)

    def configure(self, rate: float) -> None:
        self.rate = max(0.0, float(rate))

    def acquire(self) -> float:
        """Wait for this request's slot; returns the seconds waited."""
        if self.rate <= 0 or getattr(_urgent, "depth", 0) > 0:
            return 0.0
        interval = 1.0 / self.rate
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + interval
        wait = start - now
        if wait > 0:
            time.sleep(wait)
        return wait


_urgent = threading.local()
limiter = RateLimiter()


@contextlib.contextmanager
def urgent() -> Iterator[None]:
    """Requests on this thread inside the block bypass the rate limit."""
    _urgent.depth = getattr(_urgent, "depth", 0) + 1
    try:
        yield
    finally:
        _urgent.depth -= 1


# Attempts per idempotent request before it counts as one failure. With a
# rotating proxy pool every attempt runs on a fresh connection, i.e. (usually)
# a fresh exit IP, so a refused address is retried from another one at once.
DEFAULT_ATTEMPTS = 3

# Only these are retried: replaying a POST could submit a booking twice.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD"})

_ATTEMPT_INFOS = (
    CurlInfo.CONNECT_TIME_T,
    CurlInfo.PRETRANSFER_TIME_T,
    CurlInfo.POSTTRANSFER_TIME_T,
    CurlInfo.STARTTRANSFER_TIME_T,
    CurlInfo.TOTAL_TIME_T,
    CurlInfo.NUM_CONNECTS,
    CurlInfo.USED_PROXY,
)


@dataclass
class _AttemptTrace:
    lines: list[str] = field(default_factory=list)
    retry_time: float = 0.0


_attempt_trace: ContextVar[Optional[_AttemptTrace]] = ContextVar("http_attempt_trace", default=None)


@contextlib.contextmanager
def log_attempts(log: Callable[[str], None], label: str) -> Iterator[None]:
    """Log transport attempts after the request, without logging URLs or credentials."""
    trace = _AttemptTrace()
    token = _attempt_trace.set(trace)
    try:
        yield
    finally:
        _attempt_trace.reset(token)
        for line in trace.lines:
            log(f"{label} {line}")
        if trace.lines:
            log(
                f"{label} summary: attempts={len(trace.lines)} retries={len(trace.lines) - 1} "
                f"retry_time={trace.retry_time:.3f}s"
            )


def _attempt_details(response: Optional[Response], error: str, proxy_scheme: str) -> str:
    infos = getattr(response, "infos", {})
    used_proxy = infos.get(CurlInfo.USED_PROXY)
    if used_proxy:
        route = proxy_scheme or "proxy"
    elif used_proxy == 0 and (not error or getattr(response, "primary_ip", "")):
        route = "direct"
    else:
        route = "unknown"

    connected = infos.get(CurlInfo.CONNECT_TIME_T)
    ready = infos.get(CurlInfo.PRETRANSFER_TIME_T)
    sent = infos.get(CurlInfo.POSTTRANSFER_TIME_T)
    first_byte = infos.get(CurlInfo.STARTTRANSFER_TIME_T)
    total = infos.get(CurlInfo.TOTAL_TIME_T)
    new_connections = infos.get(CurlInfo.NUM_CONNECTS, "n/a")
    status = getattr(response, "status_code", 0)
    if error and not status:
        # libcurl can fill missing milestones with the end time on failure.
        first_byte = None
        if not getattr(response, "request_size", 0):
            ready = sent = None

    def seconds(micros: Optional[int]) -> str:
        return f"{micros / 1_000_000:.3f}s" if micros is not None else "n/a"

    # A request that was sent can still time out before its first byte, in
    # which case the wait ends at the transfer failure.
    body_time = total - first_byte if first_byte and total and total >= first_byte else None
    send_time = sent - ready if sent and ready and sent >= ready else None
    wait_end = first_byte or (total if error else None)
    wait_time = wait_end - sent if wait_end and sent and wait_end >= sent else None
    wait_label = "wait_first_byte" if first_byte or not error else "wait_until_error"
    setup_label = "setup"
    setup_time = ready
    if error and ready is None and total is not None:
        setup_label = "setup_until_error"
        setup_time = total
    connect_time = None if error and not connected else connected
    outcome = error or f"HTTP {status}"
    details = (
        f"route={route} outcome={outcome} connect={seconds(connect_time)} "
        f"{setup_label}={seconds(setup_time)} send={seconds(send_time)} "
        f"{wait_label}={seconds(wait_time)} "
        f"receive={seconds(body_time)} curl={seconds(total)} new_connections={new_connections}"
    )
    if route == "unknown" and proxy_scheme:
        details += f" configured_proxy={proxy_scheme}"
    redirects = getattr(response, "redirect_count", 0)
    if redirects:
        details += f" redirects={redirects}"
    return details


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
        kwargs.setdefault("curl_infos", list(_ATTEMPT_INFOS))
        super().__init__(**kwargs)
        self.site_replies = 0
        self.attempts = max(1, int(attempts))
        self.retries = 0  # retried attempts, for logs and tests
        # Relay for availability polls only (``poll=True`` requests, see
        # app/gateway.py). Sign-in, discovery and booking keep the session's
        # own route; a poll the relay cannot answer falls back to it.
        self.poll_gateway: Optional[_gateway.Gateway] = None
        self.gateway_fallbacks = 0
        self.last_gateway_failure = ""

    def _send(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return super().request(method, *args, **kwargs)

    def request(self, method: str, *args: Any, **kwargs: Any) -> Response:  # type: ignore[override]
        """Send a request; ``poll=True`` marks an availability poll.

        A poll goes through :attr:`poll_gateway` when one is set, except
        inside :func:`urgent`, which uses the session's own route. If the relay
        fails or returns a 403/429/5xx that is not the site's own reply, the
        poll is re-sent over the session's own route, so a broken gateway
        costs latency, never a missed calendar. ``gateway=`` forces a relay
        for one call with no fallback.
        """
        explicit = kwargs.pop("gateway", None)
        poll = kwargs.pop("poll", False)
        relay = explicit or (
            self.poll_gateway
            if poll and getattr(_urgent, "depth", 0) == 0 else None
        )
        if relay is None:
            return self._attempts(method, args, kwargs, None)
        try:
            resp = self._attempts(method, args, kwargs, relay)
        except RequestException as exc:
            if explicit is not None:
                raise
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if explicit is not None or site_replied(resp.status_code):
                return resp
            reason = f"HTTP {resp.status_code}"
        self.gateway_fallbacks += 1
        self.last_gateway_failure = reason
        return self._attempts(method, args, kwargs, None)

    def _attempts(self, method: str, args: tuple, kwargs: dict,
                  relay: Optional[_gateway.Gateway]) -> Response:
        trace = _attempt_trace.get()
        proxy_scheme = ""
        if trace is not None and relay is None:
            url = urlsplit(args[0] if args else kwargs["url"])
            proxies = kwargs.get("proxies")
            if proxies is None:
                proxies = self.proxies
            proxy = (kwargs.get("proxy") or proxies.get(f"{url.scheme}://{url.hostname}")
                     or proxies.get(f"all://{url.hostname}") or proxies.get(url.scheme)
                     or proxies.get("all") or "")
            scheme = proxy.partition("://")[0].lower()
            if scheme in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
                proxy_scheme = scheme
        if relay is not None:
            target = args[0] if args else kwargs["url"]
            relayed = {k: v for k, v in kwargs.items() if k not in ("proxies", "url")}
        tries = self.attempts if str(method).upper() in _RETRYABLE_METHODS else 1
        for attempt in range(1, tries + 1):
            started = time.perf_counter() if trace is not None else 0.0
            if attempt > 1:
                self.retries += 1
                self.drop_connection()
            limiter.acquire()
            curl_started = time.perf_counter() if trace is not None else 0.0
            resp = None
            error = ""
            try:
                if relay is not None:
                    resp = _gateway.request(
                        self._send, relay, self, method, target, **relayed
                    )
                else:
                    resp = super().request(method, *args, **kwargs)
            except RequestException as exc:
                resp = exc.response
                error = type(exc).__name__
                if attempt == tries:
                    raise
                continue
            finally:
                if trace is not None and (resp is not None or error):
                    elapsed = time.perf_counter() - started
                    if trace.lines:
                        trace.retry_time += elapsed
                    details = _attempt_details(resp, error, proxy_scheme)
                    trace.lines.append(
                        f"attempt {attempt}/{tries}: {details} "
                        f"before_curl={curl_started - started:.3f}s wall={elapsed:.3f}s"
                    )
            if site_replied(resp.status_code):
                if relay is None:  # a relayed reply says nothing about our route
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
