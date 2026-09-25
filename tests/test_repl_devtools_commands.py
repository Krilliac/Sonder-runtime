"""REPL ``/tools``, ``/test`` and ``/digest`` against fake composed services."""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import permission_modes as pm
import sonder_runtime.interfaces.repl.repl as repl
from sonder_runtime.adapters import command_catalog
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.diagnostics.ports import TextWindow
from sonder_runtime.application.diagnostics.service import OutputDigestService
from sonder_runtime.domain.common.errors import CapacityExceeded, InvalidInput, NotFound
from sonder_runtime.interfaces.repl.facades import developer_tools as facade
from sonder_runtime.platform.logging import Redactor


# --- lane-B request type: the real one once composed, else a spec-exact double --

@pytest.fixture(autouse=True)
def _test_run_request_type(monkeypatch):
    try:
        from sonder_runtime.application.testing.ports import TestRunRequest  # noqa: F401
        return
    except ImportError:
        pass

    @dataclass(frozen=True)
    class TestRunRequest:
        project: str = "."
        runner: str = "auto"
        selector: str = ""
        timeout_seconds: int | None = None
        workers: int | None = None

    package = types.ModuleType("sonder_runtime.application.testing")
    package.__path__ = []
    ports = types.ModuleType("sonder_runtime.application.testing.ports")
    ports.TestRunRequest = TestRunRequest
    monkeypatch.setitem(sys.modules, "sonder_runtime.application.testing", package)
    monkeypatch.setitem(sys.modules, "sonder_runtime.application.testing.ports", ports)


# --- fakes -------------------------------------------------------------------------

CATEGORIES = ("compiler", "build_system", "test_runner")


def _tool(name, category, version, path, **extra):
    return SimpleNamespace(
        name=name, category=category, version=version, version_status="ok",
        source="path", on_path=True, path_display=path,
        alternatives_display=extra.get("alternatives", ()), details=extra.get("details", ()),
    )


TOOLS = (
    _tool("gcc", "compiler", "13.2.0", "/usr/bin/gcc", alternatives=("/opt/gcc/bin/gcc",)),
    _tool("clang", "compiler", "18.1.3", "/usr/bin/clang"),
    _tool("cmake", "build_system", "3.28.3", "/usr/bin/cmake", details=(("generator", "Ninja"),)),
    _tool("pytest", "test_runner", "9.1.0", "/home/me/.venv/bin/pytest"),
)


class FakeInventory:
    def __init__(self):
        self.calls = []

    def view(self, *, category=None, name=None, refresh=False, redacted=True):
        self.calls.append(("view", category, name, refresh, redacted))
        if category is not None and category not in CATEGORIES:
            raise InvalidInput("unknown tool category")
        tools = [t for t in TOOLS if category in (None, t.category)]
        if name is not None:
            tools = [t for t in tools if t.name == name.lower()]
        counts = tuple((c, sum(1 for t in TOOLS if t.category == c)) for c in CATEGORIES)
        return SimpleNamespace(
            snapshot_digest="ab" * 32, created_at=0.0, age_seconds=90, stale=False,
            os="Linux", machine="x86_64", counts=counts, tools=tuple(tools),
            filtered_by=category or name or "",
        )

    def snapshot(self, *, refresh=False, full=False):
        self.calls.append(("snapshot", refresh, full))

    def cached(self):
        return None


class FakeTestRuns:
    def __init__(self, polls_before_done=2, start_error=None):
        self.polls_before_done = polls_before_done
        self.start_error = start_error
        self.started = []
        self.cancelled = []
        self.results = 0

    def start(self, request, context, *, plan=None):
        if self.start_error is not None:
            raise self.start_error
        self.started.append((request, context))
        return "test-run-0001"

    def _status(self, job_id, status="running"):
        if job_id != "test-run-0001":
            raise NotFound("job not found")
        return SimpleNamespace(
            job_id=job_id, status=status, runner="pytest", elapsed_seconds=3.0,
            command_digest="d" * 64, display_command=("python", "-m", "pytest", "-q"),
        )

    def status(self, job_id, context):
        return self._status(job_id)

    def result(self, job_id, context, *, wait_seconds=0):
        self.results += 1
        if self.results <= self.polls_before_done:
            return self._status(job_id)
        return SimpleNamespace(
            job_id=job_id, runner="pytest", status="failed", exit_code=1,
            duration_seconds=2.5, display_command=("python", "-m", "pytest", "-q"),
            totals=SimpleNamespace(passed=2, failed=1, skipped=1, errors=0, total=4),
            totals_source="junit_xml", totals_reliable=True,
            summary_line="1 failed, 2 passed, 1 skipped in 0.20s",
            failures=(SimpleNamespace(id="test_mod.py::test_bad", file="test_mod.py", line=13,
                                      message_excerpt="assert 1 == 2"),),
            failures_truncated=False, notes=(), output_truncated=False, report_truncated=False,
        )

    def cancel(self, job_id, context, *, reason):
        self.cancelled.append((job_id, reason))
        return self._status(job_id, status="cancelled")


