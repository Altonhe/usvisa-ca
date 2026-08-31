from datetime import date

import pytest

from app.config import ConfigError, load_config

BASE = """
test_mode: true
poll_interval: 300
defaults:
  consulates: [TRT]
  earliest_acceptable_date: 2026-09-01
  latest_acceptable_date: 2026-12-30
accounts:
  - name: Primary
    email: a@example.com
    password: secret
    applications:
      - schedule_id: "72856817"
        label: Applicant A
        consulates: [TRT, VAN]
        latest_acceptable_date: 2026-11-30
      - schedule_id: "72856546"
        label: Applicant B
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_three_level_cascade(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert len(cfg.accounts) == 1
    acc = cfg.accounts[0]
    assert acc.name == "Primary"
    assert len(acc.applications) == 2

    a, b = acc.applications
    # Application overrides both consulates and the latest date.
    assert a.target.consulates == [94, 95]
    assert a.target.latest == date(2026, 11, 30)
    assert a.target.earliest == date(2026, 9, 1)   # inherited from defaults
    # Application B inherits everything.
    assert b.target.consulates == [94]
    assert b.target.latest == date(2026, 12, 30)


def test_totals_and_windows(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.total_applications() == 2
    a = cfg.accounts[0].applications[0]
    assert a.target.describe_consulates() == "Toronto, Vancouver"
    assert a.target.describe_window() == "2026-09-01 .. 2026-11-30"
    assert a.display_name == "Applicant A (72856817)"


def test_target_accepts_and_exclusions(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    target = cfg.accounts[0].applications[1].target
    assert target.accepts(date(2026, 10, 1))
    assert not target.accepts(date(2026, 8, 31))     # before earliest
    assert not target.accepts(date(2027, 1, 1))      # after latest


def test_exclusions_block_dates(tmp_path):
    text = BASE.replace(
        "  latest_acceptable_date: 2026-12-30\n",
        "  latest_acceptable_date: 2026-12-30\n"
        "  exclusions:\n"
        "    - [2026-10-01, 2026-10-15]\n"
        "    - {from: 2026-11-20, to: 2026-11-25}\n",
        1,
    )
    cfg = load_config(write(tmp_path, text))
    target = cfg.accounts[0].applications[1].target
    assert len(target.exclusions) == 2
    assert target.excluded_by(date(2026, 10, 5)) == (date(2026, 10, 1), date(2026, 10, 15))
    assert target.excluded_by(date(2026, 11, 22)) == (date(2026, 11, 20), date(2026, 11, 25))
    assert target.excluded_by(date(2026, 12, 1)) is None
    assert not target.accepts(date(2026, 10, 5))
    assert target.accepts(date(2026, 12, 1))


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "env@example.com")
    monkeypatch.setenv("MY_PASS", "envsecret")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    text = """
capsolver_api_key: ${MISSING_TOKEN:-}
accounts:
  - name: Env
    email: ${MY_EMAIL}
    password: ${MY_PASS}
"""
    cfg = load_config(write(tmp_path, text))
    assert cfg.accounts[0].email == "env@example.com"
    assert cfg.accounts[0].password == "envsecret"
    assert cfg.capsolver_api_key == ""
    assert cfg.capsolver_enabled is False


def test_env_interpolation_default_value(tmp_path, monkeypatch):
    monkeypatch.delenv("POLL", raising=False)
    text = """
poll_interval: ${POLL:-600}
accounts:
  - {name: X, email: x@example.com, password: p}
"""
    assert load_config(write(tmp_path, text)).poll_interval == 600


def test_auto_discover_when_no_applications(tmp_path):
    text = """
accounts:
  - name: Auto
    email: auto@example.com
    password: p
"""
    cfg = load_config(write(tmp_path, text))
    assert cfg.accounts[0].auto_discover is True
    # Default consulate seed is Toronto only, not all seven.
    assert cfg.accounts[0].target.consulates == [94]


def test_missing_password_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    text = """
accounts:
  - name: Bad
    email: bad@example.com
    password: ${NOPE}
"""
    with pytest.raises(ConfigError, match="password is empty"):
        load_config(write(tmp_path, text))


def test_non_numeric_schedule_id_is_rejected(tmp_path):
    text = """
accounts:
  - name: Bad
    email: b@example.com
    password: p
    applications:
      - schedule_id: "not-a-number"
"""
    with pytest.raises(ConfigError, match="schedule_id"):
        load_config(write(tmp_path, text))


def test_unknown_consulate_is_rejected(tmp_path):
    text = """
accounts:
  - name: Bad
    email: b@example.com
    password: p
    consulates: [Winnipeg]
"""
    with pytest.raises(ConfigError, match="consulates"):
        load_config(write(tmp_path, text))


def test_inverted_date_window_is_rejected(tmp_path):
    text = """
defaults:
  earliest_acceptable_date: 2026-12-30
  latest_acceptable_date: 2026-09-01
accounts:
  - {name: X, email: x@example.com, password: p}
"""
    with pytest.raises(ConfigError, match="is after"):
        load_config(write(tmp_path, text))


def test_duplicate_account_email_is_rejected(tmp_path):
    text = """
accounts:
  - {name: One, email: same@example.com, password: p}
  - {name: Two, email: SAME@example.com, password: p}
"""
    with pytest.raises(ConfigError, match="more than once"):
        load_config(write(tmp_path, text))


def test_no_accounts_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="at least one account"):
        load_config(write(tmp_path, "test_mode: true\n"))


def test_missing_file_gives_actionable_error(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_dashboard_auth_flag(tmp_path):
    text = """
dashboard:
  username: admin
  password: hunter2
accounts:
  - {name: X, email: x@example.com, password: p}
"""
    cfg = load_config(write(tmp_path, text))
    assert cfg.dashboard.auth_enabled is True
    assert load_config(write(tmp_path, """
accounts:
  - {name: X, email: x@example.com, password: p}
""")).dashboard.auth_enabled is False


def test_shipped_example_config_is_valid(tmp_path):
    """config.example.yaml must parse once its blank secrets are filled in."""
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = example.read_text(encoding="utf-8").replace(
        'password: ""', 'password: "filled-in"'
    )
    cfg = load_config(write(tmp_path, text))
    assert len(cfg.accounts) == 1
    assert cfg.total_applications() == 2
    assert cfg.test_mode is True
    assert cfg.accounts[0].applications[0].target.consulates == [94, 95]
    assert cfg.accounts[0].applications[1].target.consulates == [94]
    assert cfg.store_file == cfg.data_dir / "store.json"


def test_shipped_live_config_is_valid(tmp_path):
    """config.yaml (gitignored, may not exist) must parse when it does."""
    from pathlib import Path
    live = Path(__file__).resolve().parent.parent / "config.yaml"
    if not live.exists():
        pytest.skip("config.yaml not present")
    text = live.read_text(encoding="utf-8").replace(
        'password: ""', 'password: "filled-in"'
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.accounts
    assert cfg.total_applications() >= 1


def test_example_config_declares_no_env_placeholders():
    """The example must be self-contained: no ${VAR} required to read it."""
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    body = "\n".join(
        line for line in example.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    assert "${" not in body
