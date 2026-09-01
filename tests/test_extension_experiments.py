"""Focused tests for the bounded ephemeral EXT-006/007 experiment lifecycle."""

import sys

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost
from sonder_runtime.application.extensions.experiments import (
    EphemeralExperimentManager,
    ExperimentInvalidDefinition,
    ExperimentInvalidTransition,
    ExperimentStartupDenied,
    ExperimentState,
)


READY = 'import json,sys\nprint(json.dumps({"type":"ready"}), flush=True)\n'
SERVER = READY + 'for line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"id":r["id"],"ok":True}), flush=True)'


def _manager(allowed=True):
    def factory(definition, directory):
        return ExtensionHost(definition.argv, limits=definition.limits, cwd=directory)
    return EphemeralExperimentManager(lambda definition: allowed, host_factory=factory)


def test_define_inspect_start_stop_delete_is_deterministic_and_temporary():
    manager = _manager()
    try:
        defined = manager.define("trial-1", [sys.executable, "-c", SERVER], description="one shot")
        assert defined.state == ExperimentState.DEFINED
        assert defined.stats is None
        started = manager.start("trial-1")
        assert started.state == ExperimentState.RUNNING
        assert started.starts == 1
        assert started.stats is not None and started.stats.launches == 1
        stopped = manager.stop("trial-1")
        assert stopped.state == ExperimentState.STOPPED
        assert stopped.stops == 1
        deleted = manager.delete("trial-1")
        assert deleted.state == ExperimentState.DELETED
        assert not (manager.root / "trial-1").exists()
    finally:
        manager.close()


def test_start_requires_explicit_authority_and_never_starts_child_when_denied():
    manager = _manager(False)
    try:
        manager.define("denied", [sys.executable, "-c", SERVER])
        with pytest.raises(ExperimentStartupDenied):
            manager.start("denied")
        assert manager.inspect("denied").state == ExperimentState.DEFINED
    finally:
        manager.close()


def test_running_experiment_must_stop_before_delete_and_close_is_bounded_cleanup():
    manager = _manager()
    manager.define("cleanup", [sys.executable, "-c", SERVER])
    manager.start("cleanup")
    with pytest.raises(ExperimentInvalidTransition):
        manager.delete("cleanup")
    manager.close()
    assert not manager.root.exists()


def test_definitions_are_bounded_and_repeated_transitions_are_rejected():
    manager = _manager()
    try:
        with pytest.raises(ExperimentInvalidDefinition):
            manager.define("Bad ID", [sys.executable])
        with pytest.raises(ExperimentInvalidDefinition):
            manager.define("too-long", ["x" * 513])
        manager.define("stable", [sys.executable, "-c", SERVER])
        with pytest.raises(ExperimentInvalidTransition):
            manager.stop("stable")
        manager.start("stable")
        manager.stop("stable")
        with pytest.raises(ExperimentInvalidTransition):
            manager.start("stable")
        manager.delete("stable")
        with pytest.raises(ExperimentInvalidTransition):
            manager.delete("stable")
    finally:
        manager.close()


def test_delete_retries_a_transient_directory_handle_race(monkeypatch):
    import sonder_runtime.application.extensions.experiments as experiments

    manager = _manager()
    real_rmtree = experiments.shutil.rmtree
    calls = 0

    def transient(path, *, ignore_errors=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("transient child directory handle")
        return real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(experiments.shutil, "rmtree", transient)
    try:
        manager.define("retry-delete", [sys.executable, "-c", READY])
        assert manager.delete("retry-delete").state == ExperimentState.DELETED
        assert calls == 2
    finally:
        manager.close()
