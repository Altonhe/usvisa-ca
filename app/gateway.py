"""Send AIS requests through an HTTP relay gateway instead of a proxy.

The gateway takes a JSON description of the request and makes it itself::

    POST <gateway url>
    X-Starlink-Key: <key>
    content-type: application/json

    {"target": "https://ais.usvisa-info.com/...", "method": "GET",
     "headers": {...}, "body": "..."}

Because the gateway, not libcurl, talks to AIS, three things a proxy gets for
free are done here:

* **Cookies.** The gateway does not keep a cookie jar, so the session's AIS
  cookies are sent as a ``Cookie`` header in ``headers`` and every
  ``Set-Cookie`` in the reply is written back into the session's jar under the
  target's host. Sign-in and the saved-session logic work unchanged.
* **Redirects** are followed here, one gateway call per hop, so a
  ``Set-Cookie`` on a 302 (the sign-in POST) is not lost.
* **The reply** is the target's response passed through unchanged (status,
  headers, body). Replies the gateway makes itself -- ``{"error": "..."}`` --
  raise :class:`GatewayError`, so a broken gateway counts as the egress
  failing, not as the site answering.

AIS sees the gateway's TLS fingerprint, not curl_cffi's Chrome one: the
impersonation only covers the hop to the gateway.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlsplit

from curl_cffi.requests import Headers
from curl_cffi.requests.exceptions import HTTPError, ProxyError, RequestException

KEY_HEADER = "X-Starlink-Key"
MAX_REDIRECTS = 10
_REDIRECTS = (301, 302, 303, 307, 308)
# Describe the transport, which the gateway has already undone.
_DROP_REPLY_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


@dataclass(frozen=True)
class Gateway:
    url: str
    key: str

    def __repr__(self) -> str:  # never print the key
        return f"Gateway({self.url!r})"


class GatewayResponse:
    """The target's reply, shaped like the ``Response`` callers already use."""

    def __init__(self, status: int, headers: Headers, content: bytes, url: str) -> None:
        self.status_code = status
        self.headers = headers
        self.content = content
        self.url = url
        self.history: List["GatewayResponse"] = []

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self, **kwargs: Any) -> Any:
        return _json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code} for {self.url}")


Send = Callable[..., Any]


def request(
    send: Send,
    gateway: Gateway,
    session: Any,
    method: str,
    url: str,
    *,
    params: Any = None,
    data: Any = None,
    json: Any = None,
    content: Any = None,
    headers: Any = None,
    timeout: Any = None,
    allow_redirects: Optional[bool] = None,
    **_ignored: Any,
) -> GatewayResponse:
    """Make one request through the gateway, following redirects here.

    ``send`` performs the raw HTTP call to the gateway (the session's own
    curl request, so it keeps the TLS fingerprint and timeouts). ``session``
    supplies the default headers and the cookie jar.
    """
    method = str(method).upper()
    # The gateway speaks GET and POST only; the egress probe's HEAD becomes a
    # GET whose body is discarded, so callers still see an empty HEAD reply.
    head = method == "HEAD"
    if head:
        method = "GET"
    target = _with_params(url, params)
    merged = Headers(session.headers)
    if headers:
        merged.update(headers)
    body = _body(data, json, content, merged)
    follow = True if allow_redirects is None else bool(allow_redirects)
    history: List[GatewayResponse] = []

    for _ in range(MAX_REDIRECTS + 1):
        sent = {k: v for k, v in merged.multi_items() if k.lower() != "cookie"}
        cookie = _cookie_header(session.cookies, target)
        if cookie:
            sent["Cookie"] = cookie
        payload: Dict[str, Any] = {"target": target, "method": method, "headers": sent}
        if body is not None:
            payload["body"] = body
        kwargs: Dict[str, Any] = dict(
            json=payload,
            headers={KEY_HEADER: gateway.key, "content-type": "application/json"},
            allow_redirects=False,
            discard_cookies=True,  # the gateway's own cookies are not AIS's
            # Straight to the gateway, never through the session's proxy: the
            # relay exists to take polling load (and bytes) off that proxy,
            # and must keep working when the proxy is down.
            proxies={},
        )
        if timeout is not None:
            kwargs["timeout"] = timeout
        status, pairs, raw = _unwrap(send("POST", gateway.url, **kwargs))
        _store_cookies(session.cookies, target, pairs)
        resp = GatewayResponse(
            status,
            Headers([(k, v) for k, v in pairs if k.lower() not in _DROP_REPLY_HEADERS]),
            raw,
            target,
        )
        location = resp.headers.get("location")
        if not (follow and status in _REDIRECTS and location):
            if head:
                resp.content = b""
            resp.history = history
            return resp
        history.append(resp)
        target = urljoin(target, location)
        if status in (301, 302, 303) and method not in ("GET", "HEAD"):
            method, body = "GET", None
            for name in ("content-type", "content-length"):
                if name in merged:
                    del merged[name]
    raise RequestException(f"gateway: more than {MAX_REDIRECTS} redirects from {url}")


# -- request building --------------------------------------------------------

