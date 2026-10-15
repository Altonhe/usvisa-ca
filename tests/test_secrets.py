from pathlib import Path

import pytest

from app import secrets as sec
from app.config import ConfigError, load_config


class FakeClient:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    def fetch_all(self):
        self.calls += 1
        return dict(self.values)


def test_secret_key_normalises_names():
    assert sec.secret_key("Primary", "email") == "PRIMARY_EMAIL"
    assert sec.secret_key("Canary-Alton", "password") == "CANARY_ALTON_PASSWORD"
    with pytest.raises(sec.SecretsError):
        sec.secret_key("--", "email")


def test_fill_only_missing_fields_inline_wins():
    accounts = [
        {"name": "Primary"},
        {"name": "Canary-LHK", "email": "inline@example.com"},
    ]
    client = FakeClient({
        "PRIMARY_EMAIL": "p@example.com", "PRIMARY_PASSWORD": "pw1",
        "CANARY_LHK_EMAIL": "remote@example.com", "CANARY_LHK_PASSWORD": "pw2",
    })
    sec.fill_account_credentials(accounts, {"project_id": "x"}, client)
    assert accounts[0] == {"name": "Primary", "email": "p@example.com", "password": "pw1"}
    assert accounts[1]["email"] == "inline@example.com"
    assert accounts[1]["password"] == "pw2"


def test_no_fetch_when_nothing_missing():
    client = FakeClient({})
    sec.fill_account_credentials([{"name": "A", "email": "a", "password": "b"}],
                                 {"project_id": "x"}, client)
    assert client.calls == 0


def test_missing_client_credentials_fail_loudly(monkeypatch):
    monkeypatch.delenv("INFISICAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_CLIENT_SECRET", raising=False)
    client = sec.InfisicalClient(sec.InfisicalConfig.from_raw({"project_id": "x"}))
    with pytest.raises(sec.SecretsError, match="INFISICAL_CLIENT_ID"):
        client.fetch_all()


def test_load_config_fills_from_infisical(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sec.InfisicalClient, "fetch_all",
                        lambda self: {"PRIMARY_EMAIL": "p@example.com",
                                      "PRIMARY_PASSWORD": "pw"})
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "secrets:\n  provider: infisical\n  project_id: proj\n"
        "accounts:\n  - name: Primary\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.accounts[0].email == "p@example.com"
    assert cfg.accounts[0].password == "pw"


def test_load_config_reports_missing_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sec.InfisicalClient, "fetch_all", lambda self: {})
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "secrets:\n  project_id: proj\naccounts:\n  - name: Primary\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="email is required"):
        load_config(cfg_file)


def test_unreachable_infisical_is_a_config_error(tmp_path: Path, monkeypatch):
    def boom(self):
        raise sec.SecretsError("Infisical unreachable: nope")
    monkeypatch.setattr(sec.InfisicalClient, "fetch_all", boom)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "secrets:\n  project_id: proj\naccounts:\n  - name: Primary\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unreachable"):
        load_config(cfg_file)
