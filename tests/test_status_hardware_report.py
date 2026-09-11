import pytest

import server


def test_cached_status_never_probes_hardware(monkeypatch):
    monkeypatch.setattr(server.sonder_hardware, "get_profile", lambda **kwargs: pytest.fail("live hardware probe"))
    result = server.status()
    assert "unknown" in result
    assert "whole-worker" in result
