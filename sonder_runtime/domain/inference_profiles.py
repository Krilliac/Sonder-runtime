"""Hardware capabilities, quantized inference planning and launch-profile metadata.

This module is deliberately pure.  Platform adapters may supply observations,
but this policy never probes a driver, chooses a cloud endpoint, or claims a
backend is ready merely because a vendor string was detected.

The launch-profile section at the end parses what a Sonder Inference
``serve --profile`` publishes on ``/v1/models`` (the context length a served
profile holds, and the capabilities a request to it can use).  The adapter
(``SonderInferenceGateway.model_profiles``) fetches and caches the document;
the parsing and lookup rules live here so they stay I/O free.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from types import MappingProxyType
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

# KV-cache planning envelope in decimal GB per 8192 tokens of context, keyed
# by total parameter count.  KV bytes per token grow with layer count and
# hidden width, so a 70B-class model reserves roughly four times the KV of a
# 7B at the same window.  Like the weight table above, these are conservative
# planning values, not a measured residency report.
_KV_GB_PER_8K_BANDS = (
    (5.0, 0.25),
    (13.0, 0.35),
    (40.0, 0.5),
    (float("inf"), 1.0),
)

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


def kv_gb_per_8k(total_params_b: float) -> float:
    """Planning KV-cache envelope in GB per 8192 tokens for a model size."""
    total = _positive(total_params_b)
    if total is None:
        return _KV_GB_PER_8K_BANDS[-1][1]
    for ceiling, kv_gb in _KV_GB_PER_8K_BANDS:
        if total < ceiling:
            return kv_gb
    return _KV_GB_PER_8K_BANDS[-1][1]


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
    # KV grows with context and with model depth/width; this remains a
    # deliberately conservative planning envelope and the live provider
    # remains authoritative.
    kv = round(kv_gb_per_8k(total) * context / 8192.0, 2)
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


# -- launch-profile metadata (Sonder Inference ``/v1/models``) ---------------
#
# Every field is optional.  An Inference that publishes none of them, or has
# no ``/v1/models`` route at all, yields "unknown" (``None``) for every model,
# and an unknown context length never changes a route.  Fields are read from
# the entry's ``sonder.profile`` object first (where ``serve --profile``
# publishes them, with ``context_length`` already reduced to the running value
# after an ``auto_fit`` relaunch), then from the ``sonder`` extension object,
# then from the entry itself.

CATALOG_OK = "ok"          # /v1/models answered and was parsed
CATALOG_ABSENT = "absent"  # no /v1/models (older Inference) or no usable list
CATALOG_ERROR = "error"    # unreachable, refused, timed out, malformed

_PROFILE_CONTEXT_FIELDS = ("context_length", "context_tokens", "n_ctx")
_PROFILE_MAX_CONTEXT = 16_777_216
_PROFILE_MAX_CAPABILITIES = 32
_PROFILE_MAX_TEXT = 128


def _empty_profiles() -> Mapping[str, "LaunchProfileMetadata"]:
    return MappingProxyType({})


@dataclass(frozen=True)
class LaunchProfileMetadata:
    """What ``/v1/models`` says about one served model id.  ``None`` = not reported."""

    model_id: str
    context_length: int | None = None
    capabilities: tuple[str, ...] | None = None
    profile: str | None = None
    backend: str | None = None
    default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "profile": self.profile,
            "context_length": self.context_length,
            "capabilities": list(self.capabilities) if self.capabilities is not None else None,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class LaunchProfileCatalog:
    """One observation of an endpoint's ``/v1/models`` (or of its absence)."""

    base_url: str
    state: str
    detail: str = ""
    models: Mapping[str, LaunchProfileMetadata] = field(default_factory=_empty_profiles)
    checked_monotonic: float = 0.0

    def lookup(self, model_id: object) -> LaunchProfileMetadata | None:
        """The entry serving ``model_id``; ``default`` resolves to the default entry."""
        wanted = str(model_id or "").strip()
        if not wanted:
            return None
        found = self.models.get(wanted)
        if found is not None:
            return found
        folded = wanted.casefold()
        for key, profile in self.models.items():
            if key.casefold() == folded or (profile.profile or "").casefold() == folded:
                return profile
        if folded == "default":
            for profile in self.models.values():
                if profile.default:
                    return profile
        return None


def _profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:_PROFILE_MAX_TEXT] if text else None


def _profile_context(value: object) -> int | None:
    if type(value) is bool:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 < value <= _PROFILE_MAX_CONTEXT:
        return value
    return None


def _profile_capabilities(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    names: list[str] = []
    for item in value[:_PROFILE_MAX_CAPABILITIES]:
        text = _profile_text(item)
        if text and text.lower() not in names:
            names.append(text.lower())
    return tuple(names)


def _profile_first(sources: tuple[Mapping[str, Any], ...], names: tuple[str, ...]) -> object:
    for source in sources:
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    return None


def parse_launch_profile_entry(entry: object) -> LaunchProfileMetadata | None:
    """One ``/v1/models`` ``data[]`` entry; ``None`` when it has no usable id."""
    if not isinstance(entry, Mapping):
        return None
    model_id = _profile_text(entry.get("id"))
    if model_id is None:
        return None
    extension = entry.get("sonder")
    sources: tuple[Mapping[str, Any], ...] = (
        (extension, entry) if isinstance(extension, Mapping) else (entry,)
    )
    raw_profile = _profile_first(sources, ("profile",))
    if isinstance(raw_profile, Mapping):
        # ``serve --profile``: the served entry's ``sonder.profile`` object
        # carries ``name``, ``context_length`` (the running value when
        # auto_fit shrank it) and ``capabilities``.
        sources = (raw_profile,) + sources
        raw_profile = raw_profile.get("name")
    return LaunchProfileMetadata(
        model_id=model_id,
        context_length=_profile_context(_profile_first(sources, _PROFILE_CONTEXT_FIELDS)),
        capabilities=_profile_capabilities(_profile_first(sources, ("capabilities",))),
        profile=_profile_text(raw_profile),
        backend=_profile_text(_profile_first(sources, ("backend",))),
        default=isinstance(extension, Mapping) and extension.get("default") is True,
    )


def parse_launch_profile_models(document: object) -> dict[str, LaunchProfileMetadata] | None:
    """``{model id: metadata}`` from a ``/v1/models`` body, or ``None`` without a list."""
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, list):
        return None
    result: dict[str, LaunchProfileMetadata] = {}
    for entry in data:
        profile = parse_launch_profile_entry(entry)
        if profile is not None and profile.model_id not in result:
            result[profile.model_id] = profile
    return result


__all__ = [
    "CATALOG_ABSENT",
    "CATALOG_ERROR",
    "CATALOG_OK",
    "DEFAULT_30B_CONTEXT",
    "DEFAULT_30B_MODEL",
    "DEFAULT_30B_QUANTIZATION",
    "HardwareCapabilityReport",
    "LaunchProfileCatalog",
    "LaunchProfileMetadata",
    "ModelExecutionPlan",
    "QuantizedModelProfile",
    "build_hardware_capabilities",
    "default_30b_profile",
    "kv_gb_per_8k",
    "parse_launch_profile_entry",
    "parse_launch_profile_models",
    "plan_model_execution",
    "quantized_model_profile",
]
