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

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from math import isfinite
import re
from types import MappingProxyType
from typing import Any, cast


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


def _bool_or_none(value: object) -> bool | None:
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
            vram_free = max(0.0, float(cast(float, hardware.get("vram_free_gb"))))
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


def kv_gb_per_8k(total_params_b: float | None) -> float:
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


# -- node hardware & GPU pool metadata (Sonder Inference ``/v1/sonder/node``) --
#
# Pure value types and strict parser for ``sonder.inference.node/1``, including
# PR #97's ``gpu_pool``, ``combined_gpu_vram_mib``, ``capabilities.hybrid_gpu_spread``,
# and per-GPU discrete vs integrated UMA memory fields.  Nothing here performs
# I/O or invents readings the node did not report.

NODE_SCHEMA = "sonder.inference.node/1"
NODE_API_VERSION = 1
NODE_OK = "ok"
NODE_ABSENT = "absent"
NODE_ERROR = "error"

_NODE_MAX_GPUS = 32
_NODE_MAX_DEVICES = 16
_NODE_MAX_CAPABILITIES = 64
_NODE_MAX_VRAM_MIB = 64 * 1024 * 1024  # 64 TiB defensive ceiling
_NODE_MAX_BYTES = _NODE_MAX_VRAM_MIB * 1024 * 1024
_NODE_MAX_TEXT = 128
_NODE_DEVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_NODE_CAP_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}\Z")
_NODE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _empty_bool_map() -> Mapping[str, bool]:
    return MappingProxyType({})


def _node_text(value: object, *, limit: int = _NODE_MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _NODE_CTRL_RE.sub(" ", value).strip()
    if not cleaned:
        return None
    return " ".join(cleaned.split())[:limit]


def _node_nonneg_int(value: object, *, maximum: int) -> int | None:
    if type(value) is bool:
        return None
    if isinstance(value, float):
        if not isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if type(value) is not int:
        return None
    if 0 <= value <= maximum:
        return value
    return None


def _clamp_free(free_val: int | None, total_val: int | None) -> int | None:
    if free_val is None:
        return None
    if total_val is not None:
        return min(free_val, total_val)
    return free_val


@dataclass(frozen=True)
class NodeHostMemory:
    """Host physical/commit memory reported by ``/v1/sonder/node``."""

    total_physical_bytes: int | None = None
    available_physical_bytes: int | None = None
    available_commit_bytes: int | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_physical_bytes": self.total_physical_bytes,
            "available_physical_bytes": self.available_physical_bytes,
            "available_commit_bytes": self.available_commit_bytes,
            "source": self.source,
        }


@dataclass(frozen=True)
class NodeGpuDevice:
    """One GPU entry from ``/v1/sonder/node`` ``gpus[]`` (discrete or integrated UMA)."""

    gpu_id: str
    name: str
    vendor: str = "unknown"
    is_integrated_uma: bool = False
    dedicated_total_mib: int | None = None
    dedicated_free_mib: int | None = None
    shared_total_mib: int | None = None
    effective_vram_mib: int | None = None
    effective_free_vram_mib: int | None = None
    backend_hint: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.gpu_id,
            "name": self.name,
            "vendor": self.vendor,
            "is_integrated_uma": self.is_integrated_uma,
            "dedicated_total_mib": self.dedicated_total_mib,
            "dedicated_free_mib": self.dedicated_free_mib,
            "shared_total_mib": self.shared_total_mib,
            "effective_vram_mib": self.effective_vram_mib,
            "effective_free_vram_mib": self.effective_free_vram_mib,
            "backend_hint": self.backend_hint,
            "source": self.source,
        }


