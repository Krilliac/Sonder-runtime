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


def test_native_invocations_use_positional_branch_syntax():
    row = {"name": "/artifactcheck", "native": True, "params": [
        {"name": "path", "type": "str", "required": True},
    ]}
    line = _MODULE.invocation(row, r"C:\temp\audit", False,
                              r"C:\temp\audit\fixture")
    assert line.startswith("/artifactcheck ")
    assert "path=" not in line


def test_classification_distinguishes_auth_and_model_fallthrough():
    assert _MODULE.classify(401, "") == "auth_failure"
    assert _MODULE.classify(200, '{"choices":[{"message":{"content":"model calls: 1"}}]}') == "model_fallthrough"
    assert _MODULE.classify(200, '{"choices":[{"message":{"content":"ok"}}]}') == "handled"


def _reply(text):
    import json
    return json.dumps({"choices": [{"message": {"content": text}}]})


def test_handler_failures_are_the_dispatcher_error_forms():
    for text in (
        "file_read failed: OSError: disk gone",
        "/file_read failed: boom",
        "loop is catalogued but not callable here.",
        "report\nTraceback (most recent call last):\n  File \"x\", line 1",
    ):
        assert _MODULE.classify(200, _reply(text)) == "handler_failure", text


def test_a_report_that_merely_mentions_failure_words_is_handled():
    """``/system_profile_text`` prints standing instructions that say "when
    /run reports ... a traceback"; the word in prose is not a failed handler."""
    text = (
        "profile: /home/user/Sonder-runtime/system_profile.md\n"
        "- When `/run` reports a timeout, missing output, or a traceback, "
        "diagnose that\n"
        "- a build that failed: fix it before claiming done\n"
    )
    assert _MODULE.classify(200, _reply(text)) == "handled"
