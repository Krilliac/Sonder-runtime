"""MCP runtime rendering lives in the domain; the root names stay compatible."""
import server
from sonder_runtime.domain import mcp_runtime_formatting as rendering


def _state(**overrides):
    data = {
        "status": "ok", "enabled": True, "registered_tools": 208, "refresh_count": 4,
        "last_surface_changed": True, "protocol_list_changed": True, "path": "/srv/server.py",
        "loaded_digest": "abcdef0123456789ff", "current_digest": "abcdef0123456789ff",
    }
    data.update(overrides)
    return data


def test_root_error_reducer_is_an_identity_preserving_alias():
    assert server._safe_mcp_error is rendering.safe_mcp_error


def test_refresh_errors_reduce_to_content_free_lines():
    known = "stale runtime source: loaded MCP file is unavailable"
    assert rendering.safe_mcp_error(known) == known
    assert rendering.safe_mcp_error("configured runtime root is unavailable") == (
        "configured runtime root is unavailable"
    )
    assert rendering.safe_mcp_error("ValueError: /home/op/secret.py exploded") == (
        "ValueError: source refresh failed"
    )
    assert rendering.safe_mcp_error("PermissionError") == "PermissionError: source refresh failed"
    assert rendering.safe_mcp_error("something else entirely") == "runtime source refresh failed"
    assert rendering.safe_mcp_error("") == "runtime source refresh failed"
    assert rendering.safe_mcp_error(None) == "runtime source refresh failed"


def test_the_status_block_renders_every_line_in_order():
    text = rendering.format_mcp_runtime(_state(), recovery_action=lambda _p: "unused")
    assert text.splitlines() == [
        "sonder MCP runtime",
        "  status: ok | live source refresh: on",
        "  tools: 208 | atomic refreshes: 4 | last surface changed: yes",
        "  MCP tool-list updates: advertised",
        "  source registration: available",
        "  loaded/current: abcdef012345 / abcdef012345",
    ]
    bare = rendering.format_mcp_runtime({}, recovery_action=lambda _p: "")
    assert bare.splitlines() == [
        "sonder MCP runtime",
        "  status: unknown | live source refresh: off",
        "  tools: 0 | atomic refreshes: 0 | last surface changed: no",
        "  MCP tool-list updates: not advertised",
        "  source registration: unknown",
        "  loaded/current: unknown / unknown",
    ]


def test_provenance_and_refresh_failures_use_the_injected_action_and_safe_error():
    seen = []

    def action(provenance):
        seen.append(provenance)
        return "restart Sonder from the configured runtime root"

    provenance = {
        "pid": 42, "python": "/usr/bin/python3", "cwd": "(deleted or unavailable)",
        "source_root_exists": False, "configured_root_exists": True,
        "issue": "loaded MCP source does not match configured runtime root",
    }
    text = rendering.format_mcp_runtime(
        _state(
            provenance=provenance, last_refresh_ts=1700000000,
            last_error="RuntimeError: /tmp/secret failed", last_notification_error="x",
        ),
        recovery_action=action,
    )
    lines = text.splitlines()
    assert "  process: pid=42 | python=python" in lines
    assert "  process cwd: unavailable" in lines
    assert "  source root: missing" in lines
    assert "  configured runtime root: present" in lines
    assert "  provenance ERROR: loaded MCP source does not match configured runtime root" in lines
    assert "  ACTION: restart Sonder from the configured runtime root" in lines
    assert "  last refresh unix time: 1700000000" in lines
    assert "  ERROR: RuntimeError: source refresh failed (last known-good registry remains active)" in lines
    assert lines[-1] == "  notification warning: MCP list-change notification failed"
    assert seen == [provenance]


def test_root_wrapper_collects_live_data_and_injects_the_recovery_action(monkeypatch):
    monkeypatch.setattr(server, "mcp_runtime_data", lambda: _state(status="live"))
    assert "  status: live | live source refresh: on" in server.format_mcp_runtime()
    monkeypatch.setattr(server, "_safe_mcp_recovery_action", lambda _p: "act now")
    assert "  ACTION: act now" in server.format_mcp_runtime(_state(provenance={"issue": "stale"}))
