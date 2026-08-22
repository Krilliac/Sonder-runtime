"""ProcessProbeAdapter over the process_liveness adapter."""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.process_probe import ProcessProbeAdapter
from sonder_runtime.application.ports.process_probe import (
    ProbeResult,
    ProcessIdentity,
)
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def probe():
    return ProcessProbeAdapter()


def test_identity_of_self_is_present(probe):
    ident = probe.identity(os.getpid())
    if ident is None:
        pytest.skip("process_liveness could not fingerprint this process")
    assert ident.pid == os.getpid()
    assert ident.fingerprint  # opaque instance identity string


def test_same_live_process_is_not_dead_for_self(probe):
    ident = probe.identity(os.getpid())
    if ident is None:
        pytest.skip("process_liveness could not fingerprint this process")
    # The live self-process must never report DEAD (ALIVE, or UNKNOWN if the
    # platform probe is degraded — never a false death that could steal a claim).
    assert probe.is_same_live_process(ident) in (ProbeResult.ALIVE, ProbeResult.UNKNOWN)


def test_wrong_fingerprint_at_live_pid_is_dead(probe):
    ident = probe.identity(os.getpid())
    if ident is None or not ident.fingerprint:
        pytest.skip("process_liveness could not fingerprint this process")
    imposter = ProcessIdentity(
        pid=os.getpid(), started_at=0.0, fingerprint=ident.fingerprint + "-wrong"
    )
    # A different identity at the same live PID is a dead *expected* owner.
    assert probe.is_same_live_process(imposter) is ProbeResult.DEAD


def test_bogus_pid_has_no_identity(probe):
    # A PID that cannot exist yields no identity (dead), never a false alive.
    assert probe.identity(2_147_483_000) is None


def test_application_exposes_process_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    bootstrap_app.reset_for_tests()
    app = bootstrap_app.build_application()
    assert isinstance(app.process_probe, ProcessProbeAdapter)
    bootstrap_app.reset_for_tests()
