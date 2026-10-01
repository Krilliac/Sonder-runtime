"""Contract tests for the bounded, numbered agent file page renderer."""

from __future__ import annotations

from pathlib import Path
import ast
import io
import os
import re
import textwrap
from types import SimpleNamespace

import pytest

from sonder_runtime.domain.agents.file_page_rendering import (
    normalize_file_page_args,
    render_file_page,
)


def page_data(
    lines: list[str], *, path: str = "src/example.py", start: int = 1,
    total: int | None = None, binary: bool = False, size: int | None = None,
) -> dict:
    total_lines = len(lines) if total is None else total
    return {
        "path": path,
        "start_line": start,
        "end_line": start + len(lines) - 1,
        "total_lines": total_lines,
        "lines": [
            {"line": start + index, "text": text, "characters": len(text)}
            for index, text in enumerate(lines)
        ],
        "bytes": len(lines) if size is None else size,
        "binary": binary,
    }


def test_default_page_numbers_lines_and_reports_next_offset():
    rendered = render_file_page(page_data([f"line {number}" for number in range(1, 121)], total=1000))

    assert rendered.startswith("file src/example.py: lines 1-120 of 1000 (next: offset=121)")
    body = rendered.splitlines()[1:]
    assert len(body) == 120
    assert body[0] == "     1  line 1"
    assert body[-1] == "   120  line 120"


def test_page_at_end_uses_end_of_file_header():
    rendered = render_file_page(page_data(["last"], start=1000, total=1000))

    assert rendered.startswith("file src/example.py: lines 1000-1000 of 1000 (end of file)")
    assert "  1000  last" in rendered


def test_offset_past_end_has_explicit_notice():
    rendered = render_file_page(
        {"path": "empty.py", "total_lines": 1000, "start_line": 1001, "end_line": 1000, "lines": []}
    )

    assert rendered == "file has 1000 lines; offset 1001 is past the end"


def test_empty_file_has_exact_notice():
    assert render_file_page({"path": "empty.py", "total_lines": 0, "lines": []}) == (
        "file empty.py is empty (0 lines)"
    )


def test_long_line_has_length_marker_and_is_hard_cut():
    text = "x" * 3000
    rendered = render_file_page(page_data([text]))

    assert "     1  " + ("x" * 2000) + "...[+1000]" in rendered
    assert len(rendered) <= 6000


def test_crlf_line_count_is_supplied_as_logical_lines():
    rendered = render_file_page(page_data(["one", "two", "three"], path="crlf.py"))

    assert "lines 1-3 of 3" in rendered
    assert rendered.count("\n") == 3


def test_binary_file_is_refused_with_size():
    rendered = render_file_page(
        {"path": "image.bin", "binary": True, "bytes": 4097, "total_lines": 0, "lines": []}
    )

    assert rendered == "file image.bin is binary; refusing to display (4097 bytes)"


def test_renderer_accepts_full_text_line_data_without_characters_field():
    rendered = render_file_page(
        {"path": "plain.txt", "start_line": 4, "end_line": 4, "total_lines": 4,
         "lines": [{"line": 4, "text": "hello"}]}
    )

    assert "     4  hello" in rendered


def test_aliases_normalize_to_guarded_range_arguments():
    assert normalize_file_page_args(
        {"file_path": "a.py", "offset": 9, "limit": 20}
    ) == {"path": "a.py", "start_line": 9, "end_line": 28}
    assert normalize_file_page_args(
        {"filename": "b.py", "start_line": 4, "end_line": 8}
    ) == {"path": "b.py", "start_line": 4, "end_line": 8}


def test_limit_is_bounded_to_four_hundred():
    assert normalize_file_page_args({"path": "a.py", "offset": 2, "limit": 999}) == {
        "path": "a.py", "start_line": 2, "end_line": 401
    }


def test_aliases_are_accepted_by_the_same_renderer_contract():
    args = normalize_file_page_args({"file": "alias.py", "offset": 2, "limit": 2})
    rendered = render_file_page(page_data(["two", "three"], path=args["path"], start=args["start_line"], total=3))

    assert rendered.startswith("file alias.py: lines 2-3 of 3")
    assert "     2  two" in rendered