def _with_params(url: str, params: Any) -> str:
    if not params:
        return url
    query = params if isinstance(params, str) else urlencode(params, doseq=True)
    return url + ("&" if "?" in url else "?") + query


def _body(data: Any, json_: Any, content: Any, headers: Headers) -> Optional[str]:
    if json_ is not None:
        if "content-type" not in headers:
            headers["content-type"] = "application/json"
        return _json.dumps(json_)
    raw = data if data is not None else content
    if raw is None:
        return None
    if isinstance(raw, (dict, list, tuple)):
        if "content-type" not in headers:
            headers["content-type"] = "application/x-www-form-urlencoded"
        return urlencode(raw, doseq=True)
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


# -- reply parsing -----------------------------------------------------------

# Header the gateway puts on replies it generates itself (bad key, bad
# request, the target unreachable), so they are never mistaken for the site's.
ERROR_HEADER = "X-Starlink-Error"
# Headers AWS Lambda function URLs add to every reply; a reply carrying
# nothing else did not come from the target.
_PLATFORM_HEADERS = {"content-type", "content-length", "date", "connection", "via"}


class GatewayError(ProxyError):
    """The gateway itself failed; the request never got a reply from the site."""


def _gateway_error(resp: Any) -> Optional[str]:
    """The gateway's own error message, or None for a passed-through reply.

    Marked replies are recognised by :data:`ERROR_HEADER`. Without the marker
    the shapes the starlink code produces are matched, but only on a reply
    that carries nothing beyond platform headers (a passed-through AIS reply
    always has its own): the front gateway's ``errorResponse``
    ``{"error"}``, the regional proxy's ``proxyErrorResponse``
    ``{"error","target","timestamp"[,"details"]}`` (always HTTP 500) and its
    plain-text 400/405 ``textResponse``. A bare 401 ``{"error"}`` is left
    alone: it is exactly what AIS answers an XHR without a session.
    """
    marked = resp.headers.get(ERROR_HEADER)
    try:
        data = _json.loads(resp.content or b"")
    except ValueError:
        data = None
    message = data.get("error") if isinstance(data, dict) else None
    if marked is not None:
        return str(message or marked or f"HTTP {resp.status_code}")
    names = {k.lower() for k, _ in resp.headers.multi_items()}
    if any(n not in _PLATFORM_HEADERS and not n.startswith("x-amzn-") for n in names):
        return None
    if isinstance(data, dict) and "error" in data:
        keys = set(data)
        if keys == {"error"} and resp.status_code != 401:
            return str(message)
        if keys >= {"error", "target"} and keys <= _REGIONAL_ERROR_KEYS:
            detail = data.get("details")
            return f"{message}: {detail}" if detail else str(message)
        return None
    text = (resp.content or b"").decode("utf-8", errors="replace").strip()
    if resp.status_code in (400, 405) and text in _REGIONAL_TEXT_ERRORS:
        return text
    return None


# Bodies the regional proxy (starlink aws/lambda) generates itself.
_REGIONAL_ERROR_KEYS = {"error", "target", "timestamp", "details"}
_REGIONAL_TEXT_ERRORS = {
    "Only GET and POST methods are allowed",
    "Invalid target URL",
    "Only HTTP/HTTPS protocols are allowed",
    "Target domain not allowed",
}


def _unwrap(resp: Any) -> Tuple[int, List[Tuple[str, str]], bytes]:
    """``(status, header pairs, body)`` of the target's reply.

    The gateway passes the target's response through unchanged; its own
    failures raise :class:`GatewayError`, so they count as the egress failing
    (retried, then probed, then the direct fallback) rather than as the site.
    """
    error = _gateway_error(resp)
    if error is not None:
        raise GatewayError(f"gateway HTTP {resp.status_code}: {error}")
    return resp.status_code, list(resp.headers.multi_items()), resp.content or b""

# -- cookies -----------------------------------------------------------------

def _domain_matches(host: str, domain: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    return not domain or host == domain or host.endswith("." + domain)


def _cookie_header(cookies: Any, url: str) -> str:
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    seen: Dict[str, str] = {}
    for c in getattr(cookies, "jar", []):
        if c.is_expired():
            continue
        if not _domain_matches(host, c.domain) or not path.startswith(c.path or "/"):
            continue
        seen[c.name] = c.value or ""
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def _store_cookies(cookies: Any, url: str, pairs: List[Tuple[str, str]]) -> None:
    host = (urlsplit(url).hostname or "").lower()
    for name, value in pairs:
        if name.lower() != "set-cookie":
            continue
        jar = SimpleCookie()
        try:
            jar.load(value)
        except CookieError:
            continue
        for morsel in jar.values():
            domain = (morsel["domain"] or host).lstrip(".")
            path = morsel["path"] or "/"
            if morsel["max-age"] in ("0", "-1"):
                try:
                    cookies.delete(morsel.key, domain=domain, path=path)
                except Exception:  # noqa: BLE001 - not there, nothing to drop
                    pass
                continue
            cookies.set(morsel.key, morsel.value, domain=domain, path=path)
