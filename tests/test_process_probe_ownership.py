"""Focused ownership tests for the packaged process identity policy."""

from sonder_runtime.adapters import process_liveness
from sonder_runtime.adapters.process_probe import ProcessProbeAdapter


def test_process_identity_is_the_canonical_fingerprint_policy(monkeypatch):
    monkeypatch.setattr(
        process_liveness,
        "probe_process",
        lambda pid: (process_liveness.PROCESS_ALIVE, "linux:boot:42"),
    )

    assert process_liveness.process_identity(321) == "linux:boot:42"


def test_process_probe_adapter_delegates_identity_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        process_liveness,
        "process_identity",
        lambda pid: calls.append(pid) or "windows:99",
    )

    identity = ProcessProbeAdapter().identity(321)

    assert identity is not None
    assert identity.pid == 321
    assert identity.fingerprint == "windows:99"
    assert calls == [321]
