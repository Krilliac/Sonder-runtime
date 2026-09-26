"""Typed build reports judge recent generations by their terminal status.

The typed build executor sets ``ok`` on every report it returns, a failed
build included, so reading ``ok`` would file every failed compile as
``compiled``. The verdict must come from the report's own status, and a job
that has not finished (or never compiled anything) must record nothing and
leave the pending generation for the real verdict.
"""
from __future__ import annotations

import json
import uuid

import pytest

import grounded_outcomes as go
from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from tests.test_build_executor import FakeJobs, compose_facade, fake_services, gateway_call

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean():
    go.reset()
    yield
    go.reset()


def _sink():
    written = []
    return written, lambda *a, **k: written.append(tuple(a[:2]))


def report(status="succeeded", *, action="build", exit_code=0, job_id="build-job-1"):
    return {"ok": True, "object": "build_job_report", "job_id": job_id, "status": status,
            "action": action, "exit_code": exit_code, "world": "host"}


# --- the verdict ----------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(go.TYPED_BUILD_VERIFIERS))
@pytest.mark.parametrize("status, exit_code, signal", [
    ("succeeded", 0, "compiled"), ("failed", 2, "failed"),
])
def test_a_finished_compile_is_judged_by_its_status_never_by_ok(tool, status, exit_code, signal):
    written, record = _sink()
    go.note_generation("gen-1", "sonder", "/p")
    # The caller's ``ok`` is the executor's forced True (or anything else):
    # it never decides the signal.
    result = go.attribute(tool, not (signal == "compiled"), "/p", record_fn=record,
                          evidence=report(status, exit_code=exit_code))
    assert result["attributed"] is True and result["signal"] == signal
    assert written == [("gen-1", signal)]


@pytest.mark.parametrize("evidence", [
    {"ok": True, "object": "build_job_status", "job_id": "j", "status": "running"},
    {"ok": True, "object": "build_job_status", "job_id": "j", "status": "cancelled"},
    report("timed_out", exit_code=None),
    report("cancelled", exit_code=None),
    report("did_not_run", exit_code=None),
    report("running", exit_code=None),
    report("failed", exit_code=None),        # could not start: no process exited
    report("succeeded", action="configure"),  # configuring compiles nothing
    report("failed", action="include_trace", exit_code=1),
    {"ok": True, "status": "succeeded"},     # no report object at all
    "build build core: succeeded",           # rendered text is not a typed report
    None,
])
def test_no_compiler_verdict_records_nothing_and_keeps_the_generation(evidence):
    written, record = _sink()
    go.note_generation("gen-1", "sonder")
    result = go.attribute("build_job_result", True, record_fn=record, evidence=evidence)
    assert result["attributed"] is False and result["evaluation_infrastructure_error"]
    assert written == [] and go.stats()["unmeasured"] == 1
    # The generation is still there for the real verdict.
    later = go.attribute("build_job_result", True, record_fn=record,
                         evidence=report("failed", exit_code=1))
    assert later["signal"] == "failed" and written == [("gen-1", "failed")]


def test_the_fix_tools_are_not_verifiers():
    """A fix report grades the fix's own edits, never an earlier generation."""
    written, record = _sink()
    go.note_generation("gen-1", "sonder")
    for tool in ("build_fix", "build_fix_result"):
        result = go.attribute(tool, True, record_fn=record,
                              evidence={"ok": True, "object": "build_fix_report",
                                        "status": "fixed"})
        assert result["attributed"] is False
    assert written == [] and go.pending_count() == 1


# --- the receipt observer ------------------------------------------------------------------


def test_a_receipt_observer_sees_each_call_and_can_never_fail_it(tmp_path):
    tools, _audit, _grants = compose_facade(tmp_path, fake_services(str(tmp_path)))
    seen = []

    def observer(request, receipt):
        seen.append((request.tool_name, receipt.success))

    def broken(request, receipt):
        raise RuntimeError("observer bug")

    tools.add_receipt_observer(observer)
    tools.add_receipt_observer(observer)  # idempotent
    tools.add_receipt_observer(broken)
    receipt = gateway_call(tools, "build_job", {"project": str(tmp_path)}, source="repl",
                           gate="surface")
    assert receipt.success and seen == [("build_job", True)]
    tools.remove_receipt_observer(observer)
    gateway_call(tools, "build_job", {"project": str(tmp_path)}, source="repl", gate="surface")
    assert len(seen) == 1
    with pytest.raises(TypeError):
        tools.add_receipt_observer("not callable")


def test_the_server_graph_installs_the_typed_build_feed(monkeypatch):
    import server
    from sonder_runtime.bootstrap import app as bootstrap_app

    tools = ToolApplicationFacade.compose(typed_tool_registry(), receipts=ReceiptStore())

    class Graph:
        pass

    graph = Graph()
    graph.tools = tools
    monkeypatch.setattr(bootstrap_app, "build_application", lambda **kwargs: graph)
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    assert server._application() is graph
    assert server._typed_receipt_outcome in tools._observers


# --- the production feed: typed gateway -> server ledger --------------------------------------