class _Jobs:
    def job_metadata(self, job_id):
        if job_id == "test-run-0001":
            return {"kind": "process", "principal_id": "someone"}
        return None

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        text = "FAILED t.py::test_a - boom\n1 failed in 0.1s\n"
        return TextWindow(text, "job:" + job_id, len(text), len(text), False)


class _Files:
    def read_file_window(self, path, **kwargs):
        if path.endswith(".env"):
            raise PermissionError("DIGEST_SOURCE_REJECTED")
        text = "make: *** [Makefile:3: all] Error 2\n"
        return TextWindow(text, path, len(text), len(text), False)


def _services(**kwargs):
    return SimpleNamespace(
        inventory=kwargs.get("inventory", FakeInventory()),
        test_runs=kwargs.get("test_runs", FakeTestRuns()),
        digest=kwargs.get("digest", OutputDigestService(_Files(), _Jobs(), redact=Redactor(env={}).redact)),
    )


@pytest.fixture
def services(monkeypatch):
    value = _services()
    monkeypatch.setattr(repl, "_developer_services", lambda: value)
    return value


def _context():
    return local_owner_context(correlation_id="t")


# --- /tools ----------------------------------------------------------------------


def test_tools_lists_by_category_with_full_paths(services):
    text = repl._tools_command("")
    assert text.startswith("host tools: 4 on Linux x86_64; snapshot abababababab, age 90s")
    assert "compiler (2):" in text and "build_system (1):" in text
    assert "/home/me/.venv/bin/pytest" in text  # the operator sees full paths
    assert services.inventory.calls[0] == ("view", None, None, False, False)


def test_tools_category_name_and_refresh_forms(services):
    assert "cmake" in repl._tools_command("build_system")
    assert "gcc" not in repl._tools_command("build_system")
    detailed = repl._tools_command("gcc")
    assert "also: /opt/gcc/bin/gcc" in detailed
    assert "generator: Ninja" in repl._tools_command("cmake")
    repl._tools_command("refresh")
    repl._tools_command("refresh full")
    assert ("snapshot", True, False) in services.inventory.calls
    assert ("snapshot", True, True) in services.inventory.calls
    assert repl._tools_command("refresh everything") == facade.TOOLS_USAGE


def test_tools_invalid_category_or_name_message(services):
    text = repl._tools_command("bogus")
    assert text.startswith("unknown category or tool: bogus")
    assert "categories: compiler, build_system, test_runner" in text


def test_not_composed_messages(monkeypatch):
    monkeypatch.setattr(repl, "_developer_services", lambda: None)
    lines = []
    assert repl._tools_command("") == facade.NOT_COMPOSED
    assert repl._digest_command("x.log") == facade.NOT_COMPOSED
    repl._test_command("", out=lines.append)
    assert lines == [facade.NOT_COMPOSED]


# --- /test -----------------------------------------------------------------------


def test_test_start_waits_and_prints_the_report(services):
    lines = []
    ticks = iter(range(0, 1000, 11))
    repl._test_command("pytest k:login", "", poll_seconds=0.0, clock=lambda: next(ticks),
                       out=lines.append)
    request, context = services.test_runs.started[0]
    assert (request.runner, request.selector, request.project) == ("pytest", "k:login", ".")
    assert context.source == "repl" and context.workspace_roots
    assert lines[0].startswith("started test run test-run-0001: running (pytest)")
    assert any("still running" in line for line in lines)
    report = lines[-1]
    assert report.startswith("test run test-run-0001: failed (pytest) exit=1")
    assert "totals: passed=2 failed=1 skipped=1 errors=0 total=4 (from junit_xml)" in report
    assert "test_mod.py::test_bad (test_mod.py:13) assert 1 == 2" in report


