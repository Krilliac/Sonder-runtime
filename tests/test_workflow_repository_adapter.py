from __future__ import annotations

from sonder_runtime.adapters.workflow_adapters import LegacyWorkflowRepository
from sonder_runtime.adapters.workflow_repository import WorkflowRepositoryAdapter


def test_legacy_workflow_repository_is_the_canonical_adapter_alias():
    assert LegacyWorkflowRepository is WorkflowRepositoryAdapter


def test_workflow_repository_delegates_to_packaged_store(monkeypatch):
    calls = []

    class Store:
        @staticmethod
        def ensure_workflows():
            calls.append(("ensure",))
            return ({"demo": {}}, "workflows.json")

        @staticmethod
        def save_workflow(name, actions, description):
            calls.append(("save", name, actions, description))
            return ({"actions": actions}, "workflows.json")

        @staticmethod
        def get_workflow(name):
            calls.append(("get", name))
            return {"actions": []}

        @staticmethod
        def delete_workflow(name):
            calls.append(("delete", name))
            return True, "workflows.json"

        @staticmethod
        def normalize_name(name):
            calls.append(("normalize", name))
            return name.lower()

        @staticmethod
        def format_workflows(workflows):
            calls.append(("format", workflows))
            return "formatted"

    monkeypatch.setattr(
        WorkflowRepositoryAdapter, "_module", staticmethod(lambda: Store)
    )
    repository = WorkflowRepositoryAdapter()

    assert repository.ensure() == ({"demo": {}}, "workflows.json")
    assert repository.save("Demo", [{"type": "probe"}], "desc") == (
        {"actions": [{"type": "probe"}]}, "workflows.json"
    )
    assert repository.get("Demo") == {"actions": []}
    assert repository.delete("Demo") == (True, "workflows.json")
    assert repository.normalize_name("Demo") == "demo"
    assert repository.format({"demo": {}}) == "formatted"
    assert calls == [
        ("ensure",),
        ("save", "Demo", [{"type": "probe"}], "desc"),
        ("get", "Demo"),
        ("delete", "Demo"),
        ("normalize", "Demo"),
        ("format", {"demo": {}}),
    ]


def test_bootstrap_composes_canonical_workflow_repository():
    from sonder_runtime.bootstrap import app

    assert app.WorkflowRepositoryAdapter is WorkflowRepositoryAdapter
