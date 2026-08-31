"""Pure-``requests`` client for the AIS visa appointment system (Canada).

Everything the old Selenium implementation did by clicking is done here over
HTTP.  Endpoint paths and field names below were all read off the live site:

* ``POST /{locale}/niv/users/sign_in``
      ``user[email]``, ``user[password]``, ``policy_confirmed=1``, plus the CSRF
      token from ``<meta name="csrf-token">`` (the form has no hidden token).
* ``GET  /{locale}/niv/groups/{group_id}``
      landing page listing every application as a ``.../continue_actions`` link.
* ``GET  /{locale}/niv/schedule/{id}/continue_actions``
      offers ``.../continue`` labelled "Schedule Appointment" (never booked) or
      "Reschedule Appointment" (already booked).  Completed applications offer
      neither, which is how they are recognised.
* ``GET  /{locale}/niv/schedule/{id}/appointment/days/{facility}.json``
      ``[{"date": "YYYY-MM-DD", "business_day": true}, ...]``
* ``GET  /{locale}/niv/schedule/{id}/appointment/times/{facility}.json?date=...``
      ``{"available_times": [...], "business_times": [...]}``
* ``POST /{locale}/niv/schedule/{id}/appointment``
      ``authenticity_token``, ``confirmed_limit_message``,
      ``use_consulate_appointment_capacity``,
      ``appointments[consulate_appointment][facility_id]`` (**required** -- the
      date field stays empty until it is set), ``[date]``, ``[time]``, ``commit``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

import requests

from .capsolver import CapSolver
from .consulates import consulate_name
from .htmlutil import Page

BASE_URL = "https://ais.usvisa-info.com"

SCHEDULE_LINK_RE = re.compile(r"/niv/schedule/(\d+)/continue_actions")
CONSULAR_APPT_RE = re.compile(
    r"Consular Appointment:\s*(\d{1,2}\s+[A-Za-z]+,?\s*\d{4})", re.I
)

FIRST_TIME_LABEL = "schedule appointment"
RESCHEDULE_LABEL = "reschedule appointment"

FACILITY_KEY = "appointments[consulate_appointment][facility_id]"
DATE_KEY = "appointments[consulate_appointment][date]"
TIME_KEY = "appointments[consulate_appointment][time]"


class AisError(RuntimeError):
    """Base class for AIS client failures."""


class LoginFailed(AisError):
    pass


class CaptchaBlocked(LoginFailed):
    """A challenge was served but no solver is configured."""


@dataclass
class Schedule:
    """One application (one ``schedule_id``) on an account."""

    id: str
    action_label: str = ""
    continue_url: str = ""
    current_appointment: Optional[str] = None

    @property
    def actionable(self) -> bool:
        return bool(self.continue_url)

    @property
    def is_reschedule(self) -> bool:
        return RESCHEDULE_LABEL in self.action_label.lower()

    @property
    def kind(self) -> str:
        if not self.actionable:
            return "done"
        return "reschedule" if self.is_reschedule else "first_time"

    def describe(self) -> str:
        if not self.actionable:
            return f"schedule {self.id}: no scheduling action (completed or locked)"
        current = f", currently {self.current_appointment}" if self.current_appointment else ""
        return f"schedule {self.id}: {self.kind} ({self.action_label}){current}"


@dataclass
class Slot:
    """A bookable (consulate, day, time) triple."""

    schedule_id: str
    facility_id: int
    day: date
    time: str = ""

    @property
    def consulate(self) -> str:
        return consulate_name(self.facility_id)

    def __str__(self) -> str:
        stamp = f"{self.day} {self.time}".strip()
        return f"{self.consulate} @ {stamp}"


class AisClient:
    def __init__(
        self,
        email: str,
        password: str,
        capsolver: Optional[CapSolver] = None,
        locale: str = "en-ca",
        user_agent: str = "",
        timeout: int = 30,
        request_delay: float = 0.0,
        logger: Optional[Callable[[str], None]] = None,
    ):
        if not email or not password:
            raise AisError("account email/password are not configured")
        self.email = email
        self.password = password
        self.capsolver = capsolver
        self.locale = locale
        self.timeout = timeout
        self.request_delay = request_delay
        self._log = logger or (lambda msg: print(msg, flush=True))
        self.landing_url: str = ""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        })

    # -- URLs ------------------------------------------------------------

    @property
    def login_url(self) -> str:
        return f"{BASE_URL}/{self.locale}/niv/users/sign_in"

    @property
    def account_home_url(self) -> str:
        """Stable post-login entry point; redirects to /niv/groups/{group_id}."""
        return f"{BASE_URL}/{self.locale}/niv"

    def continue_actions_url(self, schedule_id: str) -> str:
        return f"{BASE_URL}/{self.locale}/niv/schedule/{schedule_id}/continue_actions"

    def schedule_continue_url(self, schedule_id: str) -> str:
        return f"{BASE_URL}/{self.locale}/niv/schedule/{schedule_id}/continue"

    def appointment_url(self, schedule_id: str) -> str:
        return f"{BASE_URL}/{self.locale}/niv/schedule/{schedule_id}/appointment"

    def days_url(self, schedule_id: str, facility_id: int) -> str:
        return (
            f"{self.appointment_url(schedule_id)}/days/{facility_id}.json"
            "?appointments[expedite]=false"
        )

    def times_url(self, schedule_id: str, facility_id: int, day: date) -> str:
        return (
            f"{self.appointment_url(schedule_id)}/times/{facility_id}.json"
            f"?date={day.isoformat()}&appointments[expedite]=false"
        )

    # -- plumbing --------------------------------------------------------

    def _get_page(self, url: str, referer: str = "") -> Page:
        headers = {"Referer": referer} if referer else {}
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return Page(resp.text, base_url=resp.url)

    def _get_json(self, url: str, referer: str = ""):
        """GET a JSON endpoint.

        Returns the decoded body, or ``None`` if the request itself failed.  A
        successful request yielding an empty list means "no availability", which
        is a normal outcome and must not be reported as an error.
        """
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if referer:
            headers["Referer"] = referer
        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            self._log(f"request failed: {exc}")
            return None
        if resp.status_code != 200:
            self._log(f"HTTP {resp.status_code} from {url}")
            return None
        try:
            return resp.json()
        except ValueError:
            snippet = resp.text[:160].replace("\n", " ")
            self._log(f"non-JSON response from {url}: {snippet}")
            return None

    def _post_form(self, url: str, payload: Dict[str, str], page: Page) -> requests.Response:
        return self.session.post(
            url,
            data=payload,
            headers={
                "Referer": page.base_url or url,
                "Origin": BASE_URL,
                "X-CSRF-Token": page.csrf_token(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
            allow_redirects=True,
        )

    # -- captcha ---------------------------------------------------------

    def _solve_captcha(self, page: Page, page_url: str) -> Dict[str, str]:
        """Extra POST params needed to satisfy an armed reCAPTCHA.

        Returns ``{}`` when the page carries no challenge, which is the common
        case: the site only injects ``data-sitekey`` / ``data-action`` when it
        decides verification is required.
        """
        challenge = page.captcha_challenge()
        if not challenge:
            return {}

        self._log(
            f"reCAPTCHA v{challenge['version']} armed "
            f"(action={challenge['action'] or 'n/a'})"
        )
        if not self.capsolver:
            raise CaptchaBlocked(
                "the site is asking for reCAPTCHA but no capsolver_api_key is "
                "configured; add it to config.yaml to let the bot solve it"
            )

        solution = self.capsolver.solve(
            website_url=page_url,
            website_key=challenge["sitekey"],
            page_action=challenge["action"],
            invisible=challenge["invisible"],
            version=challenge["version"],
        )
        for cookie in ("recaptcha-ca-t", "recaptcha-ca-e"):
            if solution.get(cookie):
                self.session.cookies.set(cookie, solution[cookie], domain="ais.usvisa-info.com")
        if solution.get("userAgent"):
            # The token is bound to the UA that solved it.
            self.session.headers["User-Agent"] = solution["userAgent"]
        return {challenge["response_field"]: solution["gRecaptchaResponse"]}

    # -- authentication --------------------------------------------------

    def login(self) -> None:
        # The account e-mail is deliberately not logged: the worker's log is
        # rendered on the dashboard, which only ever shows a masked address.
        self._log("signing in")
        page = self._get_page(self.login_url)

        form = page.form_by_id("sign_in_form")
        if form is None:
            raise LoginFailed("sign_in_form not found; the login page layout changed")

        csrf = page.csrf_token()
        if not csrf:
            raise LoginFailed("no csrf-token meta tag on the login page")

        payload = {
            "utf8": "\u2713",
            page.metas.get("csrf-param") or "authenticity_token": csrf,
            "user[email]": self.email,
            "user[password]": self.password,
            "policy_confirmed": "1",
            "commit": "Sign In",
        }
        payload.update(self._solve_captcha(page, self.login_url))

        # sign_in_form carries data-remote="true", so the controller answers
        # text/javascript. Asking for text/html makes Rails raise UnknownFormat
        # and return a 404 page, which is why these headers matter.
        self.session.post(
            form.action or self.login_url,
            data=payload,
            headers={
                "Referer": self.login_url,
                "Origin": BASE_URL,
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, "
                          "application/ecmascript, application/x-ecmascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=self.timeout,
            allow_redirects=True,
        )

        # Verify against the account home rather than trusting the POST body:
        # the response format varies, but /niv always lands on /niv/groups/{id}
        # when the session is good.
        self._confirm_session()

    def _confirm_session(self) -> None:
        resp = self.session.get(
            self.account_home_url,
            headers={"Referer": self.login_url},
            timeout=self.timeout,
            allow_redirects=True,
        )
        landed = Page(resp.text, base_url=resp.url)
        if "/niv/groups/" in resp.url and not landed.form_by_id("sign_in_form"):
            self.landing_url = resp.url
            self._log(f"signed in, landed on {resp.url}")
            return

        # Not signed in: re-read the login page for the actual reason.
        try:
            reason = self._login_error_reason(self._get_page(self.login_url))
        except requests.RequestException:
            reason = f"landed on {resp.url} (HTTP {resp.status_code})"
        raise LoginFailed(f"sign in rejected ({reason})")

    def signed_in(self) -> bool:
        """Cheap session liveness probe."""
        try:
            resp = self.session.get(
                self.account_home_url, timeout=self.timeout, allow_redirects=True
            )
        except requests.RequestException:
            return False
        return "/niv/groups/" in resp.url

    @staticmethod
    def _login_error_reason(page: Page) -> str:
        text = page.text.lower()
        for needle in (
            "invalid email or password",
            "you need to sign in or sign up",
            "your account is locked",
            "too many failed attempts",
        ):
            if needle in text:
                return needle
        if page.captcha_challenge():
            return "reCAPTCHA challenge was rejected"
        return f"unexpected page: {page.title.strip()[:60] or 'no title'}"

    # -- discovery -------------------------------------------------------

    def discover_schedules(self, only_ids: Optional[List[str]] = None) -> List[Schedule]:
        """Enumerate the applications on this account, in page order."""
        if not self.landing_url:
            raise AisError("call login() before discover_schedules()")

        page = self._get_page(self.landing_url)
        found: List[str] = []
        for link in page.links:
            m = SCHEDULE_LINK_RE.search(link.href)
            if m and m.group(1) not in found:
                found.append(m.group(1))

        if not found:
            raise AisError(
                f"no applications found on {self.landing_url}; the landing page "
                "layout may have changed"
            )
        self._log(f"account holds {len(found)} application(s): {', '.join(found)}")

        if only_ids:
            for missing in [i for i in only_ids if i not in found]:
                self._log(
                    f"WARNING: configured schedule_id {missing} is not on this account"
                )
            wanted = [i for i in only_ids if i in found]
        else:
            wanted = found

        # One request per application, spaced out: hammering the site during
        # discovery is the kind of pattern that gets a captcha armed.
        schedules: List[Schedule] = []
        for index, sid in enumerate(wanted):
            if index and self.request_delay:
                time.sleep(self.request_delay)
            schedules.append(self.inspect_schedule(sid))
        return schedules

    def inspect_schedule(self, schedule_id: str) -> Schedule:
        """Classify one application by reading its continue_actions page."""
        page = self._get_page(
            self.continue_actions_url(schedule_id), referer=self.landing_url
        )
        sched = Schedule(id=schedule_id)
        wanted = f"/niv/schedule/{schedule_id}/continue"
        for link in page.links:
            # rstrip/endswith so .../continue_actions is not mistaken for it
            if link.href.split("?")[0].rstrip("/").endswith(wanted):
                sched.continue_url = link.href
                sched.action_label = link.text
                break
        m = CONSULAR_APPT_RE.search(page.text)
        if m:
            sched.current_appointment = m.group(1)
        return sched

    def current_appointment(self, schedule_id: str) -> Optional[str]:
        """The appointment date the site currently shows, if any."""
        try:
            page = self._get_page(self.continue_actions_url(schedule_id))
        except requests.RequestException as exc:
            self._log(f"could not re-read schedule {schedule_id}: {exc}")
            return None
        m = CONSULAR_APPT_RE.search(page.text)
        return m.group(1) if m else None

    # -- availability ----------------------------------------------------

    def get_available_days(
        self, schedule_id: str, facility_id: int
    ) -> Optional[List[date]]:
        """Sorted available days; ``[]`` when fully booked, ``None`` on error."""
        data = self._get_json(
            self.days_url(schedule_id, facility_id),
            referer=self.appointment_url(schedule_id),
        )
        if data is None:
            return None
        if not isinstance(data, list):
            self._log(f"unexpected days payload for facility {facility_id}: {data!r}"[:160])
            return None
        days: List[date] = []
        for item in data:
            raw = item.get("date") if isinstance(item, dict) else None
            if not raw:
                continue
            try:
                days.append(datetime.strptime(raw, "%Y-%m-%d").date())
            except ValueError:
                self._log(f"skipping unparseable date {raw!r}")
        return sorted(days)

    def get_available_times(
        self, schedule_id: str, facility_id: int, day: date
    ) -> Optional[List[str]]:
        """Time slots for one day; ``[]`` when none, ``None`` on error."""
        data = self._get_json(
            self.times_url(schedule_id, facility_id, day),
            referer=self.appointment_url(schedule_id),
        )
        if data is None:
            return None
        if not isinstance(data, dict):
            self._log(f"unexpected times payload: {data!r}"[:160])
            return None
        times = data.get("available_times") or data.get("business_times") or []
        return [str(t) for t in times]

    # -- booking ---------------------------------------------------------

    def open_appointment_page(self, schedule_id: str) -> Page:
        """Reach the appointment form, clearing any interstitial on the way.

        Rescheduling shows a "you may only reschedule N times" consent page
        before the form; it is acknowledged automatically.
        """
        page = self._get_page(
            self.schedule_continue_url(schedule_id),
            referer=self.continue_actions_url(schedule_id),
        )
        for _ in range(3):
            if page.form_by_id("appointment-form") is not None:
                return page
            nxt = self._clear_interstitial(page)
            if nxt is None:
                break
            page = nxt
        raise AisError(f"could not reach appointment-form for schedule {schedule_id}")

    def _clear_interstitial(self, page: Page) -> Optional[Page]:
        """Acknowledge a consent page and return whatever comes next."""
        target = None
        for form in page.forms:
            if form.method != "post" or not form.action:
                continue
            if "sign_out" in form.action or "search" in form.action:
                continue
            target = form
            break
        if target is None:
            self._log(
                f"no appointment form and no consent form on "
                f"{page.title.strip()[:60]!r}"
            )
            return None

        payload = target.payload()
        for f in target.fields:
            if f.type == "checkbox" and f.name:
                payload[f.name] = f.value or "1"
        payload.update(self._solve_captcha(page, target.action))
        self._log("acknowledging interstitial consent page")
        resp = self._post_form(target.action, payload, page)
        return Page(resp.text, base_url=resp.url)

    def book(self, slot: Slot, dry_run: bool = True) -> bool:
        """Submit the appointment form for ``slot``.

        With ``dry_run`` the request is fully assembled and logged but never
        sent, which is what ``test_mode`` uses.
        """
        page = self.open_appointment_page(slot.schedule_id)
        form = page.form_by_id("appointment-form")
        payload = form.payload()

        for key in (FACILITY_KEY, DATE_KEY, TIME_KEY):
            if key not in payload:
                self._log(f"WARNING: {key} absent from the form, adding it anyway")

        payload[FACILITY_KEY] = str(slot.facility_id)
        payload[DATE_KEY] = slot.day.isoformat()

        if not slot.time:
            times = self.get_available_times(slot.schedule_id, slot.facility_id, slot.day)
            if not times:
                self._log(f"no time slots left on {slot.day} at {slot.consulate}")
                return False
            slot.time = times[0]
        payload[TIME_KEY] = slot.time
        payload.setdefault("commit", "Submit")
        payload.update(self._solve_captcha(page, form.action))

        redacted = {k: v for k, v in payload.items() if k != "authenticity_token"}
        self._log(f"booking payload: {json.dumps(redacted, ensure_ascii=False)}")

        if dry_run:
            self._log(
                f"TEST MODE: stopping before submit. Would book {slot} "
                f"for schedule {slot.schedule_id}"
            )
            return False

        resp = self._post_form(form.action, payload, page)
        return self._confirm_booking(slot, resp, Page(resp.text, base_url=resp.url))

    def _confirm_booking(self, slot: Slot, resp, result: Page) -> bool:
        """Decide whether the POST actually moved the appointment."""
        text = result.text.lower()
        if "successfully scheduled" in text or "appointment is confirmed" in text:
            self._log(f"site confirmed the booking: {slot}")
            return True

        # Authoritative check: re-read the application and compare the date.
        confirmed = self.current_appointment(slot.schedule_id)
        if confirmed:
            self._log(f"application now shows appointment: {confirmed}")
            parsed = parse_site_date(confirmed)
            if parsed is None:
                self._log("could not parse the confirmed date; treating as success")
                return True
            if parsed == slot.day:
                return True
            self._log(f"date mismatch: wanted {slot.day}, site shows {parsed}")
            return False

        for needle in ("no longer available", "not available", "invalid", "error"):
            if needle in text:
                self._log(f"booking rejected ({needle}); HTTP {resp.status_code}")
                return False
        self._log(
            f"booking outcome unclear (HTTP {resp.status_code}, landed on "
            f"{resp.url}); treating as failure"
        )
        return False


def parse_site_date(text: str) -> Optional[date]:
    """Parse the site's ``9 January, 2026`` style date into a ``date``."""
    if not text:
        return None
    cleaned = text.replace(",", " ").strip()
    cleaned = " ".join(cleaned.split())
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None
