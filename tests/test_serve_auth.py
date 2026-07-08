import trilobite_serve as ts


def test_check_auth_open_when_no_key():
    assert ts.check_auth("", "") is True


def test_check_auth_bearer_match():
    assert ts.check_auth("Bearer s3cret", "s3cret") is True


def test_check_auth_raw_match():
    assert ts.check_auth("s3cret", "s3cret") is True


def test_check_auth_wrong_key():
    assert ts.check_auth("Bearer wrong", "s3cret") is False


def test_check_auth_missing_header_when_key_set():
    assert ts.check_auth("", "s3cret") is False


def test_authorized_requires_account_when_flag_set(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda header: None)

    assert ts._authorized("") is False


def test_authorized_accepts_account_when_flag_set(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda header: {"username": "u"})

    assert ts._authorized("Bearer token") is True
