import inspect

from sonder_runtime.application.ports.tool_executor import ToolResult
from sonder_runtime.application.workflows import WorkflowService, render_workflow_result
from sonder_runtime.application.workflows import loop as workflow_loop


class Repository:
    def __init__(self):
        self.workflows = {}

    def ensure(self):
        return self.workflows, "workflows.json"

    def save(self, name, actions, description=""):
        if not isinstance(actions, list) or not actions:
            raise ValueError("workflow actions must be a non-empty JSON list")
        self.workflows[name] = {"actions": actions, "description": description}
        return self.workflows[name], "workflows.json"

    def get(self, name):
        return self.workflows.get(name)

    def delete(self, name):
        return self.workflows.pop(name, None) is not None, "workflows.json"

    def normalize_name(self, name):
        return name.strip().lower()

    def format(self, workflows):
        return "formatted:%d" % len(workflows)


class Runner:
    def __init__(self, formatted="done", ok=True, cancelled=False):
        self.formatted = formatted
        self.ok = ok
        self.cancelled = cancelled

    def run(self, actions, dispatch, **options):
        return {
            "ok": self.ok, "cancelled": self.cancelled,
            "actions": actions, "options": options,
        }

    def format(self, result):
        return self.formatted


def test_server_workflow_tool_signatures_remain_stable():
    import server

    assert str(inspect.signature(server.workflow_list)) == "() -> str"
    assert str(inspect.signature(server.workflow_save)) == (
        "(name: str, actions_json: str, description: str = '') -> str"
    )
    assert str(inspect.signature(server.workflow_run)) == (
        "(name: str, max_iterations: int = 1, stop_on_failure: bool = True, "
        "stop_on_success: bool = False, delay_seconds: float = 0) -> str"
    )
    assert str(inspect.signature(server.workflow_delete)) == "(name: str) -> str"


def test_workflow_status_is_typed_not_inferred_from_error_prefix():
    assert render_workflow_result(ToolResult(ok=True, output="ERROR: literal content")) == (
        "ERROR: literal content"
    )
    assert render_workflow_result(
        ToolResult(ok=False, output="invalid", error_code="INVALID_INPUT")
    ) == "ERROR: invalid"


def test_workflow_service_preserves_text_and_injects_dispatch_without_server_import():
    repository = Repository()
    service = WorkflowService(repository, Runner("ERROR: successful loop content"))
    saved = service.save("demo", '[{"type":"status"}]', "demo flow")
    assert saved.ok is True
    result = service.run("demo", lambda action: {"ok": True, "output": action})
    assert result.ok is True
    assert result.output == "workflow: demo\ndemo flow\nERROR: successful loop content"
    assert render_workflow_result(result) == result.output


def test_workflow_invalid_json_and_missing_name_are_typed_failures():
    service = WorkflowService(Repository(), Runner())
    invalid = service.save("demo", "not-json")
    missing = service.run("missing", lambda _action: {})
    assert invalid.ok is False and invalid.error_code == "INVALID_INPUT"
    assert render_workflow_result(invalid).startswith(
        "ERROR: actions_json is not valid JSON:"
    )
    assert missing.ok is False and missing.error_code == "NOT_FOUND"
    assert render_workflow_result(missing) == "ERROR: no workflow named 'missing'."


def test_failed_and_cancelled_loops_are_typed_without_changing_legacy_text():
    repository = Repository()
    repository.save("demo", [{"type": "probe"}], "demo flow")
    failed = WorkflowService(repository, Runner("loop status: failed", ok=False)).run(
        "demo", lambda _action: {"ok": False}
    )
    cancelled = WorkflowService(
        repository, Runner("loop status: cancelled", ok=False, cancelled=True)
    ).run("demo", lambda _action: {"ok": True})

    assert failed.ok is False and failed.error_code == "WORKFLOW_FAILED"
    assert cancelled.ok is False and cancelled.error_code == "CANCELLED"
    assert render_workflow_result(failed) == (
        "workflow: demo\ndemo flow\nloop status: failed"
    )
    assert render_workflow_result(cancelled) == (
        "workflow: demo\ndemo flow\nloop status: cancelled"
    )


def test_loop_cancellation_is_fail_closed_and_discards_late_success():
    cancelled = {"value": False}

    def dispatch(_action):
        cancelled["value"] = True
        return {"ok": True, "type": "probe", "summary": "late", "output": ""}

    result = workflow_loop.run_loop(
        [{"type": "probe"}, {"type": "never"}],
        dispatch,
        max_iterations=5,
        cancel_check=lambda: cancelled["value"],
    )
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["stop_reason"] == "cancelled after action 1 in iteration 1"
    assert len(result["iterations"][0]["actions"]) == 1

    checker_failure = workflow_loop.run_loop(
        [{"type": "never"}],
        lambda _action: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    assert checker_failure["cancelled"] is True
    assert checker_failure["iterations"] == []


def test_loop_rejects_unbounded_actions_and_invalid_dispatch_results():
    too_many = [{"type": "probe"}] * (workflow_loop.MAX_LOOP_ACTIONS + 1)
    try:
        workflow_loop.run_loop(too_many, lambda _action: {"ok": True})
    except ValueError as exc:
        assert "action loop limit" in str(exc)
    else:
        raise AssertionError("expected action-count rejection")

    invalid = workflow_loop.run_loop(
        [{"type": "probe"}], lambda _action: "ERROR: not a typed result"
    )
    row = invalid["iterations"][0]["actions"][0]["result"]
    assert invalid["ok"] is False
    assert row["summary"] == "dispatcher returned an invalid result"


def test_service_does_not_send_new_cancel_keyword_to_legacy_runner_by_default():
    class StrictLegacyRunner:
        def run(
            self, actions, dispatch, *, max_iterations, stop_on_failure,
            stop_on_success, delay_seconds,
        ):
            return {"ok": True, "cancelled": False}

        def format(self, _result):
            return "done"

    repository = Repository()
    repository.save("demo", [{"type": "status"}])
    result = WorkflowService(repository, StrictLegacyRunner()).run(
        "demo", lambda _action: {"ok": True}
    )
    assert result.ok is True
