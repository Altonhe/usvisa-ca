"""Request deduplication driven by the server's own ETags.

The topology asserted here is not invented: it was read off the live host with
an authenticated session. Two B2 applications on one account answer with one
identical ETag at Toronto, while two applications of a different class on the
same account answer with a different one. That is the fact the planner exists
to exploit.
"""

from app.calendars import CalendarCache, PollPlan

# ETags shortened from the real ones observed against ais.usvisa-info.com.
B2_TRT = 'W/"604aa90315c8ca4e3fa894aa08a678fe"'
OTHER_TRT = 'W/"6532a9c6ef636fe16526a0d2728c5472"'
B2_VAN = 'W/"61be0be140c1daedce9b0940d2b59220"'

PRIMARY_TRT = [("72856817", 94), ("72856546", 94), ("68324220", 94), ("67078262", 94)]


def _learn_real_topology(cache):
    cache.observe("72856817", 94, B2_TRT)
    cache.observe("72856546", 94, B2_TRT)
    cache.observe("68324220", 94, OTHER_TRT)
    cache.observe("67078262", 94, OTHER_TRT)


def test_nothing_is_shared_before_any_etag_is_seen():
    """Sharing must only ever follow evidence, never precede it."""
    cache = CalendarCache()
    plans = cache.plan(PRIMARY_TRT, sweep=1)
    assert len(plans) == 4
    assert all(not p.shared for p in plans)


def test_identical_etags_collapse_into_one_request_per_class():
    cache = CalendarCache(reverify_sweeps=10)
    _learn_real_topology(cache)

    plans = cache.plan(PRIMARY_TRT, sweep=2)
    assert len(plans) == 2, "four applications, two visa classes, two requests"
    assert sum(p.saved_requests for p in plans) == 2

    covered = {sid for p in plans for sid in p.covers}
    assert covered == {"72856817", "72856546", "68324220", "67078262"}
    # Each class keeps its own members together.
    groups = sorted(sorted(p.covers) for p in plans)
    assert groups == [["67078262", "68324220"], ["72856546", "72856817"]]


def test_a_different_facility_is_never_merged_with_another():
    """Same application, two consulates: two distinct calendars, two requests."""
    cache = CalendarCache()
    cache.observe("72856817", 94, B2_TRT)
    cache.observe("72856817", 95, B2_VAN)
    plans = cache.plan([("72856817", 94), ("72856817", 95)], sweep=2)
    assert len(plans) == 2
    assert {p.facility_id for p in plans} == {94, 95}


def test_reverify_sweep_polls_everything_individually():
    """The safety valve: periodically re-earn the right to share."""
    cache = CalendarCache(reverify_sweeps=10)
    _learn_real_topology(cache)
    assert len(cache.plan(PRIMARY_TRT, sweep=9)) == 2
    assert cache.is_reverify_sweep(10)
    assert len(cache.plan(PRIMARY_TRT, sweep=10)) == 4
    assert len(cache.plan(PRIMARY_TRT, sweep=11)) == 2


def test_a_diverging_member_splits_itself_back_out():
    """If a calendar stops matching its class, it stops being shared."""
    cache = CalendarCache(reverify_sweeps=10)
    _learn_real_topology(cache)
    cache.observe("67078262", 94, 'W/"something-else"')

    plans = cache.plan(PRIMARY_TRT, sweep=11)
    sizes = sorted(len(p.covers) for p in plans)
    assert sizes == [1, 1, 2], "the pair stays paired, the outlier goes alone"
    lone = {p.schedule_id for p in plans if not p.shared}
    assert "67078262" in lone and "68324220" in lone


def test_a_failed_poll_does_not_destroy_learned_grouping():
    """Errors are transient; tearing down classes on each blip would thrash."""
    cache = CalendarCache()
    cache.observe("72856817", 94, B2_TRT)
    cache.observe("72856817", 94, None)
    assert cache.known_etag("72856817", 94) == B2_TRT


def test_sharing_can_be_switched_off_entirely():
    cache = CalendarCache(enabled=False)
    _learn_real_topology(cache)
    plans = cache.plan(PRIMARY_TRT, sweep=2)
    assert len(plans) == 4
    assert all(not p.shared for p in plans)
    assert not cache.is_reverify_sweep(10)


def test_duplicate_pairs_are_requested_once():
    cache = CalendarCache()
    plans = cache.plan([("111", 94), ("111", 94)], sweep=1)
    assert len(plans) == 1


def test_forget_drops_one_application_but_keeps_the_others():
    cache = CalendarCache()
    _learn_real_topology(cache)
    cache.forget("72856817")
    assert cache.known_etag("72856817", 94) is None
    assert cache.known_etag("72856546", 94) == B2_TRT


def test_summary_reports_what_was_saved():
    cache = CalendarCache()
    _learn_real_topology(cache)
    plans = cache.plan(PRIMARY_TRT, sweep=2)
    text = CalendarCache.summarise(plans, len(PRIMARY_TRT))
    assert "2 availability request(s) instead of 4" in text
    assert "2 shared" in text
    # Nothing shared reads as a plain count, not a boast about zero savings.
    plain = CalendarCache.summarise([PollPlan("1", 94)], 1)
    assert plain == "1 availability request(s)"
