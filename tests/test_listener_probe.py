from sonder_runtime.adapters.web import listener_probe


def test_listener_probe_normalizes_wildcard_bind(monkeypatch):
    calls = []

    def fake_create_connection(address, timeout):
        calls.append((address, timeout))
        return _Connected()

    class _Connected:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(listener_probe.socket, "create_connection", fake_create_connection)
    assert listener_probe.port_open("0.0.0.0", 11435)
    assert calls == [(("127.0.0.1", 11435), 0.5)]
