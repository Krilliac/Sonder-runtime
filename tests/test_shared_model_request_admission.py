"""Cross-process physical-send admission shares durable, bounded capacity."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission

_PROCESS = """
import json
import os
import sys
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission

policy = HostModelRequestAdmission.from_environ(
    {"SONDER_HOME": sys.argv[1], "SONDER_MODEL_REQUEST_BURST": "1",
     "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
    clock=lambda: float(sys.argv[2]),
)
print("ready", flush=True)
sys.stdin.readline()
result = policy.try_acquire()
print(json.dumps({"allowed": result.allowed, "retry_after": result.retry_after}), flush=True)
"""


def _process(home: Path, now: float):
    return subprocess.Popen(
        [sys.executable, "-c", _PROCESS, str(home), str(now)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "SONDER_HOME": str(home)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process, *, released=False):
    stdout, stderr = process.communicate(None if released else "\n", timeout=10)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


def test_two_processes_compete_for_one_physical_send_and_restart_keeps_charge(tmp_path):
    first, second = _process(tmp_path, 100), _process(tmp_path, 100)
    try:
        assert first.stdout.readline().strip() == "ready"
        assert second.stdout.readline().strip() == "ready"
        for process in (first, second):
            process.stdin.write("\n")
            process.stdin.flush()
        outcomes = [_finish(first, released=True), _finish(second, released=True)]
        assert sorted(item["allowed"] for item in outcomes) == [False, True]
        assert next(item["retry_after"] for item in outcomes if not item["allowed"]) > 0
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    restarted = _process(tmp_path, 100)
    assert restarted.stdout.readline().strip() == "ready"
    assert _finish(restarted)["allowed"] is False
    refilled = _process(tmp_path, 160)
    assert refilled.stdout.readline().strip() == "ready"
    assert _finish(refilled)["allowed"] is True


def test_backwards_clock_and_policy_change_never_mint_new_tokens(tmp_path):
    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "2",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "60"}
    first = HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0)
    assert first.try_acquire().allowed
    assert first.try_acquire().allowed
    assert not HostModelRequestAdmission.from_environ(env, clock=lambda: 99.0).try_acquire().allowed
    expanded = HostModelRequestAdmission.from_environ(
        {**env, "SONDER_MODEL_REQUEST_BURST": "3"}, clock=lambda: 100.0,
    )
    assert not expanded.try_acquire().allowed
    assert HostModelRequestAdmission.from_environ(
        {**env, "SONDER_MODEL_REQUEST_BURST": "3"}, clock=lambda: 101.0,
    ).try_acquire().allowed


def test_unavailable_admission_store_refuses_instead_of_resetting_budget(tmp_path):
    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"}
    policy = HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0)
    assert policy.try_acquire().allowed
    path = tmp_path / "model-request-admission.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_request_rate SET tokens = -1 WHERE id = 1")
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        policy.try_acquire()


def test_removed_bucket_row_cannot_reset_burst_in_running_or_restarted_process(tmp_path):
    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"}
    policy = HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0)
    assert policy.try_acquire().allowed
    with sqlite3.connect(tmp_path / "model-request-admission.sqlite3") as connection:
        connection.execute("DELETE FROM model_request_rate WHERE id=1")
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        policy.try_acquire()
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        HostModelRequestAdmission.from_environ(env, clock=lambda: 160.0).try_acquire()


def test_preexisting_empty_store_is_not_treated_as_a_fresh_bucket(tmp_path):
    (tmp_path / "model-request-admission.sqlite3").touch()
    policy = HostModelRequestAdmission.from_environ(
        {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
         "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
    )
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        policy.try_acquire()


def test_removed_database_cannot_reset_a_restarted_bucket(tmp_path):
    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"}
    assert HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0).try_acquire().allowed
    (tmp_path / "model-request-admission.sqlite3").unlink()
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0).try_acquire()


def test_removed_initialization_marker_refuses_existing_store(tmp_path):
    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"}
    assert HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0).try_acquire().allowed
    (tmp_path / "model-request-admission.sqlite3.lock").unlink()
    with pytest.raises(RuntimeError, match="model request admission unavailable"):
        HostModelRequestAdmission.from_environ(env, clock=lambda: 100.0).try_acquire()


def test_mixed_live_configs_converge_on_strictest_burst_and_refill(tmp_path):
    def policy(burst, rate, at):
        return HostModelRequestAdmission.from_environ(
            {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": str(burst),
             "SONDER_MODEL_REQUESTS_PER_MINUTE": str(rate)}, clock=lambda: at,
        )

    high = policy(3, 120, 100.0)
    assert high.try_acquire().allowed
    low = policy(1, 1, 100.0)
    assert low.try_acquire().allowed  # Tightening caps the remaining 2 at 1.
    assert not high.try_acquire().allowed  # The old high-rate owner cannot widen it.
    assert not policy(3, 120, 101.0).try_acquire().allowed
    assert policy(3, 120, 160.0).try_acquire().allowed
    with sqlite3.connect(tmp_path / "model-request-admission.sqlite3") as connection:
        stored = connection.execute(
            "SELECT burst,requests_per_minute FROM model_request_rate WHERE id=1",
        ).fetchone()
    assert stored == (1, 1)


def test_locked_admission_store_refuses_with_bounded_wait(tmp_path):
    import time

    env = {"SONDER_HOME": str(tmp_path), "SONDER_MODEL_REQUEST_BURST": "1",
           "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"}
    now = [100.0]
    policy = HostModelRequestAdmission.from_environ(env, clock=lambda: now[0])
    assert policy.try_acquire().allowed
    with sqlite3.connect(tmp_path / "model-request-admission.sqlite3") as connection:
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="model request admission unavailable"):
            policy.try_acquire()
        assert time.monotonic() - started < 2.5
    now[0] = 160.0
    assert policy.try_acquire().allowed


def test_disabled_policy_creates_no_shared_store(tmp_path):
    admission = HostModelRequestAdmission.from_environ({"SONDER_HOME": str(tmp_path)})
    assert admission.try_acquire() is None
    assert not (tmp_path / "model-request-admission.sqlite3").exists()


def test_typed_home_is_the_host_authority_over_stale_environment(monkeypatch, tmp_path):
    from sonder_runtime.platform import paths

    typed_home = tmp_path / "typed"
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "old"))
    monkeypatch.setenv("SONDER_MODEL_REQUEST_BURST", "1")
    monkeypatch.setenv("SONDER_MODEL_REQUESTS_PER_MINUTE", "1")
    paths.configure_home(typed_home)
    assert HostModelRequestAdmission.from_environ(os.environ).try_acquire().allowed
    assert (typed_home / "model-request-admission.sqlite3").exists()
    assert not (tmp_path / "old" / "model-request-admission.sqlite3").exists()


def test_host_singleton_uses_typed_home_bound_after_module_import(tmp_path):
    old_home, typed_home = tmp_path / "old", tmp_path / "typed"
    program = """
import sonder_runtime.adapters.model_request_admission as admission
from sonder_runtime.platform import paths
import sys
paths.configure_home(sys.argv[1])
assert admission.host_model_request_admission().try_acquire().allowed
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(typed_home)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "SONDER_HOME": str(old_home),
             "SONDER_MODEL_REQUEST_BURST": "1",
             "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (typed_home / "model-request-admission.sqlite3").exists()
    assert not (old_home / "model-request-admission.sqlite3").exists()
