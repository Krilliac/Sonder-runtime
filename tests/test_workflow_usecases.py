import inspect

from sonder_runtime.application.ports.tool_executor import ToolResult
from sonder_runtime.application.workflows import WorkflowService, render_workflow_result


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
    def __init__(self, formatted="done"):
        self.formatted = formatted

    def run(self, actions, dispatch, **options):
        return {"ok": True, "actions": actions, "options": options}

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
