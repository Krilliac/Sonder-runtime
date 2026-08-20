from __future__ import annotations

from sonder_runtime.adapters.workflow_adapters import LegacyLoopRunner
from sonder_runtime.adapters.workflow_loop_runner import LoopRunnerAdapter


def test_legacy_loop_runner_is_the_canonical_adapter_alias():
    assert LegacyLoopRunner is LoopRunnerAdapter


def test_loop_runner_delegates_run_and_format(monkeypatch):
    calls = []

    class Loop:
        @staticmethod
        def run_loop(actions, dispatch, **options):
            calls.append(("run", actions, dispatch, options))
            return {"ok": True}

        @staticmethod
        def format_loop_result(result):
            calls.append(("format", result))
            return "formatted"

    monkeypatch.setattr(LoopRunnerAdapter, "_module", staticmethod(lambda: Loop))
    runner = LoopRunnerAdapter()
    dispatch = object()

    assert runner.run(["a"], dispatch, timeout=3) == {"ok": True}
    assert runner.format({"ok": True}) == "formatted"
    assert calls == [
        ("run", ["a"], dispatch, {"timeout": 3}),
        ("format", {"ok": True}),
    ]
