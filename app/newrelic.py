"""New Relic Metric API client.

Pushes one batch of gauges after every sweep. No agent, no sidecar, no scraper --
just an HTTPS POST, which is why this replaced the self-hosted Prometheus stack.

Contract (https://docs.newrelic.com/docs/data-apis/ingest-apis/metric-api/report-metrics-metric-api/):

* ``POST https://metric-api.newrelic.com/metric/v1`` with header ``Api-Key``
* body is ``[{"common": {...}, "metrics": [...]}]``, gzip allowed
* ``202`` means accepted; payloads must stay under 1 MB
* data points older than 48 h or more than 24 h in the future are dropped
* a 202 does **not** guarantee ingestion -- asynchronous validation failures show
  up as ``NrIntegrationError`` events, so query those if a metric never appears

**DNS-over-HTTPS fallback.** New Relic is an analytics product, so its domains sit
on tracker blocklists: AdGuard, Pi-hole and hosts-file blockers resolve
``metric-api.newrelic.com`` to ``0.0.0.0``, and connecting there fails with
"connection refused" -- which reads like a New Relic outage. Overriding the
container's ``dns:`` does not help, because those blockers intercept port 53 at
the network level regardless of the configured upstream. So when ``dns_mode`` is
``auto`` (the default) and the hostname comes back blackholed, this client
re-resolves over DoH (port 443, which the blocker cannot inspect) and connects to
that address directly. TLS is unaffected: SNI and certificate verification still
use the real hostname, so this is a resolution bypass, not a trust bypass.

Failures are logged and swallowed: monitoring must never take the watcher down.
"""

from __future__ import annotations

import gzip
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import certifi
import requests
import urllib3

from .metrics import Sample, to_newrelic

# Region -> ingest endpoint.
ENDPOINTS = {
    "us": "https://metric-api.newrelic.com/metric/v1",
    "eu": "https://metric-api.eu.newrelic.com/metric/v1",
    "jp": "https://metric-api.jp.nr-data.net/metric/v1",
}

DNS_MODES = ("auto", "system", "doh")

# Queried over HTTPS, so a port-53 blocker cannot see or answer them.
DOH_PROVIDERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

# What a DNS-level blocker hands back instead of a real address.
BLACKHOLE_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::", "::1"}

MAX_PAYLOAD_BYTES = 900_000        # API limit is 1 MB; leave headroom
GZIP_THRESHOLD_BYTES = 4_096
DOH_CACHE_TTL_SECONDS = 300        # New Relic rotates these addresses


class NewRelicError(RuntimeError):
    pass


