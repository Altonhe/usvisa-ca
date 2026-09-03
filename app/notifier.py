"""Telegram bot notifications.

Replaces the old Gmail/SMTP path.  Notifications are strictly best-effort: a
Telegram outage must never interrupt polling, so every failure is logged and
swallowed.

Set up:
  1. talk to @BotFather, ``/newbot``, copy the token
  2. send your bot any message, then read your numeric chat id from
     ``https://api.telegram.org/bot<TOKEN>/getUpdates``
  3. put both into ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
"""

from __future__ import annotations

import html
from datetime import date
from typing import Callable, List, Optional

import requests

from .config import TelegramConfig

API_ROOT = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        timeout: int = 15,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.timeout = timeout
        self._log = logger or (lambda msg: print(msg, flush=True))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    # -- transport -------------------------------------------------------

    def send(self, text: str) -> bool:
        """Send an HTML-formatted message. Returns True when Telegram accepted it."""
        if not self.enabled:
            return False
        url = f"{API_ROOT}/bot{self.config.bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self.config.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self._log(f"telegram send failed (ignored): {exc}")
            return False
        if resp.status_code != 200:
            # Never echo the token; it lives in the URL.
            self._log(
                f"telegram rejected the message: HTTP {resp.status_code} "
                f"{resp.text[:180]}"
            )
            return False
        return True

    def verify(self) -> bool:
        """Check the token with ``getMe`` so misconfiguration is caught at boot."""
        if not self.enabled:
            self._log("telegram not configured; notifications are off")
            return False
        try:
            resp = requests.get(
                f"{API_ROOT}/bot{self.config.bot_token}/getMe", timeout=self.timeout
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"telegram getMe failed: {exc}")
            return False
        if not data.get("ok"):
            self._log(f"telegram token rejected: {data.get('description')}")
            return False
        self._log(f"telegram bot @{data['result'].get('username')} ready")
        return True

    # -- events ----------------------------------------------------------

    def startup(self, accounts: int, applications: int, test_mode: bool) -> None:
        mode = "TEST MODE (nothing will be booked)" if test_mode else "LIVE (will book)"
        self.send(
            "<b>US visa watcher started</b>\n"
            f"Accounts: {accounts}\n"
            f"Applications watched: {applications}\n"
            f"Mode: {mode}"
        )

    def slot_found(
        self,
        account: str,
        application: str,
        consulate: str,
        day: date,
        window: str,
        booking: bool,
    ) -> None:
        action = (
            "booking attempted, did not go through; will retry"
            if booking
            else "test mode, not booking"
        )
        self.send(
            "<b>Slot found</b>\n"
            f"Account: {html.escape(account)}\n"
            f"Application: {html.escape(application)}\n"
            f"Consulate: {html.escape(consulate)}\n"
            f"Date: <b>{day}</b>\n"
            f"Target window: {html.escape(window)}\n"
            f"Action: {action}"
        )

    def booked(
        self, account: str, application: str, consulate: str, day: date, time: str
    ) -> None:
        self.send(
            "\u2705 <b>Appointment booked</b>\n"
            f"Account: {html.escape(account)}\n"
            f"Application: {html.escape(application)}\n"
            f"Consulate: {html.escape(consulate)}\n"
            f"When: <b>{day} {html.escape(time)}</b>"
        )

    def booking_failed(
        self, account: str, application: str, reason: str
    ) -> None:
        self.send(
            "\u26a0\ufe0f <b>Booking attempt failed</b>\n"
            f"Account: {html.escape(account)}\n"
            f"Application: {html.escape(application)}\n"
            f"Reason: {html.escape(reason)}"
        )

    def account_error(self, account: str, reason: str) -> None:
        self.send(
            "\u274c <b>Account error</b>\n"
            f"Account: {html.escape(account)}\n"
            f"{html.escape(reason)}"
        )

    def no_availability(self, account: str, checked: List[str]) -> None:
        if not self.config.notify_on_no_availability:
            return
        self.send(
            "<b>Sweep finished, nothing available</b>\n"
            f"Account: {html.escape(account)}\n"
            f"Checked: {html.escape(', '.join(checked))}"
        )
