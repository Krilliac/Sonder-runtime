"""A token-less loopback launcher stays allowed but warns loudly at startup."""
from __future__ import annotations

import pytest

import sonder_launcher as L


class _FakeServer:
    def __init__(self, address, handler, *, controller, token):
        self.token = token
        self.socket = None

    def serve_forever(self):
        return None


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr(L, "LauncherServer", _FakeServer)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_tokenless_loopback_start_is_allowed_with_warning(fake_server, capsys, host):
    L.serve(host, 0, "", controller=object())
    err = capsys.readouterr().err
    assert "WITHOUT authentication" in err
    assert "SONDER_LAUNCHER_TOKEN" in err


def test_token_configured_start_has_no_warning(fake_server, capsys):
    L.serve("127.0.0.1", 0, "t" * 32, controller=object())
    assert "WITHOUT authentication" not in capsys.readouterr().err


def test_tokenless_lan_binding_is_still_refused():
    with pytest.raises(ValueError, match="requires SONDER_LAUNCHER_TOKEN"):
        L.validate_configuration("192.168.1.5", "")