def test_default_page_for_runtime_adapter_files_is_bounded():
    from sonder_runtime.adapters.inspection_executor import _read_page_data

    root = Path(__file__).parents[1] / "sonder_runtime" / "adapters"
    checked = 0
    for path in root.rglob("*.py"):
        data = _read_page_data(str(path), 1, 120)
        if data["total_lines"] < 200:
            continue
        rendered = render_file_page(data, path=path.relative_to(root).as_posix())
        assert len(rendered) <= 6000, path
        checked += 1
    assert checked > 100, "the measured adapter corpus must actually be exercised"


def test_rendered_output_is_always_bounded_to_six_thousand_characters():
    rendered = render_file_page(page_data(["z" * 2000] * 120, path="wide.py", total=120))

    assert len(rendered) <= 6000


@pytest.mark.parametrize("payload,total", [
    (b"", 0), (b"\n", 1), (b"one\r\ntwo\r\nthree\r\n", 3),
    (b"one\rtwo", 2), (b"one\n\n", 2),
    (b"x" * 63_999 + b"\r\ny", 2),
], ids=["empty", "blank", "crlf", "bare-cr", "trailing-blank", "chunk-boundary"])
def test_streaming_reader_counts_real_line_endings(monkeypatch, payload, total):
    from sonder_runtime.adapters.inspection_executor import _read_page_data

    monkeypatch.setattr(Path, "open", lambda *a, **kw: io.BytesIO(payload))
    data = _read_page_data("source.txt", 1, 120)
    assert data["total_lines"] == total
    assert data["bytes"] == len(payload)
    assert all("\r" not in row["text"] for row in data["lines"])


def test_thousand_line_stream_default_and_past_end(monkeypatch):
    from sonder_runtime.adapters.inspection_executor import _read_page_data

    payload = b"".join(f"line {i}\r\n".encode() for i in range(1, 1001))
    monkeypatch.setattr(Path, "open", lambda *a, **kw: io.BytesIO(payload))
    data = _read_page_data("source.txt", 1, 120)
    output = render_file_page(data)
    assert len(output.splitlines()) == 121
    assert "lines 1-120 of 1000 (next: offset=121)" in output
    assert render_file_page(_read_page_data("source.txt", 1001, 1120)) == (
        "file has 1000 lines; offset 1001 is past the end"
    )


def test_streaming_long_line_and_binary_detection_beyond_requested_page(monkeypatch):
    from sonder_runtime.adapters.inspection_executor import _read_page_data

    payload = ("é" * 3000 + "\r\nnext\r\n").encode()
    monkeypatch.setattr(Path, "open", lambda *a, **kw: io.BytesIO(payload))
    output = render_file_page(_read_page_data("long.txt", 1, 1))
    assert "é" * 2000 + "...[+1000]" in output
    assert "next: offset=2" in output
    payload += b"\x00"
    output = render_file_page(_read_page_data("long.txt", 1, 1))
    assert f"refusing to display ({len(payload)} bytes)" in output
    assert "é" not in output


def _dispatch_file_read_branch():
    """Execute the real dispatch hunk without importing the live server bootstrap."""
    source = (Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8")
    dispatch = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_agent_dispatch")
    branch = next(node for node in dispatch.body if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "tool_name == 'file_read'"
                  and any(isinstance(child, ast.Return) for child in ast.walk(node)))
    tree = ast.parse("def dispatch(tool_name, args):\n" + textwrap.indent(ast.unparse(branch), "    "))
    calls, records, events = [], [], []

    def read(name, args, **credentials):
        calls.append((name, args, credentials))
        return {"path": "repo/source.txt", "bytes": 9, "text": "o", "truncated": True}

    namespace = {
        "_typed_tool": read, "_record_direct_tool": lambda *a, **kw: records.append((a, kw)),
        "activity_tracker": SimpleNamespace(record_event=lambda *a, **kw: events.append((a, kw))),
        "_maybe_live_reload": lambda: None,
    }
    exec(compile(tree, "<file_read dispatch>", "exec"), namespace)
    return namespace["dispatch"], calls, records, events


@pytest.mark.parametrize("path_key", ["path", "file", "filename", "file_path"])
@pytest.mark.parametrize("range_args", [{"offset": 2, "limit": 1}, {"start_line": 2, "end_line": 2}])
def test_alias_args_dispatch_to_same_guarded_renderer(monkeypatch, path_key, range_args):
    from sonder_runtime.adapters.filesystem import file_ops

    dispatch, calls, records, events = _dispatch_file_read_branch()
    monkeypatch.setattr(Path, "open", lambda *a, **kw: io.BytesIO(b"one\r\ntwo\r\nthree"))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: Path("repo"))
    token, approval = object(), object()
    output = dispatch("file_read", {path_key: "source.txt", **range_args,
                                   "token": token, "approval": approval, "extra_roots": "allowed"})
    assert output == "file source.txt: lines 2-2 of 3 (next: offset=3)\n     2  two"
    assert calls == [("file_read", {"path": "source.txt", "max_bytes": 1},
                      {"token": token, "approval": approval, "extra_roots": "allowed"})]
    assert len(records) == len(events) == 1


