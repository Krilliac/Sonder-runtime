"""Canonical adapter for the packaged workflow loop engine."""
from __future__ import annotations

from ..application.ports.specialized_lifecycle import CleanupResult


class LoopRunnerAdapter:
    """Implement the workflow loop port over the packaged loop engine."""

    @staticmethod
    def _module():
        from ..application.workflows import loop

        return loop

    def run(self, actions, dispatch, **options):
        return self._module().run_loop(actions, dispatch, **options)

    def format(self, result):
        return self._module().format_loop_result(result)

    def cleanup(self, timeout=None):
        """The packaged loop owns no external resources after it returns."""
        return CleanupResult(
            provider_id="workflow-loop",
            quiescent=True,
            resources_released=True,
            detail="bounded loop execution has returned",
        )
