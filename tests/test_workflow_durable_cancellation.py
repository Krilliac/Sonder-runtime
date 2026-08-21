from sonder_runtime.adapters.workflow_loop_runner import LoopRunnerAdapter
from sonder_runtime.application.loop.durable_control import DurableLoopControl
from sonder_runtime.application.ports.specialized_lifecycle import CleanupResult
from sonder_runtime.application.workflows import WorkflowService


class Repository:
    def __init__(self):
        self.workflow = {"actions": [{"type": "probe"}], "description": "demo"}

    def get(self, name):
        return self.workflow if name == "demo" else None

    def normalize_name(self, name):
        return name.strip().lower()


class Runner:
    def __init__(self, cleanup):
        self.cleanup_result = cleanup
        self.cancel_check = None

    def run(self, actions, dispatch, **options):
        self.cancel_check = options["cancel_check"]
        return {
            "ok": False,
            "cancelled": self.cancel_check(),
            "iterations": [],
        }

    def format(self, result):
        return "loop status: cancelled"

    def cleanup(self, timeout):
        return self.cleanup_result


def test_workflow_run_uses_durable_child_and_reports_complete_cleanup():
    control = DurableLoopControl()
    runner = Runner(CleanupResult("workflow-loop", True, True, "released"))
    service = WorkflowService(Repository(), runner, loop_control=control)

    result = service.run("demo", lambda _action: {"ok": True}, cancel_check=lambda: True)

    assert result.ok is False
    assert result.error_code == "CANCELLED"
    evidence = result.evidence["workflow_cancellation"]
    assert evidence["cancelled"] is True
    assert evidence["cleanup_complete"] is True
    assert evidence["reports"][0]["resources_released"] is True
    assert len(tuple(control.cancellation.root.children())) == 1


def test_workflow_run_does_not_claim_clean_cancellation_when_cleanup_is_incomplete():
    control = DurableLoopControl()
    runner = Runner(CleanupResult("workflow-loop", False, False, "still active"))
    service = WorkflowService(Repository(), runner, loop_control=control)

    result = service.run("demo", lambda _action: {"ok": True}, cancel_check=lambda: True)

    assert result.ok is False
    assert result.error_code == "CLEANUP_INCOMPLETE"
    assert result.evidence["workflow_cancellation"]["cleanup_complete"] is False


def test_loop_runner_cleanup_is_a_complete_bounded_result():
    result = LoopRunnerAdapter().cleanup(timeout=1)
    assert result == CleanupResult(
        "workflow-loop", True, True, "bounded loop execution has returned"
    )