def test_failed_typed_authorization_never_opens_file(monkeypatch):
    from sonder_runtime.adapters.inspection_executor import render_agent_file_page

    def refuse(*args, **kwargs):
        raise PermissionError("guarded path")

    def unexpected(*args, **kwargs):
        pytest.fail("read or activity happened after typed authorization refusal")

    monkeypatch.setattr(Path, "open", unexpected)
    output = render_agent_file_page(
        {"path": "blocked.txt"}, read=refuse, record=lambda *a, **kw: None,
        activity=SimpleNamespace(record_event=unexpected), reload=lambda: None,
    )
    assert output == "ERROR: guarded path"


def test_mcp_raw_read_format_keeps_metadata_bytes_and_crlf():
    from sonder_runtime.adapters.inspection_executor import _format_file_result

    data = {"path": "a.txt", "bytes": 10, "truncated": True, "text": "one\r\ntwo"}
    expected = "file read\n  path: a.txt\n  bytes: 10\n  truncated: True\n\none\r\ntwo"
    assert _format_file_result("file read", data).encode() == expected.encode()


def test_canonical_page_knobs_win_over_aliases_and_alias_range_is_capped():
    assert normalize_file_page_args({"path": "a", "offset": 3, "limit": 2,
                                     "start_line": 7, "end_line": 20}) == {
        "path": "a", "start_line": 3, "end_line": 4,
    }
    assert normalize_file_page_args({"path": "a", "start_line": 2, "end_line": 999}) == {
        "path": "a", "start_line": 2, "end_line": 401,
    }


@pytest.mark.parametrize("args", [{"path": "a", "offset": 0}, {"path": "a", "limit": -1},
                                  {"path": "a", "start_line": 3, "end_line": 2}, {}])
def test_invalid_page_arguments_fail_explicitly(args):
    with pytest.raises(ValueError):
        normalize_file_page_args(args)


def test_file_range_uses_same_renderer_without_changing_raw_read(monkeypatch):
    from sonder_runtime.adapters.inspection_executor import _format_file_result
    from sonder_runtime.adapters.filesystem import file_ops

    monkeypatch.setattr(Path, "open", lambda *a, **kw: io.BytesIO(b"one\r\ntwo\r\nthree"))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: Path("repo"))
    assert _format_file_result("file range", {"path": "repo/source.txt", "start_line": 2,
                                              "end_line": 2}) == (
        "file source.txt: lines 2-2 of 3 (next: offset=3)\n     2  two"
    )


@pytest.mark.parametrize("alias", ["file", "filename", "file_path"])
def test_dispatch_policies_and_project_scoping_receive_canonical_alias_path(monkeypatch, alias):
    from sonder_runtime.bootstrap import prepared_workbench

    source = (Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8")
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_agent_dispatch", "_project_scope_args"}]
    namespace = {
        "os": os, "re": re, "_require_managed_agent_admission": lambda: None,
        "unsafe_lab": SimpleNamespace(active=lambda: False),
        "_canonical_agent_tool_name": lambda name: name,
        "_AGENT_SYSTEM_OPERATOR_TOOLS": set(),
        "_agent_permission_gate_error": lambda name, args: dict(args),
        "_PROJECT_SCOPED_PATH_TOOLS": {"file_read"}, "_PROJECT_SCOPED_EXECUTION_TOOLS": set(),
        "_project_scoped_path_key": lambda name: "path",
    }
    monkeypatch.setattr(prepared_workbench, "require_prepared_tool", lambda name: None)
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<real dispatch and scope>", "exec"), namespace)
    proposed = {alias: "src/example.py"}
    policy_args = namespace["_agent_dispatch"]("file_read", proposed, read_only=True)
    assert policy_args["path"] == "src/example.py"
    assert "path" not in proposed
    scoped = namespace["_project_scope_args"]("file_read", proposed, os.path.abspath("project"))
    assert scoped["path"] == os.path.join(os.path.abspath("project"), "src/example.py")
    assert scoped["extra_roots"] == os.path.abspath("project")
