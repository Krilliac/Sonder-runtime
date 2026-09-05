"""Agent call signatures live in the adapters layer; the root name is a delegate."""
import json
import os

import server
from sonder_runtime.adapters import agent_call_signature


def _signature(tool_name, args):
    return agent_call_signature.call_signature(
        tool_name, args,
        project_scoped_path_tools=server._PROJECT_SCOPED_PATH_TOOLS,
        project_scoped_path_key=server._project_scoped_path_key,
    )


def test_root_delegate_matches_the_packaged_signature(tmp_path):
    args = {"path": str(tmp_path / "src" / "..")}
    assert server._agent_call_signature("file_read", args) == _signature("file_read", args)


def test_equivalent_paths_collapse_to_one_signature(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    first = _signature("file_read", {"path": str(project / "src" / "..")})
    second = _signature("file_read", {"path": str(project)})
    assert first == second
    assert first[0] == "file_read"
    assert json.loads(first[1])["path"] == os.path.normcase(str(project.resolve()))
    assert first != _signature("file_read", {"path": str(project / "src")})


def test_tool_specific_keys_and_non_dict_args_are_handled(tmp_path):
    convert = _signature("data_convert", {
        "input_path": str(tmp_path / "a.csv"), "output_path": str(tmp_path / "sub" / ".." / "b.json"),
    })
    assert json.loads(convert[1])["output_path"] == os.path.normcase(str((tmp_path / "b.json").resolve()))
    run_a = _signature("workspace_run", {"cwd": str(tmp_path / "."), "program": "x"})
    run_b = _signature("workspace_run", {"cwd": str(tmp_path), "program": "x"})
    assert run_a == run_b
    assert _signature("status", None) == ("status", "null")
    assert _signature("status", ["a"]) == ("status", '["a"]')


def test_archive_create_inputs_are_resolved_against_the_root(tmp_path):
    name, payload = _signature("archive_create", {
        "root": str(tmp_path), "destination": "out/x.zip",
        "inputs_json": json.dumps(["a.txt", str(tmp_path / "b.txt")]),
    })
    decoded = json.loads(payload)
    assert name == "archive_create"
    assert decoded["root"] == os.path.normcase(str(tmp_path.resolve()))
    assert decoded["destination"] == os.path.normcase(str((tmp_path / "out" / "x.zip").resolve()))
    assert decoded["inputs_json"] == [os.path.normcase(str((tmp_path / "a.txt").resolve())), os.path.normcase(str((tmp_path / "b.txt").resolve()))]
    assert "inputs" not in decoded
