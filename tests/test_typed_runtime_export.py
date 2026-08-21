from __future__ import annotations

from sonder_runtime.__main__ import _export_runtime_environment
from sonder_runtime.platform.config import ServerConfig, SonderConfig


def test_canonical_export_leaves_typed_http_values_out_of_environment(monkeypatch):
    monkeypatch.setenv("SONDER_HOST", "poisoned-host")
    monkeypatch.setenv("SONDER_PORT", "9999")
    monkeypatch.setenv("SONDER_MAX_REQUEST_BYTES", "17")

    _export_runtime_environment(
        SonderConfig(server=ServerConfig(host="127.0.0.1", port=11435)),
        include_typed_runtime=False,
    )

    assert __import__("os").environ["SONDER_HOST"] == "poisoned-host"
    assert __import__("os").environ["SONDER_PORT"] == "9999"
    assert __import__("os").environ["SONDER_MAX_REQUEST_BYTES"] == "17"