def test_test_runner_word_is_optional(services):
    repl._test_command("tests/test_x.py::test_y", "/w/proj", poll_seconds=0.0, out=lambda _l: None)
    request, context = services.test_runs.started[0]
    assert (request.runner, request.selector, request.project) == (
        "auto", "tests/test_x.py::test_y", "/w/proj",
    )
    assert str(context.workspace_roots[0]) == "/w/proj"


def test_test_status_result_and_cancel_followups(services):
    lines = []
    repl._test_command("status test-run-0001", out=lines.append)
    repl._test_command("cancel test-run-0001", out=lines.append)
    repl._test_command("result missing-job", out=lines.append)
    repl._test_command("status", out=lines.append)
    assert lines[0].startswith("test run test-run-0001: running (pytest) 3s")
    assert lines[1].startswith("cancelled test run test-run-0001: cancelled")
    assert services.test_runs.cancelled == [("test-run-0001", "cancelled by the local operator")]
    assert lines[2] == "no test run job missing-job"
    assert lines[3] == facade.TEST_USAGE


def test_keyboard_interrupt_during_the_wait_cancels_the_job(monkeypatch, services):
    lines = []

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(repl, "_poll_test_result", interrupted)
    repl._test_command("", poll_seconds=0.0, out=lines.append)
    assert services.test_runs.cancelled and services.test_runs.cancelled[0][0] == "test-run-0001"
    assert lines[-1].startswith("cancelled")


@pytest.mark.parametrize("error, expected", [
    (CapacityExceeded("busy"), "test run refused: CAPACITY_EXCEEDED"),
    (InvalidInput("selector must not start with '-'"), "test run refused: INVALID_INPUT: selector"),
])
def test_start_refusals_print_and_do_not_wait(monkeypatch, error, expected):
    runs = FakeTestRuns(start_error=error)
    monkeypatch.setattr(repl, "_developer_services", lambda: _services(test_runs=runs))
    lines = []
    repl._test_command("pytest --junitxml=x", out=lines.append)
    assert lines[0].startswith(expected)
    assert runs.results == 0


# --- /digest ---------------------------------------------------------------------


def test_digest_job_as_operator_and_path_fallback(services):
    job = repl._digest_command("test-run-0001")
    assert job.startswith("summary: 1 failed in 0.1s")  # operator may read any job kind
    path = repl._digest_command("build.log")
    assert path.startswith("summary: make: *** [Makefile:3: all] Error 2")
    refused = repl._digest_command("secrets/.env")
    assert refused.startswith("digest refused: secrets/.env")
    assert repl._digest_command("") == facade.DIGEST_USAGE


# --- wiring into the console chain --------------------------------------------------


def test_activity_still_works_and_tools_is_no_longer_its_alias():
    import server

    assert "activity" in server.control_command("/activity").lower()
    groups = command_catalog._slash_groups(command_catalog._source_path("sonder_repl.py"))
    assert ("/tools",) in groups and ("/activity",) in groups
    assert not any("/tools" in group and "/activity" in group for group in groups)


def test_console_catalog_grades_the_new_commands():
    tools = command_catalog.console_tools()
    assert "test_run" in tools["/test"]
    assert tools["/tools"] == ("toolchain_status",)
    assert tools["/digest"] == ("log_inspect",)
    assert command_catalog.by_name("/test").risk == "execution"
    assert command_catalog.by_name("/tools").risk == "safe"
    assert command_catalog.by_name("/digest").risk == "safe"


def test_plan_mode_refuses_test_and_allows_tools_and_digest(monkeypatch):
    monkeypatch.setattr(repl, "_confirm", lambda _q: pytest.fail("plan must not prompt"))
    previous = pm.current_mode()
    pm.set_mode(pm.PLAN)
    try:
        may_run, refusal = repl._named_command_gate("/test", "pytest")
        assert not may_run and refusal.startswith("refused /test:")
        assert repl._named_command_gate("/tools") == (True, "")
        assert repl._named_command_gate("/digest", "build.log") == (True, "")
    finally:
        pm.set_mode(previous)


def test_help_lists_the_new_commands():
    for command in ("/tools", "/test", "/digest"):
        assert command in repl.HELP
