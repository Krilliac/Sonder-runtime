"""Focused contract tests for model tool-argument normalization."""
import pytest
from sonder_runtime.domain.agents.tool_args import (
    normalize_decision_aliases,
    normalize_tool_args,
)


READ = {"properties": {"path": {"type": "string"}, "query": {"type": "string"}}}
RUN = {"properties": {
    "program": {"type": "string"}, "args": {"type": "array"},
    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
    "check": {"type": "boolean"},
}}


def test_stringified_json_and_arguments_envelope_are_recovered():
    args, notes = normalize_tool_args(
        None, '{"tool":"file_read","arguments":{"path":"a"}}', READ
    )
    assert args == {"path": "a"}
    assert any("envelope" in note for note in notes)


def test_aliases_and_schema_unknowns_are_normalized():
    args, notes = normalize_tool_args(
        "file_edit",
        {"filename": "a.py", "pattern": "needle", "ignored": 1},
        {"properties": {"path": {}, "query": {}}},
    )
    assert args == {"path": "a.py", "query": "needle"}
    assert any("ignored" in note for note in notes)


def test_command_alias_and_scalar_coercions_are_bounded():
    args, notes = normalize_tool_args(
        "workspace_run",
        {"command": "python -m pytest -q", "timeout_seconds": 100, "check": "false"},
        RUN,
    )
    assert args["program"] == "python"
    assert args["args"] == ["-m", "pytest", "-q"]
    assert args["timeout_seconds"] == 30
    assert args["check"] is False
    assert any("clamped" in note for note in notes)


def test_content_fence_is_stripped_once():
    args, notes = normalize_tool_args(
        "file_write", {"body": "```python\ndef f():\n    return 1\n```"},
        {"properties": {"content": {"type": "string"}}},
    )
    assert args["content"] == "def f():\n    return 1"
    assert any("outer fence" in note for note in notes)


def test_normalization_is_idempotent():
    schema = {"properties": {"path": {}, "timeout": {"minimum": 1, "maximum": 4}}}
    first, _ = normalize_tool_args("file_read", {"file": "a", "timeout": 99}, schema)
    second, notes = normalize_tool_args("file_read", first, schema)
    assert second == first == {"path": "a", "timeout": 4}
    assert notes == []


def test_plain_mapping_without_schema_preserves_unknowns():
    args, notes = normalize_tool_args("tool", {"custom": 1, "text": "hello"})
    assert args == {"custom": 1, "text": "hello"}
    assert notes == []


def test_ambiguous_text_alias_follows_tool_kind_without_schema():
    query, _ = normalize_tool_args("text_search", {"text": "needle"})
    content, _ = normalize_tool_args("file_write", {"text": "body"})
    assert query == {"query": "needle"}
    assert content == {"content": "body"}


def test_authority_fields_survive_normalization_for_host_policy():
    args, notes = normalize_tool_args(
        "file_read",
        {"path": "a", "token": "model-secret", "approval": "trusted", "extra_roots": "C:/"},
        READ,
    )
    assert args["path"] == "a"
    assert args["token"] == "model-secret"
    assert args["approval"] == "trusted"
    assert args["extra_roots"] == "C:/"
    assert all("dropped unknown argument token" not in note for note in notes)


def test_normalization_does_not_create_authority_or_execution_grants():
    args, _ = normalize_tool_args(
        "file_read", {"filename": "a", "cmd": "python -c pass"}, READ
    )
    assert args["path"] == "a"
    assert "approval" not in args
    assert "extra_roots" not in args
    assert "program" not in args


def test_boolean_coercion_is_limited_to_boolean_schema_fields():
    args, _ = normalize_tool_args(
        "file_write", {"content": "false", "label": "false", "check": "false"},
        {"properties": {
            "content": {"type": "string"}, "label": {"type": "string"},
            "check": {"type": "boolean"},
        }},
    )
    assert args == {"content": "false", "label": "false", "check": False}


def test_schema_declared_alias_names_are_not_rewritten():
    args, _ = normalize_tool_args(
        "repo_blame", {"file_path": "a.py", "pattern": "symbol"},
        {"properties": {"file_path": {}, "pattern": {}}},
    )
    assert args == {"file_path": "a.py", "pattern": "symbol"}


def test_command_quotes_preserve_windows_backslashes_and_argv():
    args, _ = normalize_tool_args(
        "workspace_run",
        {"command": r'python "C:\\Program Files\\suite\\run.py" --name "a b"'},
        RUN,
    )
    assert args == {
        "program": "python",
        "args": [r"C:\\Program Files\\suite\\run.py", "--name", "a b"],
    }


