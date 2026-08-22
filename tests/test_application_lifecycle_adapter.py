"""Regression coverage for the packaged application lifecycle adapter."""
from __future__ import annotations

import threading

from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
from sonder_runtime.bootstrap import app as bootstrap_app


def test_lifecycle_lazily_caches_and_resets() -> None:
    calls: list[int] = []
    lifecycle = ApplicationLifecycle(lambda: calls.append(1) or object())

    first = lifecycle.get()
    assert lifecycle.get() is first
    assert calls == [1]

    lifecycle.reset()
    assert lifecycle.get() is not first
    assert calls == [1, 1]


def test_lifecycle_builds_once_under_concurrent_access() -> None:
    calls: list[int] = []
    lifecycle = ApplicationLifecycle(lambda: calls.append(1) or object())
    results: list[object] = []

    threads = [threading.Thread(target=lambda: results.append(lifecycle.get())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert len({id(result) for result in results}) == 1


def test_bootstrap_compatibility_functions_use_packaged_lifecycle(monkeypatch) -> None:
    bootstrap_app.reset_for_tests()
    sentinel = object()
    monkeypatch.setattr(bootstrap_app, "build_application", lambda: sentinel)

    assert bootstrap_app.default_app() is sentinel
    assert bootstrap_app.default_app() is sentinel

    bootstrap_app.reset_for_tests()
    assert bootstrap_app.default_app() is sentinel
    bootstrap_app.reset_for_tests()
