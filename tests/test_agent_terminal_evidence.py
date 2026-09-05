"""Restart evidence must retain order and independently recompute coverage."""

import pytest

from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger


def write(ledger, root):
    ledger.observe(
        tool="write_file", arguments={"path": str(root / "app.py")},
        observation="written", dispatched=True, success=True,
        dirty=True, mutation_records=({"tool": "write_file", "path": str(root / "app.py")},),
    )


def check(ledger, root, *, success=True, path=""):
    ledger.observe(tool="test_run", arguments={"root": str(root), "path": path},
                   observation="passed" if success else "failed", dispatched=True,
                   success=success, verifier=True)


def test_restart_recomputes_covering_verifier(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    write(ledger, tmp_path)
    check(ledger, tmp_path)
    restored = HostObservationLedger.restore(ledger.seal())
    assert restored.resolve().parent_effects_valid is True
    assert restored.resolve().dirty is True
    assert restored.resolve().verification_ok is True


def test_later_mutation_invalidates_earlier_verifier(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    check(ledger, tmp_path)
    write(ledger, tmp_path)
    assert HostObservationLedger.restore(ledger.seal()).resolve().parent_effects_valid is False


@pytest.mark.parametrize("success,path", [(False, ""), (True, "tests")])
def test_latest_failed_or_narrow_verifier_overrides_pass(tmp_path, success, path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    write(ledger, tmp_path)
    check(ledger, tmp_path)
    check(ledger, tmp_path, success=success, path=path)
    assert ledger.resolve().parent_effects_valid is False


def test_failed_dispatched_effect_still_requires_validation(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    ledger.observe(tool="workspace_run", arguments={}, observation="failed",
                   dispatched=True, success=False, dirty=True)
    assert ledger.resolve().parent_effects_valid is False


def test_observations_snapshot_arguments(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    write(ledger, tmp_path)
    args = {"root": str(tmp_path), "path": "tests"}
    ledger.observe(tool="test_run", arguments=args, observation="passed",
                   dispatched=True, success=True, verifier=True)
    args["path"] = ""
    assert ledger.resolve().parent_effects_valid is False


def test_overflow_poisoned_instead_of_silently_truncated(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    with pytest.raises(ValueError):
        ledger.observe(tool="test_run", arguments={}, observation="x" * 65536,
                       dispatched=True, success=True, verifier=True)
    with pytest.raises(ValueError):
        ledger.seal()


def test_noncanonical_or_unknown_policy_cannot_restore(tmp_path):
    ledger = HostObservationLedger(project_scope=str(tmp_path))
    sealed = ledger.seal()
    with pytest.raises(ValueError):
        HostObservationLedger.restore(b" " + sealed)
    with pytest.raises(ValueError):
        HostObservationLedger.restore(sealed.replace(b'"policy":1', b'"policy":2'))