def test_decision_alias_helper_is_pure_and_preserves_final():
    decision = {"name": "file_read", "arguments": {"path": "a"}, "final": "keep"}
    out = normalize_decision_aliases(decision)
    assert out == {"tool": "file_read", "args": {"path": "a"}, "final": "keep"}
    assert decision == {"name": "file_read", "arguments": {"path": "a"}, "final": "keep"}


def test_decision_name_without_arguments_is_not_reinterpreted():
    decision = {"name": "file_read", "final": "keep"}
    assert normalize_decision_aliases(decision) == decision


@pytest.mark.parametrize(
    ("tool", "raw", "schema"),
    [
        ("file_read", {"file": "a"}, READ),
        ("text_search", {"pattern": "needle"}, {"properties": {"query": {}}}),
        ("file_write", {"content": "```\nfalse\n```"}, {"properties": {"content": {"type": "string"}}}),
        ("workspace_run", {"command": 'python "a b.py"'}, RUN),
        ("workspace_run", {"args": {"cmd": "python -m pytest", "check": "false"}}, RUN),
    ],
)
def test_normalization_is_idempotent_across_alias_envelopes(tool, raw, schema):
    first, _ = normalize_tool_args(tool, raw, schema)
    second, notes = normalize_tool_args(tool, first, schema)
    assert second == first
    assert notes == []


def test_nested_fences_are_not_consumed_by_a_second_pass():
    raw = {"content": "```python\n```js\nvalue\n```\n```"}
    schema = {"properties": {"content": {"type": "string"}}}
    first, _ = normalize_tool_args("file_write", raw, schema)
    second, notes = normalize_tool_args("file_write", first, schema)
    assert second == first == raw
    assert notes == []


def test_workspace_args_alias_remains_supported_by_dispatch_schema():
    import server
    from sonder_runtime.bootstrap.agent_help_tools import argument_schema
    schema = argument_schema(server.mcp, "workspace_run")
    result, _ = normalize_tool_args("workspace_run", {"cmd": "python -m pytest -q"}, schema)
    assert result == {"program": "python", "args": ["-m", "pytest", "-q"]}


def test_empty_quoted_argument_and_legitimate_nested_args_survive():
    result, _ = normalize_tool_args("workspace_run", {"command": 'python -c ""'}, RUN)
    assert result["args"] == ["-c", ""]
    schema = {"properties": {"path": {}, "args": {"type": "object"}}}
    raw = {"path": "a", "args": {"custom": "data"}}
    assert normalize_tool_args("example", raw, schema)[0] == raw


@pytest.mark.parametrize("raw", ["not json", "[]", 42, None])
def test_invalid_argument_payloads_remain_invalid_for_dispatch(raw):
    result, notes = normalize_tool_args("file_read", raw, READ)
    assert not isinstance(result, dict)
    assert notes


def test_agent_loop_recovers_arguments_before_dispatch(monkeypatch):
    import server
    decisions = iter(['{"tool":"file_read","arguments":{"filename":"a.py","unknown":1}}',
                      '{"final":"done"}'])
    calls = []
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return next(decisions)

    generate.last_response_meta = {}
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: ("fixture", False, False, "code"))
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: generate)
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda tool, args, **kw: calls.append((tool, args)) or "file content")
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    server._agent_turn("Read a.py", max_steps=2, allow_web=False)
    assert calls == [("file_read", {"path": "a.py"})]
    assert "argument normalization: dropped unknown argument unknown" in prompts[1]


@pytest.mark.parametrize("boundary", ["server", "packaged"])
def test_name_arguments_alias_survives_structural_validation(boundary):
    """The decision parser must keep A8's alternate envelope through validation.

    Both generators must canonicalize {"name", "arguments"} before the
    structural checks, which read "tool" and would otherwise reject the
    decision as having neither "tool" nor "final".
    """
    import server
    from sonder_runtime.adapters.agent_decision_generation import generate_decision
    raw = '{"name":"file_read","arguments":{"path":"a.py"}}'
    if boundary == "server":
        decision, original, error = server._agent_generate_decision(lambda _: raw, "read", repair_limit=0)
    else:
        decision, original, error = generate_decision(lambda _: raw, "read", repair_limit=0, write_chunk_hint=1000)
    assert error is None
    assert decision == {"tool": "file_read", "args": {"path": "a.py"}}
    assert original == raw