class NewRelicClient:
    def __init__(
        self,
        license_key: str,
        region: str = "us",
        dns_mode: str = "auto",
        timeout: int = 15,
        common_attributes: Optional[Dict[str, Any]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.license_key = license_key.strip()
        self.region = (region or "us").strip().lower()
        if self.region not in ENDPOINTS:
            raise NewRelicError(
                f"unknown New Relic region {region!r}; expected one of "
                + ", ".join(ENDPOINTS)
            )
        self.dns_mode = (dns_mode or "auto").strip().lower()
        if self.dns_mode not in DNS_MODES:
            raise NewRelicError(
                f"unknown newrelic.dns_mode {dns_mode!r}; expected one of "
                + ", ".join(DNS_MODES)
            )

        self.endpoint = ENDPOINTS[self.region]
        parsed = urlparse(self.endpoint)
        self.host = parsed.hostname or ""
        self.path = parsed.path or "/"

        self.timeout = timeout
        self.common_attributes = dict(common_attributes or {})
        self._log = logger or (lambda msg: print(msg, flush=True))
        self._consecutive_failures = 0
        self._pinned_ip: Optional[str] = None
        self._pinned_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.license_key)

    # -- public ----------------------------------------------------------

    def send(self, samples: List[Sample]) -> bool:
        """Push samples. Returns True when New Relic accepted them."""
        if not self.enabled or not samples:
            return False
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for batch in self._batches(samples, timestamp_ms):
            if not self._post(batch):
                return False
        self._consecutive_failures = 0
        return True

    def verify(self) -> bool:
        """Send a single probe gauge so a bad key or a blocked host is caught early."""
        if not self.enabled:
            self._log("new relic not configured; metrics are not being sent")
            return False
        if self.send([Sample(name="up", value=1, help="startup probe")]):
            via = f" via DoH ({self._pinned_ip})" if self._pinned_ip else ""
            self._log(
                f"new relic ready ({self.region} endpoint){via}; query with: "
                "FROM Metric SELECT latest(usvisa.earliest_slot_days) "
                "FACET account, application, consulate TIMESERIES"
            )
            return True
        return False

    # -- batching --------------------------------------------------------

    def _batches(self, samples: List[Sample], timestamp_ms: int):
        """Split into payloads that stay under the size limit."""
        payload = to_newrelic(samples, self.common_attributes, timestamp_ms)
        body = json.dumps(payload).encode("utf-8")
        if len(body) <= MAX_PAYLOAD_BYTES or len(samples) == 1:
            yield body
            return
        mid = len(samples) // 2
        yield from self._batches(samples[:mid], timestamp_ms)
        yield from self._batches(samples[mid:], timestamp_ms)

    # -- transport -------------------------------------------------------

    def _headers(self, body: bytes) -> Tuple[bytes, Dict[str, str]]:
        headers = {
            "Api-Key": self.license_key,
            "Content-Type": "application/json",
        }
        if len(body) > GZIP_THRESHOLD_BYTES:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return body, headers

    def _post(self, raw_body: bytes) -> bool:
        body, headers = self._headers(raw_body)

        # A live pin, or an explicit doh mode, skips system resolution entirely.
        if self.dns_mode == "doh" or self._pin_is_fresh():
            if self.dns_mode == "doh" and not self._pin_is_fresh():
                if not self._pin_via_doh():
                    return False
            result = self._post_pinned(body, headers)
            if result is not None:
                return result

        try:
            resp = requests.post(
                self.endpoint, data=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            if self.dns_mode == "auto" and self._is_blackholed():
                self._log(
                    f"new relic: {self.host} is blackholed by a DNS-level blocker; "
                    "re-resolving over DNS-over-HTTPS"
                )
                if self._pin_via_doh():
                    result = self._post_pinned(body, headers)
                    if result is not None:
                        return result
            self._fail(f"request failed: {exc}{self._dns_hint()}")
            return False

        return self._interpret(resp.status_code, resp.text)

    def _post_pinned(self, body: bytes, headers: Dict[str, str]) -> Optional[bool]:
        """POST to the pinned address. ``None`` means "could not attempt".

        SNI, ``Host`` and certificate verification all still use the real
        hostname, so only address resolution is bypassed.
        """
        if not self._pinned_ip:
            return None
        pinned = dict(headers, Host=self.host)
        try:
            pool = urllib3.HTTPSConnectionPool(
                host=self._pinned_ip,
                port=443,
                server_hostname=self.host,
                assert_hostname=self.host,
                cert_reqs="CERT_REQUIRED",
                ca_certs=certifi.where(),
                timeout=urllib3.Timeout(total=self.timeout),
                retries=False,
            )
            resp = pool.request("POST", self.path, body=body, headers=pinned)
        except Exception as exc:  # noqa: BLE001 - urllib3 raises several types
            self._pinned_ip = None          # stale or wrong; re-resolve next time
            self._fail(f"pinned request to {self.host} failed: {exc}")
            return False
        text = (resp.data or b"")[:200].decode("utf-8", "replace")
        return self._interpret(resp.status, text)

    def _interpret(self, status: int, text: str) -> bool:
        if status == 202:
            return True
        if status == 403:
            self._fail("authentication failed -- check newrelic.license_key "
                       "(it must be a licence/ingest key, not a user API key)")
        elif status == 429:
            self._fail("rate limited; this batch was dropped")
        else:
            self._fail(f"HTTP {status}: {text}")
        return False

    # -- DNS -------------------------------------------------------------

    def _resolve_system(self) -> set:
        try:
            return {ai[4][0] for ai in socket.getaddrinfo(self.host, 443)}
        except OSError:
            return set()

    def _is_blackholed(self) -> bool:
        addresses = self._resolve_system()
        return bool(addresses) and bool(addresses & BLACKHOLE_ADDRESSES)

    def _pin_is_fresh(self) -> bool:
        return bool(
            self._pinned_ip
            and time.time() - self._pinned_at < DOH_CACHE_TTL_SECONDS
        )

    def _pin_via_doh(self) -> bool:
        """Resolve the endpoint over HTTPS and remember the address."""
        for provider in DOH_PROVIDERS:
            try:
                resp = requests.get(
                    provider,
                    params={"name": self.host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                    timeout=self.timeout,
                )
                answers = resp.json().get("Answer") or []
            except Exception:  # noqa: BLE001 - try the next provider
                continue
            addresses = [
                a["data"] for a in answers
                if a.get("type") == 1 and a.get("data") not in BLACKHOLE_ADDRESSES
            ]
            if addresses:
                self._pinned_ip = addresses[0]
                self._pinned_at = time.time()
                self._log(
                    f"new relic: resolved {self.host} to {self._pinned_ip} over DoH "
                    f"({urlparse(provider).hostname})"
                )
                return True
        self._fail(
            "DNS-over-HTTPS resolution also failed; the blocker may be filtering "
            "the DoH providers too. Allowlist newrelic.com in it, or exclude the "
            "Docker/WSL processes from its filtering."
        )
        return False

    def _dns_hint(self) -> str:
        """Explain a DNS blackhole -- the least obvious way this fails."""
        addresses = self._resolve_system()
        if not addresses:
            return f"\n  {self.host} does not resolve at all -- check the container's DNS."
        if addresses & BLACKHOLE_ADDRESSES:
            return (
                f"\n  {self.host} resolves to {', '.join(sorted(addresses))}: a "
                "DNS-level blocker (AdGuard, Pi-hole, a hosts entry) is dropping it "
                "as a tracker domain."
                "\n  The DoH fallback should handle this; set newrelic.dns_mode: doh "
                "to skip system resolution entirely, or allowlist newrelic.com in "
                "the blocker."
            )
        return ""

    # -- logging ---------------------------------------------------------

    def _fail(self, message: str) -> None:
        self._consecutive_failures += 1
        # Stay quiet after the first few so a long outage cannot flood the log.
        if self._consecutive_failures <= 3 or self._consecutive_failures % 20 == 0:
            self._log(
                f"new relic: {message}"
                + (f" (failure #{self._consecutive_failures})"
                   if self._consecutive_failures > 1 else "")
            )
