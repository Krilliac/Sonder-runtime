"""TPU-class accelerator descriptors (Coral Edge TPU, Hailo).

Neither vendor ships an onnxruntime execution provider, so these are
descriptor-only in exactly the way ``winml`` is: detection reports them
honestly instead of staying silent, and the resolver can never select one.
"""
from __future__ import annotations

import npu_contract
import npu_providers


def _rows():
    # No onnxruntime available: the common case on a machine with a TPU stick.
    return npu_providers.detect_providers(None, "onnxruntime not installed")


def test_tpu_ids_are_declared_and_classified():
    for pid in ("edgetpu", "hailo"):
        assert pid in npu_contract.PROVIDER_IDS
        assert pid in npu_contract.TPU_PROVIDER_IDS
    # NPU-class providers must not be mislabeled as TPU-class.
    for pid in ("vitisai", "openvino", "qnn"):
        assert pid not in npu_contract.TPU_PROVIDER_IDS


def test_tpu_rows_are_present_but_never_ready():
    rows = {row["id"]: row for row in _rows()}
    for pid in ("edgetpu", "hailo"):
        row = rows[pid]
        assert row["registered"] is False
        assert row["runtime_ready"] is False
        assert row["ep"] == ""  # no onnxruntime execution provider exists
        assert row["reason"], "a descriptor-only provider must explain itself"


def test_tpu_reason_names_the_real_runtime_path():
    rows = {row["id"]: row for row in _rows()}
    assert "tflite" in rows["edgetpu"]["reason"].lower()
    assert "hailort" in rows["hailo"]["reason"].lower()
    # Both must state they are not an onnxruntime execution provider.
    for pid in ("edgetpu", "hailo"):
        assert "onnxruntime execution provider" in rows[pid]["reason"].lower()


def test_tpu_can_never_be_resolved_as_a_runtime_provider():
    rows = _rows()
    provider_id, fallback, error = npu_providers.resolve_provider(
        {"providers": ["edgetpu", "hailo"]}, rows
    )
    assert provider_id == ""
    assert fallback is False
    assert error  # the caller is told why, and falls back to local behavior
    assert npu_providers.provider_candidates(
        {"providers": ["edgetpu", "hailo"]}, rows
    ) == []


def test_tpu_has_a_human_label_marking_the_hardware_class():
    for pid in ("edgetpu", "hailo"):
        assert "TPU-class" in npu_providers.provider_label(pid)


def test_adding_tpu_ids_did_not_disturb_existing_providers():
    rows = {row["id"]: row for row in _rows()}
    # every previously-supported id still described
    for pid in ("vitisai", "openvino", "qnn", "winml", "cpu", "cpu-sim"):
        assert pid in rows
    # and no execution-provider id is accidentally shared with a TPU row
    assert npu_providers.provider_id_for_ep("VitisAIExecutionProvider") == "vitisai"
    assert npu_providers.provider_id_for_ep("") == ""
