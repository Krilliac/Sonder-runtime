from __future__ import annotations

import io

from sonder_runtime.interfaces.cli.headless import HeadlessCliOperations, run


def _operations(**overrides):
    values = dict(
        status=lambda host, port: f"status {host}:{port}",
        stop_pid=lambda name, host, port: f"{name} stopped",
        stop_succeeded=lambda message: True,
        start_succeeded=lambda message: True,
        start_ollama=lambda: "ollama ready",
        ensure_sonder_alias=lambda: (True, "alias ready"),
        validate_start=lambda host, env: None,
        start_sonder=lambda host, port, env: "sonder: started pid=7",
        managed_listener_pid=lambda host, port: 7,
        wait_until=lambda fn, seconds: fn(),
    )
    values.update(overrides)
    return HeadlessCliOperations(**values)


def test_packaged_headless_interface_owns_parser_and_sequences_callbacks():
    output = io.StringIO()
    calls = []
    operations = _operations(
        start_ollama=lambda: calls.append("ollama") or "ollama ready",
        ensure_sonder_alias=lambda: calls.append("alias") or (True, "alias ready"),
        start_sonder=lambda host, port, env: calls.append((host, port, env)) or "started",
    )

    assert run(["start", "--host", "127.0.0.2", "--port", "12000", "--context-size", "8"], operations, out=output) == 0
    assert calls == ["ollama", "alias", ("127.0.0.2", 12000, {"SONDER_CONTEXT_SIZE": "8"})]
    assert "status 127.0.0.2:12000" in output.getvalue()


def test_packaged_headless_interface_preserves_stop_ollama_port_contract():
    seen = []
    operations = _operations(stop_pid=lambda *args: seen.append(args) or "ok")

    assert run(["stop", "--host", "127.0.0.1", "--port", "12000", "--stop-ollama"], operations) == 0
    assert seen == [("sonder_serve", "127.0.0.1", 12000), ("ollama",)]
