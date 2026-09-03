"""Agent activity command rendering lives in the domain; root names are aliases."""
import json

import server
from sonder_runtime.domain.agents import activity_command


def test_root_names_are_identity_preserving_aliases():
    assert server._activity_argv is activity_command.activity_argv
    assert server._batch_agent_operations is activity_command.batch_operations
    assert server._agent_argv is activity_command.agent_argv
    assert server._agent_activity_command is activity_command.activity_command


def test_argv_normalization_keeps_secret_bearing_items_separate():
    assert activity_command.activity_argv('["--token", "abc"]') == ["--token", "abc"]
    assert activity_command.activity_argv("not json") == "not json"
    assert activity_command.activity_argv('{"a": 1}') == '{"a": 1}'
    assert activity_command.activity_argv(["already", "list"]) == ["already", "list"]
    assert activity_command.agent_argv({"args_json": '["-v", 3]'}) == ["-v", "3"]
    assert activity_command.agent_argv({"args_json": "plain"}) == ["plain"]
    assert activity_command.agent_argv({"args": None}) == []
    assert activity_command.agent_argv({}) == []


def test_batch_operations_accept_json_or_lists_and_refuse_other_shapes():
    ops = [{"path": "a.txt"}, {"path": "b.txt"}]
    assert activity_command.batch_operations({"operations_json": json.dumps(ops)}) == ops
    assert activity_command.batch_operations({"operations": ops}) == ops
    assert activity_command.batch_operations({"operations_json": "nope"}) is None
    assert activity_command.batch_operations({"operations": {"path": "x"}}) is None
    assert activity_command.batch_operations({}) == []


def test_activity_command_renders_each_tool_family_without_serializing_argv_twice():
    render = activity_command.activity_command
    assert render("file_batch_write", {"operations": [{"path": "a"}, "junk", {"path": "b"}]}) == '["a", "b"]'
    assert render("workspace_compare", {"left": "x", "right": "y"}) == "x | y"
    assert render("data_convert", {"input_path": "in.csv", "output_path": "out.json"}) == "in.csv -> out.json"
    assert render("workspace_run", {"program": "make", "args_json": '["-j", "4"]'}) == 'make ["-j", "4"]'
    assert render("script_run", {"path": "run.sh", "args": ["--fast"]}) == 'run.sh ["--fast"]'
    assert render("file_copy", {"source": "a", "destination": "b"}) == "a -> b"
    assert render("local_service_probe", {"method": "post", "url": "http://localhost:1"}) == "POST http://localhost:1"
    assert render("process_memory_risk_inspect", {"pid": 7}) == "pid=7"
    assert render("process_list", {}) == "max_processes=128"
    assert render("file_read", {"path": "notes.md"}) == "notes.md"
    assert render("text_search", {"root": "src", "query": "x"}) == "src"
    assert render("memory_search", {"query": "cache"}) == "query=cache"
    assert render("status", None) == ""
