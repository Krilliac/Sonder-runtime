import server

from sonder_runtime.domain.request_timeout import bound_request_timeout


def test_server_retains_live_ceiling_compatibility_wrapper():
    assert server._bound_request_timeout is bound_request_timeout


def test_request_timeout_defaults_and_clamps_to_positive_ceiling():
    assert bound_request_timeout(None, 300) == 300
    assert bound_request_timeout(" 17 ", 300) == 17
    assert bound_request_timeout(0, 300) == 1
    assert bound_request_timeout(-4, 300) == 1
    assert bound_request_timeout(999, 300) == 300


def test_request_timeout_uses_ceiling_for_invalid_values():
    assert bound_request_timeout("not-a-number", 45) == 45
    assert bound_request_timeout(object(), 45) == 45


def test_root_wrapper_tracks_runtime_timeout_ceiling(monkeypatch):
    monkeypatch.setattr(server, "TIMEOUT", 23)
    assert server._bounded_timeout(None) == 23
    assert server._bounded_timeout(99) == 23
    assert server._bounded_timeout(0) == 1
