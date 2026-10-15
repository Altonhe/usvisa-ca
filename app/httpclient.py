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


class Session(_curl.Session):
    """A curl_cffi session that impersonates Chrome unless told otherwise."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("impersonate", IMPERSONATE)
        super().__init__(**kwargs)


def request(method: str, url: str, **kwargs: Any) -> Response:
    kwargs.setdefault("impersonate", IMPERSONATE)
    return _curl.request(method, url, **kwargs)


def get(url: str, **kwargs: Any) -> Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return request("POST", url, **kwargs)
