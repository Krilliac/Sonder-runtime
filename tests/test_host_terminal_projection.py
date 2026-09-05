import json

import pytest

from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
from sonder_runtime.application.ports.lane_continuation import (
    ProjectionBinding, open_projection, seal_projection,
)


def binding(root):
    return ProjectionBinding("continuation", "principal", "run", "conversation",
                             "parent", 1, "verification", "a" * 64, (str(root),), 1)


def make(codec, root, output="done", blockers=()):
    return codec.capture(binding=binding(root), ledger=HostObservationLedger(project_scope=str(root)),
                         output=output, terminal_class="NORMAL", blockers=blockers,
                         terminal_receipt_id="terminal-receipt")


def test_fresh_host_codec_restores_exact_output_and_binding(tmp_path):
    first = TerminalProjectionCodec()
    original = make(first, tmp_path, "original output\n")
    sealed = seal_projection(first, original, binding(tmp_path))
    second = TerminalProjectionCodec()
    restored = open_projection(second, sealed, binding(tmp_path))
    assert restored.output == "original output\n"
    assert second.binding(restored) == binding(tmp_path)
    assert second.parent_effects_valid(restored) is True


@pytest.mark.parametrize("prefix", ["ERROR", "CANCELLED", "EVIDENCE_REQUIRED", "VALIDATION_FAILED"])
def test_failure_prefix_cannot_be_declared_normal(tmp_path, prefix):
    codec = TerminalProjectionCodec()
    restored = codec.decode(codec.encode(make(codec, tmp_path, "  " + prefix + ": failed")))
    assert restored.terminal_class == prefix
    assert codec.parent_effects_valid(restored) is False


def test_completion_blockers_remain_failure_after_restart(tmp_path):
    codec = TerminalProjectionCodec()
    projection = make(codec, tmp_path, blockers=("required-call-failed",))
    assert codec.parent_effects_valid(codec.decode(codec.encode(projection))) is False


@pytest.mark.parametrize("output,terminal", [
    ("CANCELLED", "CANCELLED"),
    ("  CANCELLED by host", "CANCELLED"),
    ("EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED"),
    (" EVIDENCE_REQUIRED missing tool evidence", "EVIDENCE_REQUIRED"),
])
def test_host_no_colon_failure_markers_survive_restart(tmp_path, output, terminal):
    codec = TerminalProjectionCodec()
    projection = codec.decode(codec.encode(make(codec, tmp_path, output)))
    assert projection.terminal_class == terminal
    assert codec.parent_effects_valid(projection) is False


def test_foreign_issuer_and_raw_dictionary_are_refused(tmp_path):
    first, second = TerminalProjectionCodec(), TerminalProjectionCodec()
    original = make(first, tmp_path)
    with pytest.raises(PermissionError):
        second.encode(original)
    with pytest.raises(PermissionError):
        first.encode({"parent_effects_valid": True})


def test_output_digest_mismatch_is_refused(tmp_path):
    codec = TerminalProjectionCodec()
    data = json.loads(codec.encode(make(codec, tmp_path)))
    data["output"] = "replacement"
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        codec.decode(payload)


def test_oversized_output_is_unavailable_without_immutable_blob(tmp_path):
    codec = TerminalProjectionCodec()
    with pytest.raises(ValueError):
        make(codec, tmp_path, "x" * 16385)
