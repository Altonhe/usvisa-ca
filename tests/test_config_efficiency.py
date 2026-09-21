"""The documented example config must actually load, with the new keys.

A config file that documents options the loader ignores is worse than no
documentation, so this parses the real config.example.yaml rather than a
hand-rolled fixture.
"""

from pathlib import Path

import pytest

from app.config import ConfigError, Config, load_config, _as_hour_windows

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


@pytest.fixture
def example(tmp_path):
    """A loadable copy of the shipped example, with credentials filled in."""
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace("you@example.com", "someone@example.com")
    text = text.replace('password: ""', 'password: "secret"')
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_example_config_loads(example):
    config = load_config(example)
    assert config.accounts, "the example should define at least one account"


def test_every_documented_efficiency_option_is_actually_read(example):
    """Each key documented in the example must reach the Config object."""
    config = load_config(example)
    assert config.discovery_interval == 900
    assert config.max_concurrent_requests == 3
    assert config.calendar_sharing is True
    assert config.calendar_reverify_sweeps == 10
    assert config.booking_prewarm_ttl == 120
    assert config.speculative_booking is False
    assert config.fast_poll_interval == 0
    assert config.active_hours == []
    assert config.done_confirmations == 5


def test_defaults_match_the_example_so_the_docs_do_not_drift():
    """A bare Config must agree with what the example file states."""
    bare = Config(accounts=[], telegram=None, dashboard=None)
    assert bare.discovery_interval == 900
    assert bare.max_concurrent_requests == 3
    assert bare.calendar_sharing is True
    assert bare.calendar_reverify_sweeps == 10
    assert bare.booking_prewarm_ttl == 120
    assert bare.speculative_booking is False
    assert bare.fast_poll_interval == 0
    assert bare.active_hours == []


def test_concurrency_is_never_below_one(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "max_concurrent_requests: 0\n"
        "accounts:\n"
        "  - name: A\n"
        "    email: a@b.c\n"
        "    password: pw\n",
        encoding="utf-8",
    )
    assert load_config(path).max_concurrent_requests == 1


def test_reverify_interval_is_never_below_one(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "calendar_reverify_sweeps: 0\n"
        "accounts:\n"
        "  - name: A\n"
        "    email: a@b.c\n"
        "    password: pw\n",
        encoding="utf-8",
    )
    assert load_config(path).calendar_reverify_sweeps == 1


# ---------------------------------------------------------------------------
# active_hours parsing
# ---------------------------------------------------------------------------

def test_active_hours_accepts_the_three_documented_shapes():
    assert _as_hour_windows([[9, 17], [20, 22]], "x") == [(9, 17), (20, 22)]
    assert _as_hour_windows("9-17, 20-22", "x") == [(9, 17), (20, 22)]
    assert _as_hour_windows([{"from": 9, "to": 17}], "x") == [(9, 17)]
    assert _as_hour_windows(None, "x") == []


def test_active_hours_allows_a_window_that_wraps_midnight():
    assert _as_hour_windows([[22, 6]], "x") == [(22, 6)]


@pytest.mark.parametrize("bad", [[[9]], [[1, 2, 3]], ["nonsense"], [[9, 99]]])
def test_active_hours_rejects_nonsense_loudly(bad):
    with pytest.raises(ConfigError):
        _as_hour_windows(bad, "active_hours")


def test_active_hours_rejects_an_empty_window():
    """start == end would select no time at all, which is never intended."""
    with pytest.raises(ConfigError, match="no time at all"):
        _as_hour_windows([[9, 9]], "active_hours")
