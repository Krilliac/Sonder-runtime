"""Windows lock-acquire behaviour of the minimized failure store.

On Windows, opening a lock file whose unlink is still pending raises
``PermissionError``; the store treats that as a held lock.  Elsewhere it is a
real permission problem and must propagate.  The module's own ``os`` and ``sys``
names are replaced with shims so no process-global state is patched.
"""
from __future__ import annotations

import os
import types

import pytest

from sonder_runtime.adapters import evaluation_failure_corpus as corpus
from sonder_runtime.adapters.evaluation_failure_corpus import JsonMinimizedFailureStore
from sonder_runtime.application.evaluation.divergence import (
    DivergenceError,
    minimize_failure,
)
from sonder_runtime.application.evaluation.trajectory_replay import (
    TrajectoryRecord,
    TrajectoryStep,
)


def _failure():
    steps = tuple(TrajectoryStep(index, {"x": index}, {"y": index * 2}) for index in range(4))
    expected = TrajectoryRecord.from_steps("lock-win32", steps)

    def candidate():
        return lambda request: {"y": -1 if request["x"] == 2 else request["x"] * 2}

    return minimize_failure(expected, candidate)


def _patch_platform(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(corpus, "sys", types.SimpleNamespace(platform=platform))


def _patch_open(monkeypatch, failures: int | None) -> list[int]:
    """Make the module's ``os.open`` raise PermissionError ``failures`` times
    (``None`` means always); later calls reach the real ``os.open``."""
    calls: list[int] = []
    real_open = os.open

    def fake_open(*args, **kwargs):
        calls.append(1)
        if failures is None or len(calls) <= failures:
            raise PermissionError(13, "Permission denied")
        return real_open(*args, **kwargs)

    shim = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if not name.startswith("__")})
    shim.open = fake_open
    monkeypatch.setattr(corpus, "os", shim)
    return calls


def test_win32_permission_error_is_a_held_lock_and_acquire_retries(tmp_path, monkeypatch) -> None:
    _patch_platform(monkeypatch, "win32")
    calls = _patch_open(monkeypatch, failures=1)
    store = JsonMinimizedFailureStore(tmp_path / "failures", lock_timeout_seconds=5)
    failure = _failure()

    digest = store.retain(failure)

    assert len(calls) >= 2
    assert store.load(digest) == failure
    assert not (tmp_path / "failures" / ".lock").exists()


def test_non_win32_permission_error_propagates(tmp_path, monkeypatch) -> None:
    _patch_platform(monkeypatch, "linux")
    calls = _patch_open(monkeypatch, failures=None)
    store = JsonMinimizedFailureStore(tmp_path, lock_timeout_seconds=5)

    with pytest.raises(PermissionError), store._locked():
        pytest.fail("the lock must not be acquired")

    assert len(calls) == 1


def test_win32_permission_error_on_every_attempt_times_out(tmp_path, monkeypatch) -> None:
    _patch_platform(monkeypatch, "win32")
    calls = _patch_open(monkeypatch, failures=None)
    store = JsonMinimizedFailureStore(tmp_path, lock_timeout_seconds=0.1)

    with pytest.raises(DivergenceError, match="held by another writer") as caught, store._locked():
        pytest.fail("the lock must not be acquired")
    assert isinstance(caught.value.__cause__, PermissionError)

    assert len(calls) >= 2