@dataclass(frozen=True)
class NodeGpuPoolSummary:
    """Discrete, integrated UMA, and combined GPU VRAM pool summary in MiB."""

    discrete_vram_mib: int = 0
    integrated_uma_vram_mib: int = 0
    combined_gpu_vram_mib: int = 0
    discrete_free_vram_mib: int = 0
    integrated_uma_free_vram_mib: int = 0
    combined_free_gpu_vram_mib: int = 0
    discrete_gpu_count: int = 0
    integrated_uma_gpu_count: int = 0
    hybrid_vulkan_pool_available: bool = False
    recommended_devices: tuple[str, ...] = ()
    llama_arg_device: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "discrete_vram_mib": self.discrete_vram_mib,
            "integrated_uma_vram_mib": self.integrated_uma_vram_mib,
            "combined_gpu_vram_mib": self.combined_gpu_vram_mib,
            "discrete_free_vram_mib": self.discrete_free_vram_mib,
            "integrated_uma_free_vram_mib": self.integrated_uma_free_vram_mib,
            "combined_free_gpu_vram_mib": self.combined_free_gpu_vram_mib,
            "discrete_gpu_count": self.discrete_gpu_count,
            "integrated_uma_gpu_count": self.integrated_uma_gpu_count,
            "hybrid_vulkan_pool_available": self.hybrid_vulkan_pool_available,
            "recommended_devices": list(self.recommended_devices),
            "llama_arg_device": self.llama_arg_device,
        }


@dataclass(frozen=True)
class InferenceNodeSnapshot:
    """Content-free observation of one Sonder Inference node (``/v1/sonder/node``)."""

    base_url: str
    state: str
    detail: str = ""
    schema: str | None = None
    node_id: str | None = None
    platform: str | None = None
    version: str | None = None
    api_version: int | None = None
    synthetic: bool | None = None
    host_memory: NodeHostMemory | None = None
    gpus: tuple[NodeGpuDevice, ...] = ()
    combined_gpu_vram_mib: int | None = None
    gpu_pool: NodeGpuPoolSummary | None = None
    capabilities: Mapping[str, bool] = field(default_factory=_empty_bool_map)
    checked_monotonic: float = 0.0

    @property
    def hybrid_gpu_spread(self) -> bool:
        return self.capabilities.get("hybrid_gpu_spread") is True

    @property
    def uma_telemetry(self) -> bool:
        return self.capabilities.get("uma_telemetry") is True

    @property
    def multi_gpu(self) -> bool:
        return self.capabilities.get("multi_gpu") is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "state": self.state,
            "detail": self.detail,
            "schema": self.schema,
            "node_id": self.node_id,
            "platform": self.platform,
            "version": self.version,
            "api_version": self.api_version,
            "synthetic": self.synthetic,
            "host_memory": self.host_memory.to_dict() if self.host_memory is not None else None,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
            "combined_gpu_vram_mib": self.combined_gpu_vram_mib,
            "gpu_pool": self.gpu_pool.to_dict() if self.gpu_pool is not None else None,
            "capabilities": dict(self.capabilities),
        }


def parse_node_host_memory(raw: object) -> NodeHostMemory | None:
    """Parse ``host_memory`` from ``/v1/sonder/node`` without fabricating missing fields."""
    if not isinstance(raw, Mapping):
        return None
    total = _node_nonneg_int(raw.get("total_physical_bytes"), maximum=_NODE_MAX_BYTES)
    avail = _clamp_free(
        _node_nonneg_int(raw.get("available_physical_bytes"), maximum=_NODE_MAX_BYTES),
        total,
    )
    commit = _node_nonneg_int(raw.get("available_commit_bytes"), maximum=_NODE_MAX_BYTES)
    source = _node_text(raw.get("source"), limit=64)
    if total is None and avail is None and commit is None and source is None:
        return None
    return NodeHostMemory(
        total_physical_bytes=total,
        available_physical_bytes=avail,
        available_commit_bytes=commit,
        source=source,
    )