class ReportingJobs(FakeJobs):
    """``FakeJobs`` whose result is a finished report the test chooses."""

    def __init__(self, status, exit_code):
        super().__init__()
        self.final = (status, exit_code)

    def result(self, job_id, context, *, wait_seconds=0):
        self._owned(job_id, context)
        status, exit_code = self.final
        return report(status, exit_code=exit_code, job_id=job_id)


@pytest.fixture
def feed(tmp_path, monkeypatch):
    """A typed gateway over fake build services, observed by the server feed."""
    import server

    written = []
    monkeypatch.setattr(server, "_record_outcome_signal",
                        lambda ident, signal: written.append((ident, signal)))

    def compose(status, exit_code):
        services = fake_services(str(tmp_path))
        services.jobs = ReportingJobs(status, exit_code)
        tools, _audit, _grants = compose_facade(tmp_path, services)
        tools.add_receipt_observer(server._typed_receipt_outcome)
        return tools

    return compose, written


def _run_and_collect(tools, project):
    started = gateway_call(tools, "build_job", {"project": project, "target": "core"},
                           source="repl", gate="surface")
    assert started.success, started.error
    job_id = json.loads(started.output)["job_id"]
    finished = gateway_call(tools, "build_job_result", {"job_id": job_id},
                            source="repl", gate="surface")
    assert finished.success, finished.error
    assert json.loads(finished.output)["ok"] is True  # the executor's forced ok
    return job_id


@pytest.mark.parametrize("status, exit_code, signal", [
    ("succeeded", 0, "compiled"), ("failed", 1, "failed"),
])
def test_a_typed_build_result_feeds_the_generation_in_its_project(feed, tmp_path, status,
                                                                  exit_code, signal):
    compose, written = feed
    project = tmp_path / "game"
    project.mkdir()
    go.note_generation("gen-" + uuid.uuid4().hex[:8], "sonder", str(project.resolve()))
    ident = go._PENDING[-1].interaction_id
    _run_and_collect(compose(status, exit_code), str(project))
    # The running build_job measured nothing; the result judged the generation.
    assert written == [(ident, signal)]
    assert go.stats()["unmeasured"] == 1


def test_a_typed_build_of_another_project_never_judges_this_generation(feed, tmp_path):
    compose, written = feed
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    go.note_generation("gen-mine", "sonder", str(mine.resolve()))
    _run_and_collect(compose("failed", 1), str(other))
    assert written == [] and go.pending_count() == 1


def test_an_unfinished_typed_build_records_nothing(feed, tmp_path):
    compose, written = feed
    go.note_generation("gen-1", "sonder")
    tools = compose("timed_out", None)
    _run_and_collect(tools, str(tmp_path))
    assert written == [] and go.pending_count() == 1
    assert go.stats()["unmeasured"] == 2


# --- the production handoff: default_app -> configure_application -> REPL /build -----------


def test_a_handed_over_graph_feeds_the_ledger_from_a_repl_build(tmp_path, monkeypatch):
    """``repl``/``serve``/``mcp`` compose the graph themselves and hand it over
    through ``legacy_root.configure_application``; ``server._application()``
    then returns it without building one. That graph must feed the ledger too,
    so a REPL ``/build`` result judges the pending generation of its project."""
    from dataclasses import replace

    import server
    import sonder_runtime.platform.environment_probe as environment_probe
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import build_tools, legacy_root
    from sonder_runtime.interfaces.repl import repl
    from sonder_runtime.platform.config import SonderConfig

    workspace = tmp_path / "workspace"
    project = workspace / "game"
    project.mkdir(parents=True)
    services = fake_services(str(project))
    services.jobs = ReportingJobs("failed", 1)
    monkeypatch.setattr(build_tools, "compose_build_tools", lambda **kwargs: services)
    written = []
    monkeypatch.setattr(server, "_record_outcome_signal",
                        lambda ident, signal: written.append((ident, signal)))
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    config = SonderConfig()
    config = replace(config, state=replace(config.state, home=str(tmp_path / "state"),
                                           workspace_roots=(str(workspace),)))
    previous_provider = environment_probe._capability_summary_provider
    application = bootstrap_app.build_application(config=config)
    try:
        monkeypatch.setattr(server, "OLLAMA_POOL",
                            legacy_root.require_inference_application(application))
        legacy_root.configure_application(application)
        assert server._typed_receipt_outcome in application.tools._observers
        # The REPL reaches that same graph and never builds its own.
        assert repl._typed_tools() is application.tools
        assert server._APP_GRAPH_OWNED_BY_SERVER is False

        go.note_generation("gen-" + uuid.uuid4().hex[:8], "sonder", str(project.resolve()))
        ident = go._PENDING[-1].interaction_id
        monkeypatch.setattr(repl, "_stdout_is_interactive", lambda: False)
        monkeypatch.setattr(repl, "_emit", lambda text: None)
        repl._build_command("/build", "run core --project %s" % project, str(workspace))
        (job_id,) = services.jobs.owners
        assert written == []  # a running job judges nothing
        repl._build_command("/build", "result %s" % job_id, str(workspace))
        assert written == [(ident, "failed")]
    finally:
        build_tools.uninstall_build_brief()
        environment_probe.set_capability_summary_provider(previous_provider)
        application.close_delegation(timeout=10)
