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

import npu_contract


# Provider id -> onnxruntime execution-provider name.
PROVIDER_EPS = {
    "vitisai": "VitisAIExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "qnn": "QNNExecutionProvider",
    "cpu": "CPUExecutionProvider",
}

SIMULATOR_EP = "CPUSimulator"

_VENDOR_LABELS = {
    "vitisai": "AMD VitisAI / Ryzen AI",
    "openvino": "Intel OpenVINO NPU",
    "qnn": "Qualcomm QNN",
    "winml": "Windows ML",
    "cpu": "onnxruntime CPU reference",
    "cpu-sim": "deterministic CPU simulator",
}


def provider_label(provider_id) -> str:
    return _VENDOR_LABELS.get(provider_id, provider_id)


def detect_providers(ort_module, ort_error="") -> list:
    """Describe every known provider honestly for this process."""
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
            "detected": False,
            "runtime_ready": False,
            "ep": PROVIDER_EPS.get(provider_id, ""),
            "reason": "",
        }
        if provider_id == "cpu-sim":
            row.update(
                detected=True,
                runtime_ready=True,
                ep=SIMULATOR_EP,
                reason="stdlib deterministic simulator",
            )
        elif provider_id == "winml":
            row.update(
                detected=sys.platform == "win32",
                runtime_ready=False,
                reason=(
                    "no supported Python runtime path; DirectML is GPU-class "
                    "and is not claimed as an NPU"
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
                detected=True,
                runtime_ready=True,
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
    ready = {
        row["id"] for row in provider_rows if row.get("runtime_ready")
    }
    allowlist = list(manifest.get("providers") or [])
    for provider_id in allowlist:
        if provider_id in ready:
            return provider_id, provider_id != allowlist[0], ""
    return "", False, (
        "no allowlisted provider is runtime-ready (requested: %s)"
        % ", ".join(allowlist)
    )


def provider_candidates(manifest, provider_rows) -> list:
    """Priority-ordered, allowlisted providers that are runtime-registered."""
    ready = {
        row["id"] for row in provider_rows if row.get("runtime_ready")
    }
    return [
        provider_id for provider_id in (manifest.get("providers") or [])
        if provider_id in ready
    ]


def provider_id_for_ep(ep_name) -> str:
    value = str(ep_name or "")
    if value == SIMULATOR_EP:
        return "cpu-sim"
    for provider_id, registered_name in PROVIDER_EPS.items():
        if value == registered_name:
            return provider_id
    return ""
