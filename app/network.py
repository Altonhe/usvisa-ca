"""Egress control: tell an IP ban from an account ban, and recover from it.

There are two kinds of block, and they need opposite responses:

* **Account ban.** One account is refused; the others, on the same egress IP,
  are served normally. Resting that account (see ``account_cooldown``) is
  right, and the rest of the pool carries its calendars.
* **IP ban.** The site refuses the egress address, so *every* account fails at
  once. Resting accounts is wrong here -- it punishes accounts that did nothing
  and fixes nothing. The egress has to change instead.

The worker cannot tell the two apart from one failure, so when no account
succeeded in a sweep it asks :meth:`NetworkGuard.probe`: one unauthenticated
GET of the sign-in page through the current egress. A clean answer means the
IP is fine and the failures were the accounts'. A refusal means the IP is
blocked, and :meth:`NetworkGuard.recover` rotates the OpenVPN provider through
its profiles until the site answers again.

The OpenVPN provider is the sidecar in ``docker/openvpn-provider`` (copied
from the bcpark project): it runs OpenVPN, exposes a SOCKS5 proxy bound to the
tunnel and a small control API (``/status``, ``/profiles``, ``/switch``).
Without it configured, IP bans are still *detected* -- so accounts are not
wrongly rested -- and the whole pool pauses instead.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

import requests

SIGN_IN_PATH = "/{locale}/niv/users/sign_in"
BASE_URL = "https://ais.usvisa-info.com"
EGRESS_IP_URL = "https://api.ipify.org?format=json"

# Probe verdicts.
OK = "ok"                    # the site answered this egress normally
BLOCKED = "blocked"          # refused / unreachable through this egress
MAINTENANCE = "maintenance"  # the site itself is down for everyone


@dataclass
class NetworkConfig:
    # Control API of the OpenVPN provider sidecar, e.g.
    # http://openvpn-provider:8080. Empty disables egress rotation.
    openvpn_provider_url: str = ""
    # Proxy every AIS request goes through, e.g. socks5h://openvpn-socks:1080.
    # Empty means direct.
    proxy: str = ""
    # Probe the egress when a whole sweep fails, so an IP ban is not mistaken
    # for every account being banned at once.
    ip_ban_detection: bool = True
    # If every VPN profile is refused but the host's own address is not, fall
    # back to it (the bcpark behaviour). Off keeps traffic on the VPN only.
    direct_fallback: bool = False
    # Seconds to wait for a switched profile to report "connected".
    switch_timeout: int = 60
    # Seconds the whole pool pauses when no egress works at all.
    pause_on_ip_ban: int = 300
    # After a direct fallback, how often (seconds) to check whether the VPN
    # works again and move traffic back onto it.
    vpn_retry_interval: int = 300
    # Per-request timeout for the probe and the control API.
    timeout: int = 15

    @property
    def provider_enabled(self) -> bool:
        return bool(self.openvpn_provider_url)


@dataclass
class ProbeResult:
    verdict: str
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return self.verdict == OK


class ProviderError(RuntimeError):
    pass


class NetworkGuard:
    """Owns the egress: which proxy AIS traffic uses, and rotating it."""

    def __init__(
        self,
        config: NetworkConfig,
        locale: str = "en-ca",
        user_agent: str = "",
        logger: Optional[Callable[[str], None]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config
        self.locale = locale
        self.user_agent = user_agent
        self._log = logger or (lambda m: print(m, flush=True))
        # Used for the probe and the control API; never carries AIS cookies.
        self._http = session or requests.Session()
        self._lock = threading.Lock()
        self._recovering = threading.Lock()
        # "vpn" when routing through config.proxy, "direct" after a fallback.
        self._mode = "vpn" if config.proxy else "direct"
        self._paused_until = 0.0
        # Monotonic time of the next "is the VPN back?" check while direct.
        self._next_vpn_retry = 0.0
        self.last_probe: Optional[ProbeResult] = None
        self.last_verdict = ""          # "account" | "ip" | "maintenance"
        self.ip_bans = 0
        self.history: Deque[Dict[str, Any]] = deque(maxlen=50)

    # -- egress ------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def proxies(self) -> Dict[str, str]:
        """The ``requests`` proxies mapping AIS sessions must use right now."""
        if self._mode == "vpn" and self.config.proxy:
            return {"http": self.config.proxy, "https": self.config.proxy}
        return {}

    def apply(self, session: requests.Session) -> None:
        """Point an AIS session at the current egress."""
        wanted = self.proxies()
        if session.proxies != wanted:
            session.proxies.clear()
            session.proxies.update(wanted)
            # Pooled keep-alive connections still run over the old route.
            session.close()

    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def pause_left(self) -> int:
        return max(0, int(self._paused_until - time.monotonic()))

    def check_paused(self, should_stop: Callable[[], bool] = lambda: False) -> bool:
        """While paused, look for a working egress instead of just waiting.

        One unauthenticated probe of the current egress -- no account is
        touched. If the site answers, the pause is lifted at once. If not, the
        provider is stepped to its next profile (one per call, so a sweep stays
        short) and that is probed. Returns True when the pool may run again.
        """
        if not self.paused():
            return True
        if not self._recovering.acquire(blocking=False):
            return False
        try:
            result = self.probe()
            if result.verdict == MAINTENANCE:
                return False  # nothing an egress change can fix
            if not result.ok and self.config.provider_enabled and self.config.proxy:
                if self._rotate(should_stop, limit=1):
                    result = self.last_probe or result
            if not result.ok and self._try_direct():
                result = self.last_probe or result
            if result.ok:
                self._paused_until = 0.0
                self._log("network: egress works again; resuming the pool")
                self._record("resumed", mode=self._mode, detail=result.detail)
                return True
            return False
        finally:
            self._recovering.release()

    def maybe_return_to_vpn(self, should_stop: Callable[[], bool] = lambda: False) -> bool:
        """While on a direct fallback, move back to the VPN once it works.

        Checked at most every ``vpn_retry_interval`` seconds: the current
        profile is probed through the proxy, and if it is refused the provider
        is stepped to the next profile and that is probed. Returns True when
        traffic was moved back onto the VPN.
        """
        if self._mode != "direct" or not self.config.proxy:
            return False
        now = time.monotonic()
        if now < self._next_vpn_retry:
            return False
        self._next_vpn_retry = now + max(1, self.config.vpn_retry_interval)
        if not self._recovering.acquire(blocking=False):
            return False
        try:
            vpn = {"http": self.config.proxy, "https": self.config.proxy}
            result = self.probe(proxies=vpn)
            if not result.ok and self.config.provider_enabled:
                # _rotate sets the mode back to "vpn" on success.
                if not self._rotate(should_stop, limit=1):
                    self._record("vpn-still-down", detail=result.detail)
                    return False
            elif not result.ok:
                return False
            self._mode = "vpn"
            self._log("network: the VPN works again; moving traffic back off the direct connection")
            self._record("back-to-vpn", detail=(self.last_probe or result).detail)
            return True
        finally:
            self._recovering.release()

    def _try_direct(self) -> bool:
        """Switch to the host's own address if allowed and the site takes it."""
        if not (self.config.direct_fallback and self._mode == "vpn" and self.config.proxy):
            return False
        result = self.probe(proxies={})
        if not result.ok:
            return False
        self._mode = "direct"
        self._next_vpn_retry = time.monotonic() + max(1, self.config.vpn_retry_interval)
        self._log("network: every VPN profile is refused; using the direct connection")
        self._record("fallback", mode="direct", detail=result.detail)
        return True

    # -- probing -----------------------------------------------------------

    def probe(self, proxies: Optional[Dict[str, str]] = None) -> ProbeResult:
        """One unauthenticated GET of the sign-in page through an egress."""
        url = BASE_URL + SIGN_IN_PATH.format(locale=self.locale)
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        route = self.proxies() if proxies is None else proxies
        try:
            resp = self._http.get(
                url, headers=headers, proxies=route,
                timeout=self.config.timeout, allow_redirects=True,
            )
        except requests.RequestException as exc:
            result = ProbeResult(BLOCKED, f"{type(exc).__name__}: {exc}")
        else:
            body = resp.text[:20000].lower() if resp.text else ""
            if resp.status_code == 503 and "maintenance" in body:
                result = ProbeResult(MAINTENANCE, "site reports planned maintenance")
            elif resp.status_code == 200 and ("sign_in" in body or "user[email]" in body):
                result = ProbeResult(OK, "sign-in page served")
            else:
                result = ProbeResult(BLOCKED, f"HTTP {resp.status_code}")
        self.last_probe = result
        return result

    def classify_failed_sweep(self) -> str:
        """Why did every account fail? ``account``, ``ip`` or ``maintenance``."""
        if not self.config.ip_ban_detection:
            return "account"
        result = self.probe()
        verdict = {OK: "account", BLOCKED: "ip", MAINTENANCE: "maintenance"}[result.verdict]
        self.last_verdict = verdict
        self._record("probe", verdict=verdict, detail=result.detail, mode=self._mode)
        return verdict

    # -- recovery ----------------------------------------------------------

    def recover(self, should_stop: Callable[[], bool] = lambda: False) -> bool:
        """Find an egress the site accepts. Returns True once one works.

        Walks every provider profile after the active one; each is switched to,
        waited on until connected, and probed. Falls back to direct only when
        ``direct_fallback`` allows it. When nothing works the pool is paused
        for ``pause_on_ip_ban`` seconds instead of hammering a dead route.
        """
        if not self._recovering.acquire(blocking=False):
            return False  # another recovery is already running
        try:
            self.ip_bans += 1
            if self.config.provider_enabled and self.config.proxy:
                if self._rotate(should_stop):
                    return True
            if self.config.direct_fallback and self._mode == "vpn":
                if self._try_direct():
                    return True
            elif self._mode == "direct" and self.config.proxy:
                # A previous fallback put us on the host IP, which is now
                # refused too: going back to the VPN is the only way out.
                self._mode = "vpn"
            self._paused_until = time.monotonic() + max(0, self.config.pause_on_ip_ban)
            self._log(
                "network: no working egress; pausing the whole pool for "
                f"{self.config.pause_on_ip_ban}s"
            )
            self._record("paused", seconds=self.config.pause_on_ip_ban)
            return False
        finally:
            self._recovering.release()

    def _rotate(self, should_stop: Callable[[], bool], limit: Optional[int] = None) -> bool:
        try:
            status = self.provider_status()
            profiles = self.profiles()
        except ProviderError as exc:
            self._log(f"network: OpenVPN provider unavailable: {exc}")
            self._record("provider-offline", detail=str(exc))
            return False
        if not profiles:
            self._log("network: OpenVPN provider has no profiles to rotate to")
            return False

        active = status.get("activeProfile") or ""
        start = profiles.index(active) if active in profiles else -1
        order = [profiles[(start + i) % len(profiles)] for i in range(1, len(profiles) + 1)]
        if limit is not None:
            order = order[:limit]
        for profile in order:
            if should_stop():
                return False
            self._log(f"network: switching OpenVPN profile to {profile}")
            try:
                self.switch(profile=profile)
            except ProviderError as exc:
                self._record("switch-failed", profile=profile, detail=str(exc))
                continue
            state = self._wait_connected(should_stop)
            if state != "connected":
                self._record("connect-failed", profile=profile, detail=state)
                continue
            result = self.probe(proxies={"http": self.config.proxy, "https": self.config.proxy})
            if result.ok:
                self._mode = "vpn"
                self._log(f"network: {profile} is accepted by the site")
                self._record("switched", profile=profile, detail=result.detail)
                return True
            self._record("refused", profile=profile, detail=result.detail)
        return False

    def _wait_connected(self, should_stop: Callable[[], bool]) -> str:
        deadline = time.monotonic() + max(1, self.config.switch_timeout)
        state = "unknown"
        while time.monotonic() < deadline and not should_stop():
            try:
                state = self.provider_status().get("status", "unknown")
            except ProviderError:
                state = "offline"
            if state in ("connected", "error"):
                return state
            time.sleep(2)
        return state

    # -- provider control API ----------------------------------------------

    def _provider(self, method: str, path: str, payload: Optional[dict] = None) -> Dict[str, Any]:
        if not self.config.provider_enabled:
            raise ProviderError("openvpn_provider_url is not configured")
        url = self.config.openvpn_provider_url.rstrip("/") + path
        try:
            resp = self._http.request(
                method, url, json=payload, timeout=self.config.timeout, proxies={},
            )
        except requests.RequestException as exc:
            raise ProviderError(str(exc)) from None
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:200]}
        # /health answers 503 while not connected but still carries a status.
        if resp.status_code >= 400 and not (isinstance(body, dict) and "status" in body):
            detail = body.get("error") if isinstance(body, dict) else resp.text[:200]
            raise ProviderError(f"HTTP {resp.status_code}: {detail}")
        return body if isinstance(body, dict) else {}

    def provider_status(self) -> Dict[str, Any]:
        return self._provider("GET", "/status")

    def profiles(self) -> List[str]:
        return list(self._provider("GET", "/profiles").get("profiles") or [])

    def switch(self, profile: str = "", random: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"random": True} if random else {"profile": profile}
        body = self._provider("POST", "/switch", payload)
        self._record("manual-switch" if not self._recovering.locked() else "switch",
                     profile=profile or "random")
        # A manual switch is an explicit choice of the VPN route. Automatic
        # rotation decides the mode itself, only once a profile is accepted.
        if self.config.proxy and not self._recovering.locked():
            self._mode = "vpn"
        return body

    def egress_ip(self) -> Dict[str, Any]:
        """The public address AIS traffic currently leaves from."""
        try:
            resp = self._http.get(EGRESS_IP_URL, proxies=self.proxies(),
                                  timeout=self.config.timeout)
            resp.raise_for_status()
            return {"ok": True, "ip": resp.json().get("ip"), "mode": self._mode}
        except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
            return {"ok": False, "error": str(exc), "mode": self._mode}

    # -- reporting ---------------------------------------------------------

    def _record(self, event: str, **fields: Any) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": event, **fields}
        with self._lock:
            self.history.appendleft(entry)

    def status(self) -> Dict[str, Any]:
        """Everything the dashboard shows. Never raises."""
        provider: Dict[str, Any] = {}
        profiles: List[str] = []
        if self.config.provider_enabled:
            try:
                provider = self.provider_status()
                profiles = self.profiles()
            except ProviderError as exc:
                provider = {"status": "offline", "error": str(exc)}
        with self._lock:
            history = list(self.history)
        return {
            "enabled": True,
            "mode": self._mode,
            "proxy": self.config.proxy,
            "provider_enabled": self.config.provider_enabled,
            "provider": provider,
            "profiles": profiles,
            "ip_ban_detection": self.config.ip_ban_detection,
            "direct_fallback": self.config.direct_fallback,
            "paused": self.paused(),
            "pause_left": self.pause_left(),
            "ip_bans": self.ip_bans,
            "last_verdict": self.last_verdict,
            "last_probe": (
                {"verdict": self.last_probe.verdict, "detail": self.last_probe.detail,
                 "at": self.last_probe.at}
                if self.last_probe else None
            ),
            "history": history,
        }
