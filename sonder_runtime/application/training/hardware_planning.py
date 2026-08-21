"""Typed, hardware-aware training and inference planning boundary.

This module contains the pure planning slice of the legacy adaptive-training
entrypoint.  It does not start processes, write state, deploy models, or
mutate runtime policy; those lifecycle operations remain behind their
existing attended boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


class HardwareProfilePort(Protocol):
    """Read-only host-capacity port supplied by the platform detector."""

    os_name: str
    architecture: str
    system_ram_total_gb: float
    system_ram_available_gb: float
    gpu_vendor: str
    gpu_name: str
    cuda_available: bool
    rocm_available: bool
    vram_total_gb: float
    vram_free_gb: float
    compute_capability: str
    cpu_offload_supported: bool
    system_ram_availability_live: bool
    vram_availability_live: bool

    def to_dict(self) -> dict: ...


MODEL_SPECS = {
    "1.5b": {"params": 1.5, "hf": "Qwen/Qwen2.5-Coder-1.5B-Instruct", "hf_revision": "2e1fd397ee46e1388853d2af2c993145b0f1098a", "ollama": "qwen2.5-coder:1.5b", "train_vram": 2.8, "train_ram": 6.0, "infer_vram": 1.6, "infer_ram": 3.0},
    "3b": {"params": 3.0, "hf": "Qwen/Qwen2.5-Coder-3B-Instruct", "hf_revision": "488639f1ff808d1d3d0ba301aef8c11461451ec5", "ollama": "qwen2.5-coder:3b", "train_vram": 5.0, "train_ram": 10.0, "infer_vram": 2.8, "infer_ram": 5.0},
    "7b": {"params": 7.0, "hf": "Qwen/Qwen2.5-Coder-7B-Instruct", "hf_revision": "c03e6d358207e414f1eca0bb1891e29f1db0e242", "ollama": "qwen2.5-coder:7b", "train_vram": 10.0, "train_ram": 18.0, "infer_vram": 5.5, "infer_ram": 9.0},
}
MODEL_ALIASES = {"1.5": "1.5b", "1.5b": "1.5b", "3": "3b", "3b": "3b", "7": "7b", "7b": "7b"}
TRAINING_CPU_OFFLOAD_SUPPORTED = False
TRAINING_CPU_OFFLOAD_REASON = (
    "Training CPU offload is disabled for the current bitsandbytes/Trainer "
    "backend: its device_map='auto' path is intended for inference, not QLoRA training."
)


@dataclass(frozen=True)
class PlanOptions:
    model: str = "auto"
    allow_cpu_offload: bool = False
    max_vram_gb: float | None = None
    max_system_ram_gb: float | None = None
    context_length: int = 8192
    sequence_length: int = 1024
    batch_size: int = 1
    gradient_accumulation: int = 8
    full_finetune: bool = False
    gpu_index: int = 0


@dataclass
class Recommendation:
    enabled: bool
    model_size: str
    model: str
    method: str
    estimated_vram_gb: float
    estimated_system_ram_gb: float
    cpu_offload: bool
    reason: str
    rejected: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class HardwarePlan:
    hardware: HardwareProfilePort
    inference: Recommendation
    training: Recommendation
    usable_vram_gb: float
    usable_system_ram_gb: float
    options: PlanOptions

    def to_dict(self):
        return {"hardware": self.hardware.to_dict(), "budgets": {"usable_vram_gb": self.usable_vram_gb, "usable_system_ram_gb": self.usable_system_ram_gb}, "inference": self.inference.to_dict(), "training": self.training.to_dict(), "options": asdict(self.options)}


def _bounded_available(value, maximum):
    return min(value, maximum) if maximum is not None else value


def memory_budgets(profile, options):
    usable_ram = max(0.0, profile.system_ram_available_gb - max(2.0, profile.system_ram_total_gb * 0.25))
    usable_ram = _bounded_available(usable_ram, options.max_system_ram_gb)
    available_vram = profile.vram_free_gb
    vram_reserve = 2.0 if available_vram and profile.vram_total_gb >= 12 else 1.0 if available_vram else 0.0
    usable_vram = _bounded_available(max(0.0, available_vram - vram_reserve), options.max_vram_gb)
    return round(usable_vram, 2), round(usable_ram, 2)


def _training_estimate(size, options):
    spec = MODEL_SPECS[size]
    scale = max(0.5, options.sequence_length / 1024) * max(1, options.batch_size)
    return round(spec["train_vram"] + (scale - 1.0) * (0.35 + spec["params"] * 0.10), 2), round(spec["train_ram"] + max(0.0, scale - 1.0) * spec["params"] * 0.35, 2)


def _inference_estimate(size, options):
    spec = MODEL_SPECS[size]
    scale = max(0.25, options.context_length / 8192)
    return round(max(spec["infer_vram"], spec["infer_vram"] + (scale - 1.0) * spec["params"] * 0.18), 2), round(max(spec["infer_ram"], spec["infer_ram"] + (scale - 1.0) * spec["params"] * 0.12), 2)


def _requested_size(value):
    value = str(value or "auto").strip().lower()
    if value == "auto":
        return value
    if value not in MODEL_ALIASES:
        raise ValueError("model must be auto, 1.5b, 3b, or 7b")
    return MODEL_ALIASES[value]


def build_plan(profile=None, options=None):
    options = options or PlanOptions()
    if profile is None:
        raise ValueError("a detected hardware profile must be supplied to the application planning boundary")
    requested = _requested_size(options.model)
    usable_vram, usable_ram = memory_budgets(profile, options)
    available_vram = _bounded_available(profile.vram_free_gb, options.max_vram_gb)
    rejected = []
    inference_size = "1.5b"
    for size in ("7b", "3b", "1.5b"):
        est_vram, est_ram = _inference_estimate(size, options)
        if (available_vram and est_vram <= usable_vram) or est_ram <= usable_ram:
            inference_size = size
            break
        rejected.append(f"Inference {size} rejected: needs about {est_vram:.1f} GB VRAM or {est_ram:.1f} GB RAM headroom.")
    if requested != "auto":
        est_vram, est_ram = _inference_estimate(requested, options)
        if est_vram <= usable_vram or est_ram <= usable_ram:
            inference_size = requested
        else:
            rejected.append(f"Requested inference {requested} cannot preserve memory reserves; using {inference_size}.")
    infer_vram, infer_ram = _inference_estimate(inference_size, options)
    infer_offload = bool(available_vram and infer_vram > usable_vram)
    inference = Recommendation(True, inference_size, MODEL_SPECS[inference_size]["ollama"], "Ollama 4-bit inference" if available_vram else "Ollama 4-bit CPU inference", min(infer_vram, usable_vram) if usable_vram else 0.0, infer_ram if (infer_offload or not available_vram) else min(2.0, infer_ram), infer_offload, f"{available_vram:.1f} GB currently free VRAM and {usable_ram:.1f} GB usable system RAM after independent reserves.", list(rejected), {"context_length": options.context_length})
    train_rejected = []
    training_size = ""
    runtime_supported = profile.cuda_available and profile.gpu_vendor == "nvidia"
    if not runtime_supported:
        train_rejected.append("Local QLoRA disabled: this bitsandbytes path requires a supported NVIDIA CUDA runtime.")
    if options.allow_cpu_offload:
        train_rejected.append(TRAINING_CPU_OFFLOAD_REASON)
    candidates = [requested] if requested != "auto" else ["7b", "3b", "1.5b"]
    for size in candidates:
        est_vram, est_ram = _training_estimate(size, options)
        range_ok = (size == "1.5b" and available_vram >= 4.0) or (size == "3b" and available_vram >= 7.5) or (size == "7b" and available_vram >= 11.5 and usable_ram >= 16.0)
        direct_fit = est_vram <= usable_vram and est_ram <= usable_ram
        if runtime_supported and not options.allow_cpu_offload and range_ok and direct_fit:
            training_size = size
            break
        reasons = []
        if not range_ok: reasons.append("outside the conservative free-VRAM starting range")
        if est_vram > usable_vram: reasons.append(f"~{est_vram:.1f} GB VRAM exceeds {usable_vram:.1f} GB budget")
        if est_ram > usable_ram: reasons.append(f"~{est_ram:.1f} GB RAM exceeds {usable_ram:.1f} GB budget")
        if options.allow_cpu_offload: reasons.append("requested CPU offload backend is unavailable")
        train_rejected.append(f"QLoRA {size} rejected: "+"; ".join(reasons or ["runtime unsupported"])+".")
    method = "QLoRA (4-bit NF4)"
    if options.full_finetune:
        dense_size = requested if requested != "auto" else "1.5b"
        dense_vram, dense_ram = round(MODEL_SPECS[dense_size]["params"] * 16 + 4, 1), round(MODEL_SPECS[dense_size]["params"] * 8 + 8, 1)
        if not runtime_supported or dense_vram > usable_vram or dense_ram > usable_ram:
            train_rejected.append(f"Dense {dense_size} rejected: estimated {dense_vram:.1f} GB VRAM/{dense_ram:.1f} GB RAM; it is explicit opt-in and does not fit safely.")
            training_size = ""
        else:
            training_size, method = dense_size, "full-parameter bf16 (advanced opt-in)"
    if training_size:
        est_vram, est_ram = (dense_vram, dense_ram) if method.startswith("full-parameter") else _training_estimate(training_size, options)
        training = Recommendation(True, training_size, MODEL_SPECS[training_size]["hf"], method, est_vram, est_ram, False, f"{available_vram:.1f} GB currently free VRAM; {usable_vram:.1f} GB GPU budget and {usable_ram:.1f} GB RAM budget after desktop/OS reserves.", train_rejected, {"quantization": "NF4" if method.startswith("QLoRA") else "none", "sequence_length": options.sequence_length, "batch_size": options.batch_size, "gradient_accumulation": options.gradient_accumulation, "gradient_checkpointing": True})
    else:
        training = Recommendation(False, "", "", "disabled", 0.0, 0.0, False, "No supported attended local weight-training plan fits the live memory budgets.", train_rejected, {})
    return HardwarePlan(profile, inference, training, usable_vram, usable_ram, options)


def format_hardware(profile=None):
    if profile is None:
        raise ValueError("a detected hardware profile must be supplied to the application planning boundary")
    p = profile
    runtime = "CUDA" if p.cuda_available else "ROCm" if p.rocm_available else "none"
    ram_freshness = "live" if p.system_ram_availability_live else "conservative fallback"
    vram_freshness = "live" if p.vram_availability_live else "conservative fallback"
    return "\n".join(["Sonder Runtime hardware", f"  OS: {p.os_name} {p.architecture}", f"  system RAM: {p.system_ram_available_gb:.1f} GB available / {p.system_ram_total_gb:.1f} GB total ({ram_freshness})", f"  GPU: {p.gpu_vendor} {p.gpu_name or '(none)'} | runtime: {runtime}", f"  VRAM: {p.vram_free_gb:.1f} GB free / {p.vram_total_gb:.1f} GB total ({vram_freshness})", f"  compute capability: {p.compute_capability or 'n/a'}", f"  CPU offload hardware/runtime capability: {'yes' if p.cpu_offload_supported else 'no'}; QLoRA backend: disabled"])


def format_plan(plan):
    t, i = plan.training, plan.inference
    lines = [format_hardware(plan.hardware), "", f"Memory budgets: {plan.usable_vram_gb:.1f} GB VRAM; {plan.usable_system_ram_gb:.1f} GB system RAM", f"Inference: {i.model} ({i.method})", f"  estimate: {i.estimated_vram_gb:.1f} GB VRAM; {i.estimated_system_ram_gb:.1f} GB RAM; CPU offload: {'yes' if i.cpu_offload else 'no'}", f"  reason: {i.reason}"]
    if t.enabled:
        lines += [f"Training: {t.method} {t.model_size}, GPU {plan.options.gpu_index}, batch {t.settings['batch_size']}, gradient accumulation {t.settings['gradient_accumulation']}", f"  base: {t.model}", f"  estimate: {t.estimated_vram_gb:.1f} GB VRAM; {t.estimated_system_ram_gb:.1f} GB RAM; CPU offload: {'yes' if t.cpu_offload else 'no'}", f"  reason: {t.reason}"]
    else:
        lines += ["Training: disabled", f"  reason: {t.reason}"]
    rejected = i.rejected + t.rejected
    if rejected:
        lines.append("Rejected alternatives:")
        lines.extend(f"  - {item}" for item in rejected)
    return "\n".join(lines)


__all__ = ["HardwareProfilePort", "MODEL_SPECS", "MODEL_ALIASES", "TRAINING_CPU_OFFLOAD_SUPPORTED", "TRAINING_CPU_OFFLOAD_REASON", "PlanOptions", "Recommendation", "HardwarePlan", "memory_budgets", "build_plan", "format_hardware", "format_plan"]
