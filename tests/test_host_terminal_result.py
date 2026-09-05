from dataclasses import replace
import pytest

from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
from sonder_runtime.adapters.host_terminal_result import TerminalResultCodec
from sonder_runtime.application.ports.delegated_verification import PreparedVerification, VerificationVerdict
from sonder_runtime.application.ports.lane_continuation import ProjectionBinding, seal_projection, open_projection


def setup(root, output="done"):
    binding = ProjectionBinding("continuation", "principal", "run", "conversation", "parent",
                                1, "verification", "a" * 64, (str(root),), 1)
    original_codec = TerminalProjectionCodec()
    original = original_codec.capture(binding=binding, ledger=HostObservationLedger(project_scope=str(root)),
        output=output, terminal_class="NORMAL", blockers=(), terminal_receipt_id="receipt")
    prepared = PreparedVerification("verification", "parent", "principal", 1, 7,
        (("child", 2, 3),), (str(root),), (), "context", "a" * 64)
    verdict = VerificationVerdict(True, "CERTIFIED", "verification", 7, "parent", 1,
                                   (str(root),), (("child", 2, 3),))
    state = {"original": original, "verdict": verdict, "calls": 0}
    def load(key):
        assert key == binding
        return state["original"]
    def verify(key):
        assert key == binding
        state["calls"] += 1
        return state["verdict"]
    codec = TerminalResultCodec(original_codec=original_codec, load_original=load,
        prepared_verification=lambda key: prepared, current_verdict=verify,
        certificate=lambda key: {"id": "verification", "bundle": prepared.approval_payload(),
                                 "cleanup_proofs": [], "after_manifest_digest": "b" * 64})
    return codec, original, binding, state


def test_result_uses_fresh_verdict_and_exact_original(tmp_path):
    codec, original, binding, state = setup(tmp_path, "original output")
    result = codec.capture(original)
    assert state["calls"] == 1
    assert result.output == "original output"
    assert result.valid is True
    assert len(codec.certificate_digest(result)) == 64
    result_binding = replace(binding, revision=2)
    sealed = seal_projection(codec, result, result_binding)
    restored = open_projection(codec, sealed, result_binding)
    assert restored.output == result.output
    assert state["calls"] == 1  # persisted historical result replay is not a new check


def test_child_certificate_cannot_erase_cancelled_parent(tmp_path):
    codec, original, _, _ = setup(tmp_path, "CANCELLED by host")
    result = codec.capture(original)
    assert result.valid is False
    assert result.output == "CANCELLED by host"


@pytest.mark.parametrize("change", [dict(valid=False), dict(valid=1), dict(generation=8),
                                   dict(children=()), dict(certificate_id="other")])
def test_stale_or_malformed_child_verdict_cannot_publish(tmp_path, change):
    codec, original, _, state = setup(tmp_path)
    state["verdict"] = replace(state["verdict"], **change)
    with pytest.raises(PermissionError):
        codec.capture(original)
