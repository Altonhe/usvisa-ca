"""Recognising an already-booked application as reschedulable.

The fixtures mirror the live site's markup (structure verified with an
authenticated session, personal details removed):

* continue_actions for a booked application links "Reschedule Appointment"
  to ``.../appointment`` -- not ``.../continue``, which is what the parser used
  to require, so booked applications were reported as having no action.
* "Cancel Appointment" uses the *same* href, distinguished only by
  ``data-method="delete"``.
* The current appointment is on the group page, inside the application card.
* ``.../appointment`` first serves a "Scheduling Limit Warning" GET form.
"""

from datetime import date

import pytest

from app.ais_client import (BASE_URL, AisClient, appointments_by_schedule,
                            find_schedule_action, parse_site_date)
from app.htmlutil import Page

SID = "76189848"

ACTIONS_BOOKED = f"""
<ul class="accordion">
  <li class="accordion-item">
    <a class="accordion-title"><h5>Reschedule Appointment</h5></a>
    <div class="accordion-content"><p>Change the date and/or time.</p>
      <a class="button small primary" href="/en-ca/niv/schedule/{SID}/appointment">Reschedule Appointment</a>
    </div>
  </li>
  <li class="accordion-item">
    <a class="accordion-title"><h5>Cancel Appointment</h5></a>
    <div class="accordion-content"><p>Cancel any existing appointments.</p>
      <a data-method="delete" data-confirm="{{}}" class="button small primary"
         href="/en-ca/niv/schedule/{SID}/appointment">Cancel Appointment</a>
    </div>
  </li>
  <li><a href="/en-ca/niv/schedule/{SID}/appointment/print_instructions">Print Instructions</a></li>
</ul>
"""

ACTIONS_CANCEL_ONLY = f"""
<a data-method="delete" href="/en-ca/niv/schedule/{SID}/appointment">Cancel Appointment</a>
"""

ACTIONS_FIRST_TIME = """
<a href="/en-ca/niv/schedule/72856817/continue">Schedule Appointment</a>
"""

GROUP_PAGE = f"""
<main>
 <div class="application attend_appointment card success">
   <a href="/en-ca/niv/schedule/{SID}/continue_actions">Continue</a>
   <div class="card">
     <p class="consular-appt"><strong>Consular Appointment<span>:</span></strong>
       1 November, 2027, 09:15 Vancouver local time at Vancouver &mdash;
       <a href="/en-ca/niv/schedule/{SID}/addresses/consulate">get directions</a></p>
   </div>
 </div>
 <div class="application card">
   <a href="/en-ca/niv/schedule/72856817/continue_actions">Continue</a>
   <p>No appointment yet.</p>
 </div>
</main>
"""

LIMIT_WARNING = f"""
<h2>Scheduling Limit Warning</h2>
<form action="/en-ca/niv/schedule/{SID}/appointment" method="get">
  <input type="checkbox" name="confirmed_limit_message" value="1">
  <input type="submit" name="commit" value="Continue">
</form>
<form action="/en-ca/niv/search" method="get"><input name="visa_type"></form>
"""

APPOINTMENT_FORM = f"""
<form id="appointment-form" method="post" action="/en-ca/niv/schedule/{SID}/appointment">
  <input type="hidden" name="authenticity_token" value="tok">
  <input type="hidden" name="confirmed_limit_message" value="1">
  <input type="hidden" name="use_consulate_appointment_capacity" value="true">
  <select name="appointments[consulate_appointment][facility_id]"><option value="95" selected>Vancouver</option></select>
  <input type="text" name="appointments[consulate_appointment][date]">
  <select name="appointments[consulate_appointment][time]"></select>
  <input type="submit" name="commit" value="Reschedule">
</form>
"""


def page(html, path="/x"):
    return Page(html, base_url=BASE_URL + path)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_booked_application_is_found_as_reschedulable():
    href, label = find_schedule_action(page(ACTIONS_BOOKED).links, SID)
    assert label == "Reschedule Appointment"
    assert href.endswith(f"/schedule/{SID}/appointment")


def test_cancel_link_is_never_taken_for_the_action():
    """Same href as Reschedule; only data-method="delete" distinguishes it."""
    href, label = find_schedule_action(page(ACTIONS_CANCEL_ONLY).links, SID)
    assert (href, label) == ("", "")


def test_parser_records_the_delete_method():
    cancel = [l for l in page(ACTIONS_BOOKED).links
              if l.text == "Cancel Appointment" and l.href]
    assert cancel and cancel[0].method == "delete"


def test_first_time_detection_is_unchanged():
    href, label = find_schedule_action(page(ACTIONS_FIRST_TIME).links, "72856817")
    assert label == "Schedule Appointment"
    assert href.endswith("/schedule/72856817/continue")


def test_unrelated_appointment_subpages_are_ignored():
    html = f'<a href="/en-ca/niv/schedule/{SID}/appointment/print_instructions">Print</a>'
    assert find_schedule_action(page(html).links, SID) == ("", "")


# ---------------------------------------------------------------------------
# Current appointment, read from the group page
# ---------------------------------------------------------------------------

def test_current_appointment_is_read_from_the_group_card():
    found = appointments_by_schedule(page(GROUP_PAGE).html)
    assert found == {SID: "1 November, 2027"}
    assert parse_site_date(found[SID]) == date(2027, 11, 1)


def test_an_unbooked_card_gets_no_date():
    assert "72856817" not in appointments_by_schedule(page(GROUP_PAGE).html)


def test_dates_are_not_attributed_across_cards():
    """A booked card followed by an unbooked one must not leak its date."""
    swapped = GROUP_PAGE.replace(SID, "AAA").replace("72856817", SID).replace("AAA", "72856817")
    found = appointments_by_schedule(page(swapped).html)
    assert found == {"72856817": "1 November, 2027"}
    assert SID not in found


# ---------------------------------------------------------------------------
# Reaching the form through the limit warning
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, text, url):
        self.text = text
        self.url = url
        self.status_code = 200

    def raise_for_status(self):
        pass


def test_limit_warning_is_acknowledged_by_get_and_reaches_the_form(monkeypatch):
    client = AisClient("a@example.com", "p", logger=lambda m: None)
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        if "confirmed_limit_message=1" in url:
            return Resp(APPOINTMENT_FORM, url)
        if url.endswith(f"/schedule/{SID}/appointment"):
            return Resp(LIMIT_WARNING, url)
        raise AssertionError(f"unexpected GET {url}")

    def no_post(*a, **k):
        raise AssertionError("acknowledging the limit warning must not POST")

    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr(client.session, "post", no_post)

    form_page = client.open_appointment_page(SID)
    assert form_page.form_by_id("appointment-form") is not None
    assert seen[0].endswith(f"/schedule/{SID}/appointment"), "enter via /appointment"
    assert "confirmed_limit_message=1" in seen[1]


def test_the_search_form_is_not_mistaken_for_the_limit_warning(monkeypatch):
    client = AisClient("a@example.com", "p", logger=lambda m: None)
    only_search = '<form action="/en-ca/niv/search" method="get"><input name="q"></form>'
    monkeypatch.setattr(client.session, "get",
                        lambda url, headers=None, timeout=None: Resp(only_search, url))
    monkeypatch.setattr(client.session, "post",
                        lambda *a, **k: pytest.fail("must not POST"))
    with pytest.raises(Exception, match="could not reach appointment-form"):
        client.open_appointment_page(SID)
