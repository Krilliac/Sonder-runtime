"""Hardware capabilities and quantized inference planning.

This module is deliberately pure.  Platform adapters may supply observations,
but this policy never probes a driver, chooses a cloud endpoint, or claims a
backend is ready merely because a vendor string was detected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping


# Approximate resident weight size in decimal GB per billion parameters.  The
# values are planning envelopes, not a replacement for a provider's measured
# residency report.
QUANTIZATION_GB_PER_BILLION = {
    "q4": 0.62,
    "q4_k_m": 0.62,
    "q5": 0.75,
    "q5_k_m": 0.75,
    "q6": 0.86,
    "q8": 1.05,
    "q8_0": 1.05,
}

DEFAULT_30B_MODEL = "qwen3-coder:30b-a3b-q4_K_M"
DEFAULT_30B_QUANTIZATION = "Q4_K_M"
DEFAULT_30B_CONTEXT = 8192
DEFAULT_30B_RUNTIME_OVERHEAD_GB = 2.0
DEFAULT_30B_SAFETY_MARGIN_GB = 1.0


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _quant_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


@dataclass(frozen=True)
class HardwareCapabilityReport:
    """Normalized, conservative hardware capability projection."""

    platform: str = "unknown"
    architecture: str = "unknown"
    cpu_count: int | None = None
    system_ram_total_gb: float | None = None
    gpu_vendor: str = "unknown"
    gpu_name: str = ""
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None
    vram_is_live: bool = False
    cuda_available: bool = False
    rocm_available: bool = False
    compute_capability: str = ""
    tensor_cores: bool | None = None
    npu_detected: bool = False
    backend_candidates: tuple[str, ...] = ("cpu",)
    backend_readiness: tuple[tuple[str, str], ...] = ()
    quantization_formats: tuple[str, ...] = ("GGUF",)
    safe_gpu_offload: bool = False
    offload_reason: str = "backend readiness and fit are not measured"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuantizedModelProfile:
    """A reproducible model memory envelope used by the planner."""

    model: str
    total_params_b: float
    active_params_b: float
    quantization: str
    backend: str = "ollama"
    context_size: int = DEFAULT_30B_CONTEXT
    weight_gb: float = 0.0
    kv_cache_gb: float = 0.0
    runtime_overhead_gb: float = DEFAULT_30B_RUNTIME_OVERHEAD_GB
    safety_margin_gb: float = DEFAULT_30B_SAFETY_MARGIN_GB

    @property
    def required_gb(self) -> float:
        return round(
            self.weight_gb
            + self.kv_cache_gb
            + self.runtime_overhead_gb
            + self.safety_margin_gb,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "required_gb": self.required_gb,
        }


@dataclass(frozen=True)
class ModelExecutionPlan:
    """Safe execution recommendation with explicit fallback semantics."""

    model: str
    backend: str
    mode: str
    gpu_layers: str | int
    context_size: int
    required_gb: float
    available_vram_gb: float | None
    safe_gpu_offload: bool
    fallback_model: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tensor_cores(vendor: str, capability: str) -> bool | None:
    if str(vendor or "").casefold() != "nvidia":
        return None
    try:
        major = int(str(capability).split(".", 1)[0])
    except (TypeError, ValueError):
        return None
    return major >= 7


def build_hardware_capabilities(
    hardware: Mapping[str, Any],
    *,
    backend_readiness: Mapping[str, bool | None] | None = None,
) -> HardwareCapabilityReport:
    """Normalize an adapter hardware dictionary without inventing readiness."""
    readiness = backend_readiness or {}
    vendor = str(hardware.get("gpu_vendor") or "unknown").strip().lower()
    if vendor == "none":
        vendor = "unknown"
    cuda = hardware.get("cuda_available") is True
    rocm = hardware.get("rocm_available") is True
    candidates = ["cpu"]
    if cuda:
        candidates.append("cuda")
    if rocm:
        candidates.append("rocm")
    if readiness.get("ollama") is not False:
        candidates.append("ollama")
    statuses = tuple(
        (str(name), "ready" if value is True else "unavailable" if value is False else "unknown")
        for name, value in sorted(readiness.items())
    )
    vram_total = _positive(hardware.get("vram_total_gb") or hardware.get("vram_gb"))
    vram_free = _positive(hardware.get("vram_free_gb"))
    if vram_free is None and hardware.get("vram_availability_live") is True:
        # A live zero is meaningful pressure, not an invitation to estimate.
        try:
            vram_free = max(0.0, float(hardware.get("vram_free_gb")))
        except (TypeError, ValueError):
            vram_free = None
    live = hardware.get("vram_availability_live") is True and vram_free is not None
    safe_offload = bool(
        live
        and (readiness.get("cuda") is True or readiness.get("rocm") is True)
    )
    reason = (
        "measured backend readiness is available"
        if safe_offload
        else "backend readiness and fit are not measured"
    )
    return HardwareCapabilityReport(
        platform=str(hardware.get("platform") or "unknown"),
        architecture=str(hardware.get("architecture") or "unknown"),
        cpu_count=hardware.get("cpu_count") if isinstance(hardware.get("cpu_count"), int) else None,
        system_ram_total_gb=_positive(hardware.get("total_ram_gb") or hardware.get("system_ram_total_gb")),
        gpu_vendor=vendor,
        gpu_name=str(hardware.get("gpu_name") or ""),
        vram_total_gb=vram_total,
        vram_free_gb=vram_free,
        vram_is_live=live,
        cuda_available=cuda,
        rocm_available=rocm,
        compute_capability=str(hardware.get("compute_capability") or ""),
        tensor_cores=_tensor_cores(vendor, hardware.get("compute_capability") or ""),
        npu_detected=hardware.get("npu_detected") is True,
        backend_candidates=tuple(dict.fromkeys(candidates)),
        backend_readiness=statuses,
        safe_gpu_offload=safe_offload,
        offload_reason=reason,
    )


def quantized_model_profile(
    *,
    model: str = DEFAULT_30B_MODEL,
    total_params_b: float = 30.0,
    active_params_b: float = 3.0,
    quantization: str = DEFAULT_30B_QUANTIZATION,
    backend: str = "ollama",
    context_size: int = DEFAULT_30B_CONTEXT,
    runtime_overhead_gb: float = DEFAULT_30B_RUNTIME_OVERHEAD_GB,
    safety_margin_gb: float = DEFAULT_30B_SAFETY_MARGIN_GB,
) -> QuantizedModelProfile:
    """Build a Q4/Q5/etc. profile; reject malformed optimistic inputs."""
    total = _positive(total_params_b)
    active = _positive(active_params_b)
    if total is None or active is None or active > total:
        raise ValueError("model parameter counts must be positive and active <= total")
    try:
        context = int(context_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("context_size must be a positive integer") from exc
    if context < 1:
        raise ValueError("context_size must be a positive integer")
    key = _quant_key(quantization)
    per_billion = QUANTIZATION_GB_PER_BILLION.get(key)
    if per_billion is None:
        raise ValueError("unsupported planning quantization %r" % quantization)
    overhead = _positive(runtime_overhead_gb)
    safety = _positive(safety_margin_gb)
    if overhead is None or safety is None:
        raise ValueError("runtime overhead and safety margin must be positive")
    weight = round(total * per_billion, 2)
    # KV grows with context, but this remains a deliberately conservative
    # planning envelope; the live provider remains authoritative.
    kv = round(0.5 * context / 8192.0, 2)
    return QuantizedModelProfile(
        model=str(model).strip() or DEFAULT_30B_MODEL,
        total_params_b=total,
        active_params_b=active,
        quantization=str(quantization).strip(),
        backend=str(backend).strip().lower() or "ollama",
        context_size=context,
        weight_gb=weight,
        kv_cache_gb=kv,
        runtime_overhead_gb=overhead,
        safety_margin_gb=safety,
    )


def plan_model_execution(
    capabilities: HardwareCapabilityReport,
    profile: QuantizedModelProfile,
    *,
    fallback_model: str = "qwen3:14b",
) -> ModelExecutionPlan:
    """Select GPU-resident, hybrid, or CPU execution without overclaiming."""
    available = capabilities.vram_free_gb if capabilities.vram_is_live else None
    backend_ready = dict(capabilities.backend_readiness).get(profile.backend) == "ready"
    if available is not None and available >= profile.required_gb and backend_ready:
        return ModelExecutionPlan(
            profile.model, profile.backend, "gpu-resident", "auto",
            profile.context_size, profile.required_gb, available, True,
            fallback_model,
        )
    reported_ram = capabilities.system_ram_total_gb or 0.0
    if (
        available is not None
        and available > 0
        and reported_ram > 0
        and available + reported_ram >= profile.required_gb
    ):
        return ModelExecutionPlan(
            profile.model, profile.backend, "gpu+ram-hybrid", "auto",
            profile.context_size, profile.required_gb, available, False,
            fallback_model,
            ("weights plus KV/overhead exceed the measured free VRAM; expect lower throughput",
             "GPU offload is advisory until the selected backend reports readiness" if not backend_ready else
             "GPU offload remains advisory because the model is not fully resident"),
        )
    warnings = ["no measured safe GPU execution plan is available"]
    if available is not None and reported_ram > 0 and (
        available + reported_ram < profile.required_gb
    ):
        warnings.insert(
            0,
            "measured free VRAM plus reported system RAM is below the model envelope",
        )
    return ModelExecutionPlan(
        profile.model, "cpu", "cpu-fallback", 0, profile.context_size,
        profile.required_gb, available, False, fallback_model,
        tuple(warnings) + (
            "use the configured smaller local fallback or explicitly benchmark CPU execution",
        ),
    )


def default_30b_profile(**overrides: Any) -> QuantizedModelProfile:
    """Return the checked-in Qwen 30B-A3B Q4 planning profile."""
    return quantized_model_profile(**overrides)


__all__ = [
    "DEFAULT_30B_CONTEXT",
    "DEFAULT_30B_MODEL",
    "DEFAULT_30B_QUANTIZATION",
    "HardwareCapabilityReport",
    "ModelExecutionPlan",
    "QuantizedModelProfile",
    "build_hardware_capabilities",
    "default_30b_profile",
    "plan_model_execution",
    "quantized_model_profile",
]
