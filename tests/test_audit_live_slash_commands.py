"""Unit contracts for the authenticated bounded live slash audit."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_live_slash_commands.py"
_SPEC = importlib.util.spec_from_file_location("audit_live_slash_commands", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_safe_invocations_omit_optional_parameters():
    row = {"name": "/directory_digest", "params": [
        {"name": "root", "type": "str", "required": True},
        {"name": "max_bytes", "type": "int", "required": False},
    ]}
    line = _MODULE.invocation(row, r"C:\temp\audit", False,
                              r"C:\temp\audit\fixture")
    assert line == r"/directory_digest root=C:\temp\audit"


def test_stateful_invocations_are_bounded_and_json_safe():
    row = {"name": "/autopilot_start", "params": [
        {"name": "objective", "type": "str", "required": True},
        {"name": "max_cycles", "type": "int", "required": False},
        {"name": "wait", "type": "bool", "required": False},
    ]}
    line = _MODULE.invocation(row, r"C:\temp\audit", True)
    assert "objective=slash-audit" in line
    assert "max_cycles=1" in line
    assert "wait=false" in line


def test_classification_distinguishes_auth_and_model_fallthrough():
    assert _MODULE.classify(401, "") == "auth_failure"
    assert _MODULE.classify(200, '{"choices":[{"message":{"content":"model calls: 1"}}]}') == "model_fallthrough"
    assert _MODULE.classify(200, '{"choices":[{"message":{"content":"ok"}}]}') == "handled"
