"""Parser tests using markup captured from the live AIS site."""

from datetime import date

from app.ais_client import (CONSULAR_APPT_RE, DATE_KEY, FACILITY_KEY, TIME_KEY,
                           SCHEDULE_LINK_RE, parse_site_date)
from app.htmlutil import Page

# Trimmed from the real /en-ca/niv/users/sign_in response.
LOGIN_HTML = """<html><head><title>Sign in</title>
<meta name="csrf-param" content="authenticity_token">
<meta name="csrf-token" content="ABC123token">
</head><body>
<form class="simple_form new_user" id="sign_in_form" novalidate="novalidate"
      action="/en-ca/niv/users/sign_in" accept-charset="UTF-8" data-remote="true" method="post">
  <input class="string email required" type="email" value="" name="user[email]" id="user_email">
  <input class="password optional" type="password" name="user[password]" id="user_password">
  <div class="icheckbox icheck-item"><input type="checkbox" name="policy_confirmed"
       id="policy_confirmed" value="1" class="icheck-input"></div>
  <div class="captcha_container"><img alt="Captcha logo" width="60px" src="/assets/hcaptcha_logo.png"></div>
  <p><input type="submit" name="commit" value="Sign In" class="button primary"></p>
  <p><a data-method="get" href="/en-ca/niv/forgot">Forgot your password?</a></p>
</form>
<script>var leak = "must not appear in text";</script>
</body></html>"""

BASE = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"


def test_login_form_fields():
    page = Page(LOGIN_HTML, base_url=BASE)
    form = page.form_by_id("sign_in_form")
    assert form is not None
    assert form.action == BASE
    assert form.remote == "true"

    payload = form.payload()
    assert payload["user[email]"] == ""
    assert "user[password]" in payload
    assert payload["commit"] == "Sign In"
    # An unchecked checkbox must not be submitted; we add policy_confirmed by hand.
    assert "policy_confirmed" not in payload


def test_csrf_token_comes_from_meta_only():
    page = Page(LOGIN_HTML, base_url=BASE)
    assert page.csrf_token() == "ABC123token"
    assert page.metas["csrf-param"] == "authenticity_token"
    form = page.form_by_id("sign_in_form")
    hidden = [f for f in form.fields if f.type == "hidden"]
    assert hidden == []


def test_script_text_is_excluded_from_page_text():
    page = Page(LOGIN_HTML, base_url=BASE)
    assert "must not appear" not in page.text
    assert "Forgot your password?" in page.text


def test_captcha_absent_on_clean_load():
    # This is what the live page returns: an empty container, no sitekey.
    assert Page(LOGIN_HTML, base_url=BASE).captcha_challenge() is None


def test_captcha_v3_detected_when_armed():
    armed = LOGIN_HTML.replace(
        '<div class="captcha_container">',
        '<div class="captcha_container" id="captcha_box" data-sitekey="6LxxxxxxxxxxKEY" '
        'data-badge="bottomright" data-size="invisible">',
    ).replace(
        "<p><input type=\"submit\"",
        '<input type="hidden" name="g-recaptcha-response" id="g-recaptcha-response" '
        'data-action="login" value=""><p><input type="submit"',
    )
    challenge = Page(armed, base_url=BASE).captcha_challenge()
    assert challenge == {
        "sitekey": "6LxxxxxxxxxxKEY",
        "action": "login",
        "version": 3,
        "invisible": True,
        "response_field": "g-recaptcha-response",
    }


def test_captcha_v2_when_no_action():
    armed = LOGIN_HTML.replace(
        '<div class="captcha_container">',
        '<div class="captcha_container" data-sitekey="6LKEYv2">',
    )
    challenge = Page(armed, base_url=BASE).captcha_challenge()
    assert challenge["version"] == 2
    assert challenge["action"] == ""