def parse_node_gpu_device(entry: object) -> NodeGpuDevice | None:
    """Parse one ``gpus[]`` object from ``/v1/sonder/node``; ``None`` if unusable."""
    if not isinstance(entry, Mapping):
        return None
    gpu_id = _node_text(entry.get("id"), limit=64)
    name = _node_text(entry.get("name"), limit=_NODE_MAX_TEXT)
    if gpu_id is None and name is None:
        return None
    vendor = (_node_text(entry.get("vendor"), limit=32) or "unknown").lower()
    is_uma = entry.get("is_integrated_uma") is True
    dedicated_total = _node_nonneg_int(entry.get("dedicated_total_mib"), maximum=_NODE_MAX_VRAM_MIB)
    dedicated_free = _clamp_free(
        _node_nonneg_int(entry.get("dedicated_free_mib"), maximum=_NODE_MAX_VRAM_MIB),
        dedicated_total,
    )
    shared_total = _node_nonneg_int(entry.get("shared_total_mib"), maximum=_NODE_MAX_VRAM_MIB)
    effective_total = _node_nonneg_int(entry.get("effective_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
    effective_free = _clamp_free(
        _node_nonneg_int(entry.get("effective_free_vram_mib"), maximum=_NODE_MAX_VRAM_MIB),
        effective_total,
    )
    backend_hint = _node_text(entry.get("backend_hint"), limit=32)
    source = _node_text(entry.get("source"), limit=64)
    return NodeGpuDevice(
        gpu_id=gpu_id or "gpu",
        name=name or gpu_id or "unknown",
        vendor=vendor,
        is_integrated_uma=is_uma,
        dedicated_total_mib=dedicated_total,
        dedicated_free_mib=dedicated_free,
        shared_total_mib=shared_total,
        effective_vram_mib=effective_total,
        effective_free_vram_mib=effective_free,
        backend_hint=backend_hint.lower() if backend_hint else None,
        source=source,
    )


def _gpu_total_mib_for_pool(gpu: NodeGpuDevice) -> int | None:
    if gpu.effective_vram_mib is not None:
        return gpu.effective_vram_mib
    if gpu.is_integrated_uma:
        if gpu.shared_total_mib is not None:
            return gpu.shared_total_mib
        return gpu.dedicated_total_mib
    return gpu.dedicated_total_mib


def _gpu_free_mib_for_pool(gpu: NodeGpuDevice) -> int | None:
    if gpu.effective_free_vram_mib is not None:
        return gpu.effective_free_vram_mib
    if not gpu.is_integrated_uma:
        return gpu.dedicated_free_mib
    return None


def summarize_node_gpu_pool(
    gpus: Sequence[NodeGpuDevice],
    *,
    host_memory: NodeHostMemory | None = None,
) -> NodeGpuPoolSummary:
    """Summarize discrete, integrated UMA, and combined GPU VRAM pools from ``gpus[]``.

    Mirrors ``Sonder-Inference`` ``summarize_gpu_pools`` host-RAM capping for
    integrated UMA adapters, without fabricating ``hybrid_vulkan_pool_available``
    or ``recommended_devices`` that only the host server can attest.
    """
    discrete_total = 0
    discrete_free = 0
    uma_total = 0
    uma_free = 0
    discrete_count = 0
    uma_count = 0
    for gpu in gpus[:_NODE_MAX_GPUS]:
        total = _gpu_total_mib_for_pool(gpu)
        free = _gpu_free_mib_for_pool(gpu)
        if gpu.is_integrated_uma:
            uma_count += 1
            if total is not None:
                uma_total = min(_NODE_MAX_VRAM_MIB, uma_total + total)
            if free is not None:
                uma_free = min(_NODE_MAX_VRAM_MIB, uma_free + free)
        else:
            discrete_count += 1
            if total is not None:
                discrete_total = min(_NODE_MAX_VRAM_MIB, discrete_total + total)
            if free is not None:
                discrete_free = min(_NODE_MAX_VRAM_MIB, discrete_free + free)
    if host_memory is not None:
        if host_memory.total_physical_bytes is not None:
            host_total_mib = host_memory.total_physical_bytes // (1024 * 1024)
            uma_total = min(uma_total, host_total_mib)
        if host_memory.available_physical_bytes is not None:
            host_avail_mib = host_memory.available_physical_bytes // (1024 * 1024)
            uma_free = min(uma_free, host_avail_mib)
    uma_free = min(uma_free, uma_total) if uma_total > 0 else 0
    discrete_free = min(discrete_free, discrete_total) if discrete_total > 0 else 0
    combined_total = min(_NODE_MAX_VRAM_MIB, discrete_total + uma_total)
    combined_free = min(combined_total, discrete_free + uma_free)
    return NodeGpuPoolSummary(
        discrete_vram_mib=discrete_total,
        integrated_uma_vram_mib=uma_total,
        combined_gpu_vram_mib=combined_total,
        discrete_free_vram_mib=discrete_free,
        integrated_uma_free_vram_mib=uma_free,
        combined_free_gpu_vram_mib=combined_free,
        discrete_gpu_count=discrete_count,
        integrated_uma_gpu_count=uma_count,
        hybrid_vulkan_pool_available=False,
        recommended_devices=(),
        llama_arg_device=None,
    )


def _parse_device_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    devices: list[str] = []
    for item in raw[:_NODE_MAX_DEVICES]:
        text = _node_text(item, limit=64)
        if text is not None and _NODE_DEVICE_TOKEN_RE.fullmatch(text) and text not in devices:
            devices.append(text)
    return tuple(devices)


def _parse_llama_arg_device(raw: object) -> str | None:
    text = _node_text(raw, limit=256)
    if text is None:
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts or len(parts) > _NODE_MAX_DEVICES:
        return None
    if any(_NODE_DEVICE_TOKEN_RE.fullmatch(part) is None for part in parts):
        return None
    return ",".join(parts)


def parse_node_gpu_pool(
    raw: object,
    *,
    gpus: Sequence[NodeGpuDevice] = (),
    host_memory: NodeHostMemory | None = None,
    top_level_combined_mib: int | None = None,
) -> NodeGpuPoolSummary | None:
    """Parse ``gpu_pool`` from ``/v1/sonder/node``, falling back to ``gpus[]`` on older servers."""
    if isinstance(raw, Mapping):
        fallback = summarize_node_gpu_pool(gpus, host_memory=host_memory) if gpus else None
        discrete_total = _node_nonneg_int(raw.get("discrete_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        uma_total = _node_nonneg_int(raw.get("integrated_uma_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        combined_total = _node_nonneg_int(raw.get("combined_gpu_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        if discrete_total is None:
            discrete_total = fallback.discrete_vram_mib if fallback is not None else 0
        if uma_total is None:
            uma_total = fallback.integrated_uma_vram_mib if fallback is not None else 0
        if combined_total is None:
            combined_total = (
                top_level_combined_mib
                if top_level_combined_mib is not None
                else min(_NODE_MAX_VRAM_MIB, discrete_total + uma_total)
            )
        discrete_free = _node_nonneg_int(raw.get("discrete_free_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        if discrete_free is None:
            discrete_free = fallback.discrete_free_vram_mib if fallback is not None else 0
        discrete_free = min(discrete_free, discrete_total) if discrete_total > 0 else 0
        uma_free = _node_nonneg_int(raw.get("integrated_uma_free_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        if uma_free is None:
            uma_free = fallback.integrated_uma_free_vram_mib if fallback is not None else 0
        uma_free = min(uma_free, uma_total) if uma_total > 0 else 0
        combined_free = _node_nonneg_int(raw.get("combined_free_gpu_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
        if combined_free is None:
            combined_free = min(combined_total, discrete_free + uma_free)
        else:
            combined_free = min(combined_free, combined_total) if combined_total > 0 else 0
        discrete_count = _node_nonneg_int(raw.get("discrete_gpu_count"), maximum=_NODE_MAX_GPUS)
        if discrete_count is None:
            discrete_count = fallback.discrete_gpu_count if fallback is not None else 0
        uma_count = _node_nonneg_int(raw.get("integrated_uma_gpu_count"), maximum=_NODE_MAX_GPUS)
        if uma_count is None:
            uma_count = fallback.integrated_uma_gpu_count if fallback is not None else 0
        recommended = _parse_device_list(raw.get("recommended_devices"))
        llama_arg = _parse_llama_arg_device(raw.get("llama_arg_device"))
        if llama_arg is None and recommended:
            llama_arg = ",".join(recommended)
        hybrid_avail = raw.get("hybrid_vulkan_pool_available") is True
        return NodeGpuPoolSummary(
            discrete_vram_mib=discrete_total,
            integrated_uma_vram_mib=uma_total,
            combined_gpu_vram_mib=combined_total,
            discrete_free_vram_mib=discrete_free,
            integrated_uma_free_vram_mib=uma_free,
            combined_free_gpu_vram_mib=combined_free,
            discrete_gpu_count=discrete_count,
            integrated_uma_gpu_count=uma_count,
            hybrid_vulkan_pool_available=hybrid_avail,
            recommended_devices=recommended,
            llama_arg_device=llama_arg,
        )
    if gpus:
        synthesized = summarize_node_gpu_pool(gpus, host_memory=host_memory)
        if top_level_combined_mib is not None:
            return NodeGpuPoolSummary(
                discrete_vram_mib=synthesized.discrete_vram_mib,
                integrated_uma_vram_mib=synthesized.integrated_uma_vram_mib,
                combined_gpu_vram_mib=top_level_combined_mib,
                discrete_free_vram_mib=synthesized.discrete_free_vram_mib,
                integrated_uma_free_vram_mib=synthesized.integrated_uma_free_vram_mib,
                combined_free_gpu_vram_mib=min(synthesized.combined_free_gpu_vram_mib, top_level_combined_mib),
                discrete_gpu_count=synthesized.discrete_gpu_count,
                integrated_uma_gpu_count=synthesized.integrated_uma_gpu_count,
                hybrid_vulkan_pool_available=False,
                recommended_devices=(),
                llama_arg_device=None,
            )
        return synthesized
    if top_level_combined_mib is not None:
        return NodeGpuPoolSummary(combined_gpu_vram_mib=top_level_combined_mib)
    return None


def _parse_node_capabilities(raw: object) -> dict[str, bool]:
    if not isinstance(raw, Mapping):
        return {}
    caps: dict[str, bool] = {}
    for key, value in list(raw.items())[:_NODE_MAX_CAPABILITIES]:
        clean = _node_text(key, limit=64)
        if clean is None:
            continue
        lowered = clean.lower()
        if _NODE_CAP_KEY_RE.fullmatch(lowered) and type(value) is bool:
            caps[lowered] = value
    return caps


def parse_inference_node_document(
    document: object,
    *,
    base_url: str = "",
) -> InferenceNodeSnapshot | None:
    """Parse a ``GET /v1/sonder/node`` JSON object into a frozen snapshot.

    Returns ``None`` when ``document`` is not a valid ``sonder.inference.node/1``
    payload (for example a health or identity response on an older server) or
    reports an incompatible ``schema`` / ``api_version``.
    """
    if not isinstance(document, Mapping):
        return None
    raw_schema = document.get("schema")
    schema = _node_text(raw_schema, limit=64) if raw_schema is not None else None
    if schema is not None and schema != NODE_SCHEMA:
        return None
    raw_api = document.get("api_version")
    api_version: int | None = None
    if raw_api is not None:
        api_version = _node_nonneg_int(raw_api, maximum=1000)
        if api_version != NODE_API_VERSION:
            return None
    has_node_fields = any(
        key in document for key in ("gpu_pool", "combined_gpu_vram_mib", "gpus", "host_memory", "capabilities")
    )
    if schema != NODE_SCHEMA and not has_node_fields:
        return None

    host_memory = parse_node_host_memory(document.get("host_memory"))
    raw_gpus = document.get("gpus")
    gpus: list[NodeGpuDevice] = []
    if isinstance(raw_gpus, list):
        for entry in raw_gpus[:_NODE_MAX_GPUS]:
            parsed_gpu = parse_node_gpu_device(entry)
            if parsed_gpu is not None:
                gpus.append(parsed_gpu)
    gpu_tuple = tuple(gpus)
    top_combined = _node_nonneg_int(document.get("combined_gpu_vram_mib"), maximum=_NODE_MAX_VRAM_MIB)
    gpu_pool = parse_node_gpu_pool(
        document.get("gpu_pool"),
        gpus=gpu_tuple,
        host_memory=host_memory,
        top_level_combined_mib=top_combined,
    )
    effective_combined = (
        top_combined
        if top_combined is not None
        else (gpu_pool.combined_gpu_vram_mib if gpu_pool is not None else None)
    )
    capabilities = _parse_node_capabilities(document.get("capabilities"))
    synthetic_raw = document.get("synthetic")
    synthetic = synthetic_raw if type(synthetic_raw) is bool else None
    detail = "%d gpu(s)" % len(gpu_tuple)
    if effective_combined is not None:
        detail = "%d gpu(s), %d MiB combined" % (len(gpu_tuple), effective_combined)
    return InferenceNodeSnapshot(
        base_url=base_url,
        state=NODE_OK,
        detail=detail,
        schema=schema,
        node_id=_node_text(document.get("node_id"), limit=64),
        platform=_node_text(document.get("platform"), limit=64),
        version=_node_text(document.get("version"), limit=64),
        api_version=api_version,
        synthetic=synthetic,
        host_memory=host_memory,
        gpus=gpu_tuple,
        combined_gpu_vram_mib=effective_combined,
        gpu_pool=gpu_pool,
        capabilities=MappingProxyType(capabilities),
    )


def format_node_gpu_pool_summary(snapshot: InferenceNodeSnapshot) -> str:
    """Compact, content-free GPU pool line for doctor diagnostics (empty when absent)."""
    if snapshot.state != NODE_OK:
        return ""
    pool = snapshot.gpu_pool
    combined = snapshot.combined_gpu_vram_mib
    if pool is None and combined is None:
        return ""
    if pool is None:
        assert combined is not None
        summary = "gpu pool: %d MiB combined" % combined
    elif pool.discrete_gpu_count == 0 and pool.integrated_uma_gpu_count == 0 and pool.combined_gpu_vram_mib == 0:
        summary = "gpu pool: 0 MiB (no GPU)"
    else:
        parts: list[str] = []
        if pool.discrete_gpu_count > 0 or pool.discrete_vram_mib > 0:
            parts.append("%d discrete: %d MiB" % (pool.discrete_gpu_count, pool.discrete_vram_mib))
        if pool.integrated_uma_gpu_count > 0 or pool.integrated_uma_vram_mib > 0:
            parts.append("%d UMA: %d MiB" % (pool.integrated_uma_gpu_count, pool.integrated_uma_vram_mib))
        breakdown = ", ".join(parts)
        if pool.combined_free_gpu_vram_mib > 0:
            breakdown = (
                "%s; free %d MiB" % (breakdown, pool.combined_free_gpu_vram_mib)
                if breakdown
                else "free %d MiB" % pool.combined_free_gpu_vram_mib
            )
        total_mib = combined if combined is not None else pool.combined_gpu_vram_mib
        summary = "gpu pool: %d MiB combined (%s)" % (total_mib, breakdown) if breakdown else (
            "gpu pool: %d MiB combined" % total_mib
        )
    if "hybrid_gpu_spread" in snapshot.capabilities:
        if snapshot.hybrid_gpu_spread and pool is not None and pool.hybrid_vulkan_pool_available:
            devices = pool.llama_arg_device or ",".join(pool.recommended_devices)
            if devices:
                summary += "; hybrid_gpu_spread=available (%s)" % devices
            else:
                summary += "; hybrid_gpu_spread=available"
        elif snapshot.hybrid_gpu_spread:
            summary += "; hybrid_gpu_spread=supported"
        else:
            summary += "; hybrid_gpu_spread=no"
    return summary


__all__ = [
    "CATALOG_ABSENT",
    "CATALOG_ERROR",
    "CATALOG_OK",
    "DEFAULT_30B_CONTEXT",
    "DEFAULT_30B_MODEL",
    "DEFAULT_30B_QUANTIZATION",
    "HardwareCapabilityReport",
    "InferenceNodeSnapshot",
    "LaunchProfileCatalog",
    "LaunchProfileMetadata",
    "ModelExecutionPlan",
    "NODE_ABSENT",
    "NODE_API_VERSION",
    "NODE_ERROR",
    "NODE_OK",
    "NODE_SCHEMA",
    "NodeGpuDevice",
    "NodeGpuPoolSummary",
    "NodeHostMemory",
    "QuantizedModelProfile",
    "build_hardware_capabilities",
    "default_30b_profile",
    "format_node_gpu_pool_summary",
    "kv_gb_per_8k",
    "parse_inference_node_document",
    "parse_launch_profile_entry",
    "parse_launch_profile_models",
    "parse_node_gpu_device",
    "parse_node_gpu_pool",
    "parse_node_host_memory",
    "plan_model_execution",
    "quantized_model_profile",
    "summarize_node_gpu_pool",
]
