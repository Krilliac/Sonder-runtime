"""Canonical adapter for the packaged saved-workflow repository."""
from __future__ import annotations


class WorkflowRepositoryAdapter:
    """Implement the saved-workflow port over the packaged workflow store."""

    @staticmethod
    def _module():
        from .filesystem import workflow_store

        return workflow_store

    def ensure(self):
        return self._module().ensure_workflows()

    def save(self, name, actions, description=""):
        return self._module().save_workflow(name, actions, description)

    def get(self, name):
        return self._module().get_workflow(name)

    def delete(self, name):
        return self._module().delete_workflow(name)

    def normalize_name(self, name):
        return self._module().normalize_name(name)

    def format(self, workflows):
        return self._module().format_workflows(workflows)
