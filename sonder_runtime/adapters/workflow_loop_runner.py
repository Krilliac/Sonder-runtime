"""Canonical adapter for the packaged workflow loop engine."""
from __future__ import annotations


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
