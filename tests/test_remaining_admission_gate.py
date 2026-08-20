import pytest

from sonder_runtime.application.operations.admission_gate import (
    AdmissionClosed,
    RuntimeAdmissionGate,
)


def test_gate_accepts_before_stop_and_rejects_after_stop() -> None:
    gate = RuntimeAdmissionGate()
    gate.admit()
    assert gate.stop_admission("upgrade")
    assert not gate.stop_admission("second reason")
    with pytest.raises(AdmissionClosed, match="upgrade"):
        gate.admit()
    assert gate.snapshot().accepted == 1
    assert gate.snapshot().rejected == 1


def test_gate_requires_a_reason_and_preserves_first_reason() -> None:
    gate = RuntimeAdmissionGate()
    with pytest.raises(ValueError):
        gate.stop_admission(" ")
    gate.stop_admission("shutdown")
    with pytest.raises(AdmissionClosed, match="shutdown"):
        gate.admit()

