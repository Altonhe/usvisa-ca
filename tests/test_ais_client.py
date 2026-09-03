"""Session persistence: cookies + landing_url survive a process restart."""

from app.ais_client import AisClient


def make_client():
    return AisClient("a@example.com", "p", logger=lambda m: None)


def test_save_then_load_restores_cookies_and_landing_url(tmp_path):
    path = tmp_path / "sessions" / "primary.json"

    client = make_client()
    client.session.cookies.set("_yatri_session", "abc123", domain="ais.usvisa-info.com")
    client.landing_url = "https://ais.usvisa-info.com/en-ca/niv/groups/47515017"
    client.save_cookies(path)
    assert path.exists()

    fresh = make_client()
    assert fresh.load_cookies(path) is True
    assert fresh.session.cookies.get("_yatri_session") == "abc123"
    assert fresh.landing_url == "https://ais.usvisa-info.com/en-ca/niv/groups/47515017"


def test_load_cookies_missing_file_returns_false(tmp_path):
    client = make_client()
    assert client.load_cookies(tmp_path / "sessions" / "nope.json") is False
    assert client.landing_url == ""


def test_load_cookies_corrupt_file_returns_false(tmp_path):
    path = tmp_path / "sessions" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json at all", encoding="utf-8")
    client = make_client()
    assert client.load_cookies(path) is False


def test_load_cookies_empty_cookie_map_returns_false(tmp_path):
    import json
    path = tmp_path / "sessions" / "empty.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cookies": {}, "landing_url": "x"}), encoding="utf-8")
    client = make_client()
    assert client.load_cookies(path) is False


def test_resume_session_succeeds_when_still_signed_in(monkeypatch):
    client = make_client()

    class Landed:
        status_code = 200
        text = "<html><body>account home</body></html>"
        url = "https://ais.usvisa-info.com/en-ca/niv/groups/47515017"

    monkeypatch.setattr(client.session, "get", lambda *a, **k: Landed())
    assert client.resume_session() is True
    assert client.landing_url == Landed.url


def test_resume_session_fails_when_session_has_lapsed(monkeypatch):
    client = make_client()

    class SignInPage:
        status_code = 200
        text = (
            '<html><body><form id="sign_in_form" action="/en-ca/niv/users/sign_in" '
            'method="post"></form></body></html>'
        )
        url = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"

    monkeypatch.setattr(client.session, "get", lambda *a, **k: SignInPage())
    assert client.resume_session() is False
    assert client.landing_url == ""


def test_resume_session_false_on_network_error(monkeypatch):
    import requests

    client = make_client()

    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(client.session, "get", boom)
    assert client.resume_session() is False
