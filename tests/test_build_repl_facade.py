"""REPL build commands: grammar, the execute_tool-only route, and the specs."""
from __future__ import annotations

import ast
import inspect

import pytest

from sonder_runtime.bootstrap.typed_tools import BUILD_TOOLS
from sonder_runtime.interfaces.repl.facades import build_tools as facade
from sonder_runtime.interfaces.repl.facades.build_tools import (
    BUILD_COMMAND_SPECS,
    NOT_COMPOSED,
    BuildReplFacade,
    UsageError,
    parse_build,
    parse_fix,
    parse_restore,
    register_build_commands,
    render_result,
    split_words,
)

pytestmark = pytest.mark.unit

JOB = "build-job-" + "ab" * 16
FIX = "build-fix-" + "cd" * 16


@pytest.mark.parametrize("arg, expected", [
    ("", ("build_model", {})),
    ("model --dir build/ninja", ("build_model", {"build_dir": "build/ninja"})),
    ("model --preset ninja-debug --detail targets --refresh",
     ("build_model", {"preset": "ninja-debug", "detail": "targets", "refresh": True})),
    ("run configure --preset ninja-debug", ("build_job", {"preset": "ninja-debug", "action": "configure"})),
    ("run deploy", ("build_job", {"action": "build", "target": "deploy"})),
    ("run game --config Debug --jobs 8 --wait 30",
     ("build_job", {"config": "Debug", "jobs": 8, "wait_seconds": 30, "action": "build", "target": "game"})),
    ("run", ("build_job", {"action": "build"})),
    ("run compile src/core/math.cpp", ("build_job", {"action": "compile_one", "file": "src/core/math.cpp"})),
    ("run game --platform \"Gaming.Xbox.Scarlett.x64\" --allow-network",
     ("build_job", {"platform": "Gaming.Xbox.Scarlett.x64", "allow_network": True, "action": "build",
                    "target": "game"})),
    ("trace src\\core\\math.cpp --dir C:\\b", ("build_job", {"build_dir": "C:\\b", "action": "include_trace",
                                                            "file": "src\\core\\math.cpp"})),
    ("status " + JOB, ("build_job_result", {"job_id": JOB})),
    ("cancel " + JOB, ("build_job_result", {"job_id": JOB, "cancel": True})),
])
def test_build_grammar(arg, expected):
    assert parse_build(arg) == expected


@pytest.mark.parametrize("arg", [
    "run a b", "run compile", "trace", "status nope", "cancel build-job-xyz", "bogus",
    "model extra", "run game --jobs many", "run game --config", "run game --unknown x",
    "run configure extra", "model --allow-network",
])
def test_build_grammar_refusals(arg):
    with pytest.raises(UsageError):
        parse_build(arg)


def test_fix_and_restore_grammar():
    assert parse_fix("game --config Debug --attempts 4 --revert-after --file src/a.cpp") == (
        "build_fix", {"config": "Debug", "attempts": 4, "revert_after": True,
                      "focus_file": "src/a.cpp", "target": "game"})
    assert parse_fix("game --verify-dependents --platform x64") == (
        "build_fix", {"verify_dependents": True, "platform": "x64", "target": "game"})
    assert parse_fix("status " + FIX) == ("build_fix_result", {"job_id": FIX})
    assert parse_fix("cancel " + FIX) == ("build_fix_result", {"job_id": FIX, "cancel": True})
    # a target that happens to be called "status" is still a target
    assert parse_fix("status") == ("build_fix", {"target": "status"})
    assert parse_restore(FIX) == ("build_fix_restore", {"job_id": FIX})
    assert parse_restore(FIX + " src/a.cpp src/b.cpp") == (
        "build_fix_restore", {"job_id": FIX, "files": ["src/a.cpp", "src/b.cpp"]})
    for bad in ("", "a b", "game --attempts x", "game --preset p"):
        with pytest.raises(UsageError):
            parse_fix(bad)
    for bad in ("", "nope", FIX + " --force", FIX + " " + " ".join(["f"] * 7)):
        with pytest.raises(UsageError):
            parse_restore(bad)


def test_split_keeps_windows_backslashes_and_quotes():
    assert split_words('trace "C:\\Program Files\\x.cpp" a\\b') == [
        "trace", "C:\\Program Files\\x.cpp", "a\\b"]
    with pytest.raises(UsageError):
        split_words('trace "unterminated')


def test_the_facade_routes_only_through_execute_tool():
    calls = []

    def execute_tool(tool, arguments):
        calls.append((tool, arguments))
        return {"ok": True, "object": "build_job_status", "job_id": JOB, "status": "running",
                "display_command": ["cmake", "--build", "[BUILD]"],
                "next": "call build_job_result with this job_id"}

    repl = BuildReplFacade(execute_tool)
    text = repl.build("run game --config Debug")
    assert calls == [("build_job", {"config": "Debug", "action": "build", "target": "game"})]
    assert JOB in text and "cmake --build [BUILD]" in text
    assert "usage:" in repl.build("run a b") and len(calls) == 1
    repl.fix_build("game")
    repl.fix_build_restore(FIX)
    assert [tool for tool, _ in calls] == ["build_job", "build_fix", "build_fix_restore"]
    assert BuildReplFacade(None).build("") == NOT_COMPOSED
    # the module imports no service, adapter or root module: execute_tool is the only route
    tree = ast.parse(inspect.getsource(facade))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported <= {"__future__", "re", "shlex", "dataclasses", "typing"}


def test_refusals_and_reports_render():
    assert render_result("build_job", {"ok": False, "error_code": "UTILITY_TARGET_REFUSED",
                                       "message": "deploy is a utility target"}) == (
        "build_job refused: UTILITY_TARGET_REFUSED -- deploy is a utility target")
    text = render_result("build_fix_result", {
        "ok": True, "object": "build_fix_report", "job_id": FIX, "status": "fixed",
        "stop_reason": "FIXED", "verification_scope": "target",
        "files": [{"rel": "src/core/math.cpp", "before_sha256": "a", "after_sha256": "b"}],
        "first_errors": ["x" * 1000]})
    assert "fixed FIXED" in text and "verification_scope: target" in text
    assert "rel=src/core/math.cpp" in text
    assert max(len(line) for line in text.splitlines()) <= 250


def test_specs_are_well_formed_and_register():
    names = [spec.name for spec in BUILD_COMMAND_SPECS]
    assert names == ["/build", "/fix-build", "/fix-build-restore"]
    for spec in BUILD_COMMAND_SPECS:
        assert spec.usage.startswith(spec.name) and spec.summary and spec.tools
        assert set(spec.tools) <= set(BUILD_TOOLS)
    assert {tool for spec in BUILD_COMMAND_SPECS for tool in spec.tools} == set(BUILD_TOOLS)
    registered = {}
    register_build_commands(lambda name, handler, spec: registered.setdefault(name, (handler, spec)),
                            facade_getter=lambda: None)
    assert set(registered) == set(names)
    assert registered["/build"][0]("") == NOT_COMPOSED
    seen = []
    live = BuildReplFacade(lambda tool, arguments: seen.append(tool) or {"ok": True, "status": "x"})
    register_build_commands(lambda name, handler, spec: registered.__setitem__(name, (handler, spec)),
                            facade_getter=lambda: live)
    registered["/fix-build"][0]("game")
    assert seen == ["build_fix"]
