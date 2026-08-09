import os
from pathlib import Path

import pytest

import activity_tracker
import file_ops
import server
import symbol_index


@pytest.fixture
def project(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _index(project, **kwargs):
    return symbol_index.index_repository(str(project), **kwargs)


def test_python_ast_index_is_deterministic_and_qualified(project):
    (project / "b.py").write_text("def later():\n    pass\n", encoding="utf-8")
    (project / "a.py").write_text(
        "class Example:\n"
        "    async def work(self):\n"
        "        def nested():\n"
        "            pass\n"
        "\n"
        "def top():\n"
        "    pass\n",
        encoding="utf-8",
    )

    first = _index(project, glob="**/*.py")
    second = _index(project, glob="**/*.py")

    assert first == second
    assert [row["path"] for row in first["symbols"]] == [
        "a.py", "a.py", "a.py", "a.py", "b.py",
    ]
    assert [(row["kind"], row["name"]) for row in first["symbols"]] == [
        ("class", "Example"),
        ("async_method", "Example.work"),
        ("function", "Example.work.nested"),
        ("function", "top"),
        ("function", "later"),
    ]


@pytest.mark.parametrize(
    ("filename", "source", "language", "expected"),
    [
        ("a.js", "export function run() {}\nconst arrow = () => 1;\n", "javascript", {"run", "arrow"}),
        ("a.ts", "interface Shape {}\nexport type Id = string;\n", "typescript", {"Shape", "Id"}),
        ("a.c", "struct Point { int x; };\nint add(int a, int b);\n", "c", {"Point", "add"}),
        ("a.cpp", "namespace demo {}\nclass Widget {};\nint demo::run(int x) {\n", "cpp", {"demo", "Widget", "demo::run"}),
        ("a.cs", "public class Worker {}\npublic static int Run(int x) {\n", "csharp", {"Worker", "Run"}),
        ("a.rs", "pub struct Item;\npub async fn load() {}\nmacro_rules! make { () => {} }\n", "rust", {"Item", "load", "make"}),
        ("a.go", "type Item struct{}\nfunc (i Item) Run() {}\n", "go", {"Item", "Run"}),
    ],
)
def test_conservative_language_extractors(project, filename, source, language, expected):
    (project / filename).write_text(source, encoding="utf-8")

    data = _index(project, language=language)

    assert {row["name"] for row in data["symbols"]} == expected
    assert {row["language"] for row in data["symbols"]} == {language}


def test_glob_and_language_filters_are_both_enforced(project):
    (project / "keep.py").write_text("def keep(): pass\n", encoding="utf-8")
    (project / "skip.py").write_text("def skip(): pass\n", encoding="utf-8")
    (project / "keep.js").write_text("function wrong() {}\n", encoding="utf-8")

    data = _index(project, glob="keep.*", language="python")

    assert [row["name"] for row in data["symbols"]] == ["keep"]
    assert data["files"] == 1


def test_hard_caps_clamp_caller_values(project):
    (project / "a.py").write_text("def one(): pass\n", encoding="utf-8")

    data = _index(
        project, max_files=10**9, max_total_bytes=10**9,
        max_file_bytes=10**9, max_symbols=10**9,
    )

    assert data["limits"] == {
        "max_files": symbol_index.HARD_MAX_FILES,
        "max_total_bytes": symbol_index.HARD_MAX_TOTAL_BYTES,
        "max_file_bytes": symbol_index.HARD_MAX_FILE_BYTES,
        "max_symbols": symbol_index.HARD_MAX_SYMBOLS,
    }


def test_file_symbol_and_total_byte_truncation_are_explicit(project):
    for name in ("a.py", "b.py", "c.py"):
        (project / name).write_text("def one(): pass\ndef two(): pass\n", encoding="utf-8")

    files = _index(project, max_files=1)
    symbols = _index(project, max_symbols=1)
    total = _index(project, max_total_bytes=1)

    assert files["truncation_reasons"] == ["max_files"]
    assert symbols["truncation_reasons"] == ["max_symbols"]
    assert total["truncation_reasons"] == ["max_total_bytes"]
    assert "truncated: yes (max_files)" in symbol_index.format_index(files)


def test_per_file_cap_and_malformed_files_report_errors_without_stopping(project):
    (project / "a_large.py").write_text("def large(): pass\n" * 20, encoding="utf-8")
    (project / "b_bad.py").write_text("def broken(:\n", encoding="utf-8")
    (project / "c_binary.py").write_bytes(b"def nope():\n\xff")
    (project / "d_good.py").write_text("def good(): pass\n", encoding="utf-8")

    data = _index(project, max_file_bytes=64)

    errors = {row["path"]: row["error"] for row in data["errors"]}
    assert "exceeds max_file_bytes" in errors["a_large.py"]
    assert "syntax error" in errors["b_bad.py"]
    assert "invalid UTF-8" in errors["c_binary.py"]
    assert [row["name"] for row in data["symbols"]] == ["good"]


def test_containment_sensitive_paths_and_foreign_absolute_forms(project, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def escaped(): pass\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "hidden.py").write_text("def hidden(): pass\n", encoding="utf-8")
    (project / "visible.py").write_text("def visible(): pass\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside every authorized root"):
        symbol_index.index_repository(str(outside))
    with pytest.raises(PermissionError, match="secret or control state"):
        symbol_index.index_repository(str(project / ".git" / "hidden.py"))
    foreign = "C:\\outside\\repo" if os.name != "nt" else "/outside/repo"
    with pytest.raises(PermissionError, match="non-native absolute"):
        symbol_index.index_repository(foreign)

    data = _index(project)
    assert [row["name"] for row in data["symbols"]] == ["visible"]


def test_symlink_roots_are_rejected_and_tree_links_are_not_followed(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.py").write_text("def escaped(): pass\n", encoding="utf-8")
    linked = project / "linked"
    root_link = tmp_path / "root-link"
    try:
        linked.symlink_to(outside, target_is_directory=True)
        root_link.symlink_to(project, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    assert _index(project)["symbols"] == []
    with pytest.raises(PermissionError, match="symlink or junction"):
        symbol_index.index_repository(str(root_link))


def test_candidate_replaced_by_symlink_before_open_is_not_followed(
    project, tmp_path, monkeypatch,
):
    candidate = project / "race.py"
    candidate.write_text("def safe(): pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def escaped(): pass\n", encoding="utf-8")
    original = file_ops.resolve_repository_read_path
    replaced = False

    def race(path, **kwargs):
        nonlocal replaced
        resolved = original(path, **kwargs)
        if resolved == candidate and not replaced:
            replaced = True
            candidate.unlink()
            try:
                candidate.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                pytest.skip("symlink creation unavailable: %s" % exc)
        return resolved

    monkeypatch.setattr(file_ops, "resolve_repository_read_path", race)

    data = _index(project)

    assert replaced
    assert not any(row["name"] == "escaped" for row in data["symbols"])
    assert "symlink" in data["errors"][0]["error"].lower()


def test_relative_paths_use_forward_slashes_in_output(project):
    nested = project / "src" / "inner"
    nested.mkdir(parents=True)
    (nested / "main.py").write_text("def main(): pass\n", encoding="utf-8")

    data = _index(project)

    assert data["symbols"][0]["path"] == "src/inner/main.py"
    assert "src/inner/main.py:1:" in symbol_index.format_index(data)


def test_server_discovery_read_only_policy_activity_and_project_scope(project):
    (project / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    activity_tracker.reset_for_tests()

    assert server.mcp._tool_manager.get_tool("repository_symbol_index") is not None
    assert "repository_symbol_index" in server.tool_manifest()
    assert "repository_symbol_index" in server._agent_tool_help(read_only=True)
    assert "repository_symbol_index" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "repository_symbol_index" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "repository_symbol_index" in server._WORK_INSPECTION_TOOLS
    assert "repository_symbol_index" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "repository_symbol_index" in server._AUTOPILOT_OBSERVE_TOOLS

    with activity_tracker.response_span("test", "index symbols"):
        output = server._agent_dispatch_observed(
            "repository_symbol_index",
            {"path": ".", "glob": "*.py"},
            read_only=True,
            project=str(project),
        )

    assert "function main [python]" in output
    event = next(
        row for row in activity_tracker.latest()["events"]
        if row.get("kind") == "repository_symbol_index"
    )
    assert event["path"] == str(project.resolve())


def test_project_scope_rejects_escape_and_dedup_signature_normalizes_path(project):
    scoped = server._project_scope_args(
        "repository_symbol_index", {"path": "src/../src"}, str(project)
    )
    escaped = server._project_scope_args(
        "repository_symbol_index", {"path": "../outside"}, str(project)
    )

    assert Path(scoped["path"]).resolve() == (project / "src").resolve()
    assert "outside" in server._repository_scope_path_error(
        "repository_symbol_index", escaped, str(project)
    )
    first = server._agent_call_signature(
        "repository_symbol_index", {"path": str(project / "src" / ".." / "src"), "glob": "*.py"}
    )
    second = server._agent_call_signature(
        "repository_symbol_index", {"path": str(project / "src"), "glob": "*.py"}
    )
    assert first == second


def test_read_only_policy_forbids_model_supplied_extra_roots(project):
    error = server._repository_read_only_error(
        "repository_symbol_index",
        {"path": str(project), "extra_roots": str(project)},
    )

    assert "forbids argument(s): extra_roots" in error
