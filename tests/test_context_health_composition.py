"""Architecture and compatibility ratchets for the context-health slice."""
from __future__ import annotations

import ast
from pathlib import Path

from sonder_runtime.application.context_health import ContextHealthService


ROOT = Path(__file__).parents[1]


def test_context_health_application_is_root_free_and_port_driven():
    path = ROOT / "sonder_runtime" / "application" / "context_health.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    assert not any(
        isinstance(node, ast.Import) and any(a.name == "server" for a in node.names)
        or isinstance(node, ast.ImportFrom) and node.module == "server"
        for node in ast.walk(tree)
    )
    assert ContextHealthService.__module__ == "sonder_runtime.application.context_health"


def test_server_keeps_the_legacy_route_and_delegates_snapshot_computation():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("def context_health_data(")
    end = source.index("\ndef context_health(", start)
    slice_source = source[start:end]
    assert "ContextHealthService(" in slice_source
    assert "_resolve_session" in slice_source
    assert "_open_db()" in slice_source
    assert "ContextMemorySnapshot" in slice_source
