"""CapSolver client for reCAPTCHA v2/v3.

API reference: https://docs.capsolver.com/en/api/

The AIS site renders an *invisible* widget driven by
``grecaptcha.execute(clientId, {action})``, which is the reCAPTCHA v3 contract,
so :meth:`CapSolver.solve` defaults to ``ReCaptchaV3TaskProxyLess`` whenever an
action is present and falls back to ``ReCaptchaV2TaskProxyLess`` otherwise.
Both the site key and the action are read out of the live page at solve time
because the server only injects them when it decides to arm the challenge.
"""

import time
from datetime import datetime
from typing import Dict, Optional

import requests

CREATE_TASK_URL = "https://api.capsolver.com/createTask"
GET_TASK_RESULT_URL = "https://api.capsolver.com/getTaskResult"

# CapSolver caps getTaskResult at 120 polls per task and 5 minutes of lifetime.
MAX_POLLS = 110
MAX_WAIT_SECONDS = 280


class CapSolverError(RuntimeError):
    """Raised when CapSolver cannot produce a token."""


class CapSolver:
    """Thin synchronous wrapper around the CapSolver createTask/getTaskResult pair."""

    def __init__(
        self,
        api_key: str,
        poll_interval: float = 3.0,
        timeout: float = MAX_WAIT_SECONDS,
        request_timeout: float = 30.0,
        logger=None,
    ):
        if not api_key:
            raise CapSolverError("CapSolver API key is empty")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.timeout = min(timeout, MAX_WAIT_SECONDS)
        self.request_timeout = request_timeout
        self._log = logger or self._default_log

    @staticmethod
    def _default_log(message: str) -> None:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [capsolver] {message}")

    # -- public API ------------------------------------------------------

    def solve(
        self,
        website_url: str,
        website_key: str,
        page_action: str = "",
        invisible: bool = False,
        version: Optional[int] = None,
        is_session: bool = False,
    ) -> Dict[str, str]:
        """Solve a challenge and return CapSolver's ``solution`` dict.

        The dict always carries ``gRecaptchaResponse``.  It may additionally
        carry ``userAgent`` plus ``recaptcha-ca-t`` / ``recaptcha-ca-e`` cookie
        values, which the caller should mirror onto its session when present.
        """
        if not website_key:
            raise CapSolverError(
                "No reCAPTCHA sitekey available; the page did not expose data-sitekey"
            )

        if version is None:
            version = 3 if page_action else 2

        if version == 3:
            task = {
                "type": "ReCaptchaV3TaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
            }
            if page_action:
                task["pageAction"] = page_action
        else:
            task = {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
            }
            if invisible:
                task["isInvisible"] = True

        if is_session:
            task["isSession"] = True

        self._log(
            f"creating {task['type']} task (sitekey={website_key[:12]}…"
            + (f", action={page_action}" if page_action else "")
            + ")"
        )
        task_id = self._create_task(task)
        solution = self._await_result(task_id)
        token = solution.get("gRecaptchaResponse") or ""
        if not token:
            raise CapSolverError(f"CapSolver returned no gRecaptchaResponse: {solution}")
        self._log(f"solved, token length {len(token)}")
        return solution

    def balance(self) -> Optional[float]:
        """Account balance, or ``None`` if the endpoint is unavailable."""
        try:
            resp = requests.post(
                "https://api.capsolver.com/getBalance",
                json={"clientKey": self.api_key},
                timeout=self.request_timeout,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"balance check failed: {exc}")
            return None
        if data.get("errorId"):
            self._log(f"balance check error: {data.get('errorDescription')}")
            return None
        return data.get("balance")

    # -- internals -------------------------------------------------------

    def _post(self, url: str, payload: dict) -> dict:
        try:
            resp = requests.post(url, json=payload, timeout=self.request_timeout)
        except requests.RequestException as exc:
            raise CapSolverError(f"CapSolver request to {url} failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise CapSolverError(
                f"CapSolver returned non-JSON from {url} "
                f"(HTTP {resp.status_code}): {resp.text[:200]}"
            ) from exc
        return data

    def _create_task(self, task: dict) -> str:
        data = self._post(CREATE_TASK_URL, {"clientKey": self.api_key, "task": task})
        if data.get("errorId"):
            raise CapSolverError(
                f"createTask failed: {data.get('errorCode')} "
                f"{data.get('errorDescription')}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise CapSolverError(f"createTask returned no taskId: {data}")
        return task_id

    def _await_result(self, task_id: str) -> Dict[str, str]:
        started = time.time()
        for attempt in range(MAX_POLLS):
            elapsed = time.time() - started
            if elapsed > self.timeout:
                raise CapSolverError(
                    f"CapSolver timed out after {elapsed:.0f}s (task {task_id})"
                )
            time.sleep(self.poll_interval)
            data = self._post(
                GET_TASK_RESULT_URL, {"clientKey": self.api_key, "taskId": task_id}
            )
            if data.get("errorId"):
                raise CapSolverError(
                    f"getTaskResult failed: {data.get('errorCode')} "
                    f"{data.get('errorDescription')}"
                )
            status = data.get("status")
            if status == "ready":
                return data.get("solution") or {}
            if status == "failed":
                raise CapSolverError(f"CapSolver reported task failure: {data}")
            if attempt and attempt % 10 == 0:
                self._log(f"still {status} after {elapsed:.0f}s…")
        raise CapSolverError(f"CapSolver poll limit reached for task {task_id}")
