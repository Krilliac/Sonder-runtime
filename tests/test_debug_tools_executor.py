"""DebugToolExecutor: the typed envelope, the 48 KB fit and the error codes."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.debug_tools_executor import (
    DEBUG_TYPED_TOOLS,
    DebugToolExecutor,
    crash_digest_request,
    profile_request,
)
from sonder_runtime.application.debugging.ports import (
    CrashDigestRequest,
    DebugRunOutcome,
    ProfileDigestRequest,
    debug_error,
)
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import ToolCall, ToolDescriptor
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.tools.descriptors import ExecutionClass
from tests.test_debug_service import Stack, ctx

MAX = 48_000


class Fallback:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, descriptor, call, context, execution_class):
        self.calls.append(descriptor.name)
        return ToolExecutionResult(tool_name=descriptor.name, success=True, output="fallback")


class FakeService:
    def __init__(self, outcome=None, error=None) -> None:
        self.outcome = outcome or DebugRunOutcome("debug-run-" + "a" * 32, "running",
                                                  command_digest="d" * 64)
        self.error = error
        self.calls = []

    def _answer(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.outcome

    def crash(self, request, context, **kwargs):
        return self._answer("crash", request, **kwargs)

    def profile(self, request, context, **kwargs):
        return self._answer("profile", request, **kwargs)

    def result(self, run_id, context, **kwargs):
        return self._answer("result", run_id, **kwargs)

    def triage_detail(self, request, context):
        self._answer("triage", request)
        return (), ("note",)

    def profile_pure(self, request, context):
        return self._answer("profile_pure", request)


def run(executor, name, arguments, context=None):
    result = executor.execute(ToolDescriptor(name, "", {"type": "object"}),
                              ToolCall(name, arguments), context or ctx("mcp"), ExecutionClass.HOST)
    return result, json.loads(result.output)


def test_other_tools_go_to_the_fallback():
    fallback = Fallback()
    result = DebugToolExecutor(FakeService(), fallback).execute(
        ToolDescriptor("test_run", "", {"type": "object"}), ToolCall("test_run", {}), ctx(),
        ExecutionClass.HOST)
    assert result.output == "fallback" and fallback.calls == ["test_run"]


@pytest.mark.parametrize("name", DEBUG_TYPED_TOOLS)
def test_an_uncomposed_runtime_reports_unavailable(name):
    result, body = run(DebugToolExecutor(None, Fallback()), name, {"path": "x", "run_id": "x"})
    assert result.success is False
    assert body == {"ok": False, "error_code": "DEBUG_TOOLS_UNAVAILABLE",
                    "message": "debug tools are not composed in this runtime"}


def test_symbol_server_from_a_tool_call_is_refused_even_with_consent():
    stack = Stack()
    stack.consent.value = True
    executor = DebugToolExecutor(stack.service(), Fallback())
    for source in ("mcp", "repl", "http", "worker"):
        result, body = run(executor, "crash_digest", {"path": "/w/core.1", "symbol_server": True},
                           ctx(source))
        assert body["error_code"] == "SYMBOL_SERVER_NEEDS_CONSOLE" and not result.success
    assert stack.launcher.started == []


def test_a_running_outcome_is_an_ok_envelope_with_the_next_step():
    service = FakeService()
    result, body = run(DebugToolExecutor(service, Fallback()), "crash_digest",
                       {"path": "/w/core.1", "wait_seconds": 500})
    assert result.success and body["ok"] is True and body["status"] == "running"
    assert body["run_id"] == "debug-run-" + "a" * 32 and "debug_run_result" in body["next"]
    assert service.calls[0][2]["wait_seconds"] == 120  # clamped
    assert result.metadata["evidence"]["command_digest"] == "d" * 64


def test_the_executor_never_sets_console_confirmed():
    service = FakeService()
    run(DebugToolExecutor(service, Fallback()), "crash_digest", {"path": "/w/core.1"})
    assert "console_confirmed" not in service.calls[0][2]


@pytest.mark.parametrize("arguments", [
    {"path": "/w/c", "engine": "windbg"},
    {"path": "/w/c", "symbol_dirs": "/w/syms"},
    {"path": "/w/c", "symbol_dirs": ["/w/%d" % i for i in range(9)]},
    {"path": "/w/c", "symbol_dirs": [""]},
    {"path": "/w/c", "symbol_server": "yes"},
    {"path": "/w/c", "timeout_seconds": "60"},
    {"path": "/w/c", "timeout_seconds": True},
    {"path": ""},
    {"path": "x" * 1025},
])
def test_bad_crash_digest_arguments_are_invalid_input(arguments):
    result, body = run(DebugToolExecutor(FakeService(), Fallback()), "crash_digest", arguments)
    assert body["error_code"] == "INVALID_INPUT"


def test_request_mapping_clamps_and_keeps_host_owned_fields_out():
    request = crash_digest_request({"path": "/w/c", "timeout_seconds": 5000, "engine": "gdb",
                                    "argv": ["rm"], "resolved_command": {"x": 1}})
    assert request == CrashDigestRequest("/w/c", engine="gdb", timeout_seconds=900)
    pure = profile_request({"path": "/w/p", "engine": "perf", "executable": "/x", "top_n": 99,
                            "frame_budget_ms": 0.1}, capture=False)
    assert pure == ProfileDigestRequest("/w/p", top_n=50, frame_budget_ms=1.0)
    host = profile_request({"path": "/w/p", "engine": "perf", "timeout_seconds": 1}, capture=True)
    assert host.engine == "perf" and host.timeout_seconds == 10
    with pytest.raises(InvalidInput):
        profile_request({"path": "/w/p", "engine": "gdb"}, capture=True)


@pytest.mark.parametrize("error,code", [
    (debug_error("DEBUG_RUN_BUSY", "busy"), "DEBUG_RUN_BUSY"),
    (debug_error("ENGINE_UNAVAILABLE", "no gdb"), "ENGINE_UNAVAILABLE"),
    (debug_error("CAPTURE_TOO_LARGE", "big"), "CAPTURE_TOO_LARGE"),
    (debug_error("ENGINE_REFUSED_MANAGED_DUMP", "clr"), "ENGINE_REFUSED_MANAGED_DUMP"),
    (PermissionError("nope"), "CAPTURE_REJECTED"),
    (FileNotFoundError("/secret/host/path"), "CAPTURE_REJECTED"),
    (OSError("/secret/host/path failed"), "HOST_IO_FAILURE"),
    (ImportError("lane"), "DEBUG_TOOLS_UNAVAILABLE"),
])
def test_errors_become_typed_codes_without_host_paths(error, code):
    result, body = run(DebugToolExecutor(FakeService(error=error), Fallback()), "crash_digest",
                       {"path": "/w/core.1"})
    assert result.success is False and body["ok"] is False and body["error_code"] == code
    assert result.error_code == code
    if code in ("HOST_IO_FAILURE",):
        assert "/secret" not in result.output


def test_debug_run_result_rejects_non_ids_as_not_found():
    for run_id in (None, 5, "x" * 81):
        result, body = run(DebugToolExecutor(FakeService(), Fallback()), "debug_run_result",
                           {"run_id": run_id})
        assert body["error_code"] == "JOB_NOT_FOUND"


def test_debug_run_result_on_a_real_service_is_owner_checked():
    stack = Stack()
    executor = DebugToolExecutor(stack.service(), Fallback())
    result, body = run(executor, "debug_run_result", {"run_id": "debug-run-" + "b" * 32})
    assert body["error_code"] == "JOB_NOT_FOUND"


def test_the_payload_always_fits_48_kb():
    huge = DebugRunOutcome("debug-run-" + "a" * 32, "running", notes=tuple("n" * 239 + str(i) for i in range(32)),
                           display_argvs=tuple(tuple("a" * 1000 for _ in range(10)) for _ in range(8)))
    result, body = run(DebugToolExecutor(FakeService(huge), Fallback()), "crash_digest",
                       {"path": "/w/core.1"})
    assert len(result.output.encode("utf-8")) <= MAX
    assert body["ok"] is True and body["truncated"] is True


def test_directory_triage_returns_the_bucket_table_shape():
    result, body = run(DebugToolExecutor(FakeService(), Fallback()), "crash_triage",
                       {"path": "/w/dumps/", "max_threads": 99})
    assert body["object"] == "crash_buckets" and body["count"] == 0 and body["notes"] == ["note"]


def test_crash_reports_from_the_readers_fit_48_kb():
    from sonder_runtime.domain.crash import model

    frames = tuple(model.StackFrame(index=i, module="m.dll", function="f" * 200 + str(i),
                                    file="C:\\src\\" + "d" * 200 + ".cpp", line=i)
                   for i in range(64))
    threads = (model.ThreadSummary(1, "main", True, frames),) + tuple(
        model.ThreadSummary(i, "t%d" % i, False, frames[:8]) for i in range(2, 17))
    modules = tuple(model.ModuleInfo(name="m%d.dll" % i, path="C:\\" + "p" * 200) for i in range(128))
    report = model.CrashReport(source_kind="windows_minidump", threads=threads, modules=modules,
                               notes=tuple("n" * 240 for _ in range(32)))
    outcome = DebugRunOutcome("debug-run-" + "a" * 32, "complete", crash=report)
    result, body = run(DebugToolExecutor(FakeService(outcome), Fallback()), "crash_digest",
                       {"path": "/w/core.1"})
    assert len(result.output.encode("utf-8")) <= MAX
    assert body["ok"] is True and body["crash"]["truncated"] is True
    assert body["crash"]["untrusted_strings"] is True


# -- model-visible host paths ---------------------------------------------------

def _host_redactor(path):
    return path.replace("/home/op", "~").replace("/srv/proj", "[WORKSPACE]")


def test_notes_and_messages_are_path_redacted_on_the_wire():
    outcome = DebugRunOutcome("debug-run-" + "a" * 32, "complete", notes=(
        "module /home/op/build/game.so has no symbols",
        "source /home/bob/ci/render.cpp mapped to Engine/Render/render.cpp",
        "mac build at /Users/carol/src/x.mm",
        "[WORKSPACE] stays; relative src/op/x.c stays",
    ), command_digest="d" * 64)
    executor = DebugToolExecutor(FakeService(outcome), Fallback(), path_redactor=lambda: _host_redactor)
    result, body = run(executor, "crash_digest", {"path": "/w/core.1"})
    assert body["notes"] == [
        "module ~/build/game.so has no symbols",
        "source /home/<user>/ci/render.cpp mapped to Engine/Render/render.cpp",
        "mac build at /Users/<user>/src/x.mm",
        "[WORKSPACE] stays; relative src/op/x.c stays",
    ]
    assert body["command_digest"] == "d" * 64


def test_failure_messages_are_path_redacted():
    error = debug_error("CAPTURE_REJECTED", "capture /home/op/secret/core.1 rejected")
    executor = DebugToolExecutor(FakeService(error=error), Fallback(), path_redactor=lambda: _host_redactor)
    result, body = run(executor, "crash_digest", {"path": "/w/core.1"})
    assert "/home/op" not in result.output and "~/secret/core.1" in body["message"]


def test_os_permission_errors_do_not_name_the_path():
    error = PermissionError(13, "Permission denied", "/home/op/private/core.1")
    result, body = run(DebugToolExecutor(FakeService(error=error), Fallback()), "crash_digest",
                       {"path": "/w/core.1"})
    assert body["error_code"] == "CAPTURE_REJECTED" and "private" not in result.output


def test_crash_report_module_and_frame_paths_are_redacted_on_the_wire():
    from sonder_runtime.domain.crash import model

    frame = model.StackFrame(index=0, module="game", function="Renderer::Submit",
                             file="/home/op/src/render.cpp", line=7)
    report = model.CrashReport(
        source_kind="elf_core", threads=(model.ThreadSummary(1, "main", True, (frame,)),),
        modules=(model.ModuleInfo(name="game", path="/home/bob/build/game"),
                 model.ModuleInfo(name="w.exe", path="C:\\Users\\dana\\w.exe")))
    outcome = DebugRunOutcome("debug-run-" + "a" * 32, "complete", crash=report)
    executor = DebugToolExecutor(FakeService(outcome), Fallback(), path_redactor=lambda: _host_redactor)
    result, body = run(executor, "crash_digest", {"path": "/w/core.1"})
    for name in ("/home/op", "bob", "dana"):
        assert name not in result.output
    top = body["crash"]["threads"][0]["frames"][0]
    assert top["file"] == "~/src/render.cpp" and top["function"] == "Renderer::Submit"
