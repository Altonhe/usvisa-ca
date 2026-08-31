"""Entry-point behaviour: config layering, placeholder detection, --check."""

from pathlib import Path

import pytest

from app.config import load_config
from app.main import main, warn_about_placeholders

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def write(tmp_path, text, name="config.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


REAL = """
dashboard:
  username: admin
  password: a-real-password
accounts:
  - name: Primary
    email: real@example.com
    password: a-real-secret
    applications:
      - schedule_id: "72856817"
"""


def test_no_warnings_for_real_values(tmp_path):
    assert warn_about_placeholders(load_config(write(tmp_path, REAL))) == []


def test_placeholder_email_and_password_are_flagged(tmp_path):
    text = REAL.replace("real@example.com", "you@example.com").replace(
        "a-real-secret", "REPLACE_ME"
    )
    warnings = warn_about_placeholders(load_config(write(tmp_path, text)))
    assert any("email" in w for w in warnings)
    assert any("password" in w for w in warnings)


def test_placeholder_dashboard_password_is_flagged(tmp_path):
    text = REAL.replace("password: a-real-password", "password: change-me-before-exposing")
    warnings = warn_about_placeholders(load_config(write(tmp_path, text)))
    assert any("dashboard.password" in w for w in warnings)


def test_placeholder_detection_is_case_insensitive(tmp_path):
    text = REAL.replace("a-real-secret", "Replace_Me")
    assert warn_about_placeholders(load_config(write(tmp_path, text)))


def test_shipped_example_is_all_placeholders(tmp_path):
    """The example must not look configured, or a user could run it unedited."""
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'password: ""', 'password: "x"'
    )
    warnings = warn_about_placeholders(load_config(write(tmp_path, text)))
    assert any("email" in w for w in warnings)


def test_check_succeeds_on_a_real_config(tmp_path, capsys):
    path = write(tmp_path, REAL)
    assert main(["--config", str(path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "configuration is valid" in out
    # The summary must show what the user asked to see.
    assert "Primary" in out
    assert "72856817" in out


def test_check_reports_placeholders_and_fails(tmp_path, capsys):
    text = REAL.replace("a-real-secret", "REPLACE_ME")
    path = write(tmp_path, text)
    assert main(["--config", str(path), "--check"]) == 1
    out = capsys.readouterr().out
    assert "placeholder" in out


def test_check_rejects_a_broken_config(tmp_path, capsys):
    path = write(tmp_path, "accounts: []\n")
    assert main(["--config", str(path), "--check"]) == 2
    assert "configuration error" in capsys.readouterr().out


def test_check_reports_a_missing_file(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.yaml"), "--check"]) == 2
    assert "config.example.yaml" in capsys.readouterr().out


def test_store_path_derives_from_data_dir(tmp_path):
    text = REAL + "\ndata_dir: /var/lib/usvisa\n"
    cfg = load_config(write(tmp_path, text))
    assert cfg.store_file == Path("/var/lib/usvisa/store.json")


def test_obsolete_state_file_key_is_reported(tmp_path, capsys):
    text = REAL + '\nstate_file: data/state.json\n'
    load_config(write(tmp_path, text))
    assert "obsolete" in capsys.readouterr().out


def test_test_telegram_without_config_exits_2(tmp_path, capsys):
    path = write(tmp_path, REAL)
    assert main(["--config", str(path), "--test-telegram"]) == 2
    assert "Telegram is not configured" in capsys.readouterr().out