# Trimmed from the real /en-ca/niv/schedule/{id}/appointment response.
APPT_HTML = """<html><head><meta name="csrf-token" content="TOK"></head><body>
<form id="appointment-form" action="/en-ca/niv/schedule/72856817/appointment" method="post">
 <input name="authenticity_token" type="hidden" value="AUTHTOK">
 <input name="confirmed_limit_message" type="hidden" value="1" id="confirmed_limit_message">
 <input name="use_consulate_appointment_capacity" type="hidden" value="true"
        id="use_consulate_appointment_capacity">
 <fieldset><ol>
  <li id="appointments_consulate_appointment_facility_id_input">
   <select name="appointments[consulate_appointment][facility_id]"
           id="appointments_consulate_appointment_facility_id" class="required">
     <option value=""></option><option value="94">Toronto</option>
     <option value="95">Vancouver</option></select></li>
  <li id="appointments_consulate_appointment_date_input" class="yatri_date input required stringish">
   <input name="appointments[consulate_appointment][date]" type="text"
          id="appointments_consulate_appointment_date" class="required hasDatepicker"></li>
  <li id="appointments_consulate_appointment_time_input">
   <select name="appointments[consulate_appointment][time]"
           id="appointments_consulate_appointment_time" class="required">
     <option value=""></option></select></li>
 </ol></fieldset>
 <div><fieldset><ol><li id="appointments_submit_action">
  <input name="commit" type="submit" id="appointments_submit" value="Reschedule" class="button primary">
 </li></ol></fieldset></div>
</form>
<p>Consular Appointment: 9 January, 2026, 11:15</p>
</body></html>"""


def test_appointment_form_payload_has_every_required_field():
    page = Page(APPT_HTML, base_url="https://ais.usvisa-info.com/x")
    form = page.form_by_id("appointment-form")
    payload = form.payload()
    for key in (
        "authenticity_token",
        "confirmed_limit_message",
        "use_consulate_appointment_capacity",
        FACILITY_KEY, DATE_KEY, TIME_KEY, "commit",
    ):
        assert key in payload, key
    assert payload["authenticity_token"] == "AUTHTOK"
    assert payload["commit"] == "Reschedule"


def test_date_input_wrapper_and_inner_input_both_resolve():
    page = Page(APPT_HTML, base_url="https://ais.usvisa-info.com/x")
    assert page.find_field("appointments_consulate_appointment_date") is not None
    assert page.find_field("appointments_consulate_appointment_facility_id") is not None


def test_consular_appointment_regex_and_date_parsing():
    page = Page(APPT_HTML, base_url="https://ais.usvisa-info.com/x")
    m = CONSULAR_APPT_RE.search(page.text)
    assert m and m.group(1) == "9 January, 2026"
    assert parse_site_date(m.group(1)) == date(2026, 1, 9)


def test_parse_site_date_handles_variants():
    assert parse_site_date("9 January, 2026") == date(2026, 1, 9)
    assert parse_site_date("09 Jan 2026") == date(2026, 1, 9)
    assert parse_site_date("garbage") is None
    assert parse_site_date("") is None


CONTINUE_ACTIONS_HTML = """<html><body>
<a href="/en-ca/niv/schedule/72856817/continue_actions">English</a>
<a href="/en-ca/niv/schedule/72856817/continue">Schedule Appointment</a>
<a href="/en-ca/niv/schedule/72856817/courier/edit">Update Delivery Location</a>
</body></html>"""


def test_continue_link_does_not_match_continue_actions():
    page = Page(
        CONTINUE_ACTIONS_HTML,
        base_url="https://ais.usvisa-info.com/en-ca/niv/schedule/72856817/continue_actions",
    )
    wanted = "/niv/schedule/72856817/continue"
    hits = [l for l in page.links if l.href.split("?")[0].rstrip("/").endswith(wanted)]
    assert len(hits) == 1
    assert hits[0].text == "Schedule Appointment"


GROUP_HTML = """<html><body>
<a href="/en-ca/niv/schedule/67078262/continue_actions">Continue</a>
<a href="/en-ca/niv/schedule/72856817/continue_actions">Continue</a>
<a href="/en-ca/niv/schedule/72856546/continue_actions">Continue</a>
<a href="/en-ca/niv/schedule/68324220/continue_actions">Continue</a>
<a href="/en-ca/niv/schedule/67078262/applicants/79769982">Details</a>
</body></html>"""


def test_group_page_yields_every_schedule_in_order():
    page = Page(GROUP_HTML, base_url="https://ais.usvisa-info.com/en-ca/niv/groups/47515017")
    found = []
    for link in page.links:
        m = SCHEDULE_LINK_RE.search(link.href)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    # The old implementation only ever saw the first of these.
    assert found == ["67078262", "72856817", "72856546", "68324220"]
