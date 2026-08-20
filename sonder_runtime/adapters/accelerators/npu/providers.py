"""Provider descriptors for the accelerator worker.

Pure data plus adapter helpers: this module never imports vendor runtimes
itself. The worker hands it the imported onnxruntime module (or the import
error) and gets back honest capability rows and a session provider chain.

DirectML is deliberately not mapped here: it is a GPU-class execution provider
and must never be reported as NPU acceleration. The "winml" descriptor exists
so detection can say clearly why it is not runtime-callable yet.
"""
from __future__ import annotations

import sys

import sonder_runtime.adapters.accelerators.npu.contract as npu_contract


# Provider id -> onnxruntime execution-provider name.
PROVIDER_EPS = {
    "vitisai": "VitisAIExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "qnn": "QNNExecutionProvider",
    "cpu": "CPUExecutionProvider",
}

SIMULATOR_EP = "CPUSimulator"

_VENDOR_LABELS = {
    "vitisai": "AMD VitisAI (effective target unverified)",
    "openvino": "Intel OpenVINO NPU",
    "qnn": "Qualcomm QNN",
    "winml": "Windows ML",
    "edgetpu": "Google Coral Edge TPU (TPU-class)",
    "hailo": "Hailo-8/8L (TPU-class)",
    "cpu": "onnxruntime CPU reference",
    "cpu-sim": "deterministic CPU simulator",
}

# TPU-class descriptors: why each is present but not runtime-callable here.
# Neither vendor ships an onnxruntime execution provider, so the existing
# resolver can never select them; detection reports the honest reason.
_TPU_REASONS = {
    "edgetpu": (
        "TPU-class M.2/USB accelerator; runs TFLite INT8 graphs via "
        "libedgetpu/pycoral, not an onnxruntime execution provider. Its small "
        "on-chip SRAM also cannot hold a transformer embedding model, so it is "
        "not a target for this utility path"
    ),
    "hailo": (
        "TPU-class M.2 accelerator; runs HEF graphs compiled by the Hailo SDK "
        "and executed through HailoRT, not an onnxruntime execution provider"
    ),
}


def provider_label(provider_id) -> str:
    return _VENDOR_LABELS.get(provider_id, provider_id)


def detect_providers(ort_module, ort_error="", test_hooks=False) -> list:
    """Describe registration only; successful loads establish readiness.

    An EP name does not prove hardware, device selection, or model
    compatibility.  The broker promotes rows to runtime-ready only after an
    exclusive compatible session has actually loaded.
    """
    available = []
    if ort_module is not None:
        try:
            available = list(ort_module.get_available_providers())
        except Exception as exc:  # vendor call; keep detection alive
            ort_error = ort_error or npu_contract.sanitize_error(exc, 160)
    rows = []
    for provider_id in npu_contract.PROVIDER_IDS:
        row = {
            "id": provider_id,
            "label": provider_label(provider_id),
            "registered": False,
            "detected": False,
            "runtime_ready": False,
            "ep": PROVIDER_EPS.get(provider_id, ""),
            "reason": "",
        }
        if provider_id == "cpu-sim":
            row.update(
                registered=bool(test_hooks),
                ep=SIMULATOR_EP,
                reason=(
                    "test-only stdlib deterministic simulator"
                    if test_hooks else "disabled outside SONDER_NPU_TEST_HOOKS"
                ),
            )
        elif provider_id in _TPU_REASONS:
            # Never registered/runtime-ready: no onnxruntime EP exists, so the
            # resolver cannot select it and the utility path stays on its
            # existing local fallback.
            row["reason"] = _TPU_REASONS[provider_id]
        elif provider_id == "winml":
            row.update(
                reason=(
                    "%s; no supported Python runtime path; DirectML is GPU-class "
                    "and is not claimed as an NPU"
                    % ("Windows descriptor" if sys.platform == "win32" else "not Windows")
                ),
            )
        elif ort_module is None:
            row["reason"] = (
                "onnxruntime not installed%s"
                % (
                    (": %s" % npu_contract.sanitize_error(ort_error, 160))
                    if ort_error else ""
                )
            )
        elif row["ep"] in available:
            row.update(
                registered=True,
                reason="execution provider registered",
            )
        else:
            row["reason"] = "execution provider not in this onnxruntime build"
        rows.append(row)
    return rows


def resolve_provider(manifest, provider_rows) -> tuple:
    """Pick the first allowlisted runtime-ready provider.

    Returns ``(provider_id, ep_fallback, error)``. ``ep_fallback`` is True when
    the selected provider is not the manifest's first choice, so callers can
    report honest execution-provider fallback instead of implying NPU use.
    """
    registered = {
        row["id"] for row in provider_rows if row.get("registered")
    }
    allowlist = list(manifest.get("providers") or [])
    for provider_id in allowlist:
        if provider_id in registered:
            return provider_id, provider_id != allowlist[0], ""
    return "", False, (
        "no allowlisted provider is registered (requested: %s)"
        % ", ".join(allowlist)
    )


def provider_candidates(manifest, provider_rows) -> list:
    """Priority-ordered, allowlisted providers that are runtime-registered."""
    registered = {
        row["id"] for row in provider_rows if row.get("registered")
    }
    return [
        provider_id for provider_id in (manifest.get("providers") or [])
        if provider_id in registered
    ]


def provider_id_for_ep(ep_name) -> str:
    value = str(ep_name or "")
    if value == SIMULATOR_EP:
        return "cpu-sim"
    for provider_id, registered_name in PROVIDER_EPS.items():
        if value == registered_name:
            return provider_id
    return ""
