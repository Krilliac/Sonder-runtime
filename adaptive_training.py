"""Hardware-aware inference/training planning and attended training lifecycle.

This module is stdlib-only. Heavy ML dependencies remain isolated in
``qlora_train.py`` so hardware/status/dry-run commands work on normal installs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import runtime_policy
import promotion_eval
import ollama_endpoint
from process_liveness import pid_alive as _process_pid_alive
import system_profile
import sonder_paths
import training_data


ROOT = Path(__file__).resolve().parent
PERSONAL_MODEL = "sonder-personal:latest"
ROLLBACK_MODEL = "sonder:latest"
LLAMA_CPP_REVISION = "99f3dc32296f825fec94f202da1e9fede1e78cf9"
LLAMA_CPP_TREE_SHA256 = "3cb23bb624453dc3511df618bc1445169d44b8fb635bd04a1b3eabc45db2d4df"
MODEL_SPECS = {
    "1.5b": {
        "params": 1.5,
        "hf": "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        "hf_revision": "2e1fd397ee46e1388853d2af2c993145b0f1098a",
        "ollama": "qwen2.5-coder:1.5b",
        "train_vram": 2.8,
        "train_ram": 6.0,
        "infer_vram": 1.6,
        "infer_ram": 3.0,
    },
    "3b": {
        "params": 3.0,
        "hf": "Qwen/Qwen2.5-Coder-3B-Instruct",
        "hf_revision": "488639f1ff808d1d3d0ba301aef8c11461451ec5",
        "ollama": "qwen2.5-coder:3b",
        "train_vram": 5.0,
        "train_ram": 10.0,
        "infer_vram": 2.8,
        "infer_ram": 5.0,
    },
    "7b": {
        "params": 7.0,
        "hf": "Qwen/Qwen2.5-Coder-7B-Instruct",
        "hf_revision": "c03e6d358207e414f1eca0bb1891e29f1db0e242",
        "ollama": "qwen2.5-coder:7b",
        "train_vram": 10.0,
        "train_ram": 18.0,
        "infer_vram": 5.5,
        "infer_ram": 9.0,
    },
}
MODEL_ALIASES = {
    "1.5": "1.5b", "1.5b": "1.5b", "3": "3b", "3b": "3b",
    "7": "7b", "7b": "7b",
}
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
    hardware: system_profile.HardwareProfile
    inference: Recommendation
    training: Recommendation
    usable_vram_gb: float
    usable_system_ram_gb: float
    options: PlanOptions

    def to_dict(self):
        return {
            "hardware": self.hardware.to_dict(),
            "budgets": {
                "usable_vram_gb": self.usable_vram_gb,
                "usable_system_ram_gb": self.usable_system_ram_gb,
            },
            "inference": self.inference.to_dict(),
            "training": self.training.to_dict(),
            "options": asdict(self.options),
        }


def _bounded_available(value, maximum):
    return min(value, maximum) if maximum is not None else value


def memory_budgets(profile, options):
    # Keep 25% of total RAM available to the OS/desktop. Available memory is
    # used as the starting point, never total memory as a substitute.
    ram_reserve = max(2.0, profile.system_ram_total_gb * 0.25)
    usable_ram = max(0.0, profile.system_ram_available_gb - ram_reserve)
    usable_ram = _bounded_available(usable_ram, options.max_system_ram_gb)
    available_vram = profile.vram_free_gb
    vram_reserve = 0.0
    if available_vram:
        vram_reserve = 2.0 if profile.vram_total_gb >= 12 else 1.0
    usable_vram = max(0.0, available_vram - vram_reserve)
    usable_vram = _bounded_available(usable_vram, options.max_vram_gb)
    return round(usable_vram, 2), round(usable_ram, 2)


def _training_estimate(size, options):
    spec = MODEL_SPECS[size]
    activation_scale = max(0.5, options.sequence_length / 1024) * max(1, options.batch_size)
    # Baselines include 4-bit weights, LoRA params/optimizer, CUDA workspace,
    # and checkpointed activations at seq=1024, batch=1.
    vram = spec["train_vram"] + (activation_scale - 1.0) * (0.35 + spec["params"] * 0.10)
    ram = spec["train_ram"] + max(0.0, activation_scale - 1.0) * spec["params"] * 0.35
    return round(vram, 2), round(ram, 2)


def _inference_estimate(size, options):
    spec = MODEL_SPECS[size]
    # Conservative KV/cache growth approximation, separate from weight memory.
    context_scale = max(0.25, options.context_length / 8192)
    vram = spec["infer_vram"] + (context_scale - 1.0) * spec["params"] * 0.18
    ram = spec["infer_ram"] + (context_scale - 1.0) * spec["params"] * 0.12
    return round(max(spec["infer_vram"], vram), 2), round(max(spec["infer_ram"], ram), 2)


def _requested_size(value):
    value = str(value or "auto").strip().lower()
    if value == "auto":
        return "auto"
    if value not in MODEL_ALIASES:
        raise ValueError("model must be auto, 1.5b, 3b, or 7b")
    return MODEL_ALIASES[value]


def build_plan(profile=None, options=None):
    options = options or PlanOptions()
    profile = profile or system_profile.detect_hardware(gpu_index=options.gpu_index)
    requested = _requested_size(options.model)
    usable_vram, usable_ram = memory_budgets(profile, options)
    available_vram = _bounded_available(profile.vram_free_gb, options.max_vram_gb)

    rejected = []
    inference_size = "1.5b"
    for size in ("7b", "3b", "1.5b"):
        est_vram, est_ram = _inference_estimate(size, options)
        gpu_fit = bool(available_vram and est_vram <= usable_vram)
        offload_fit = est_ram <= usable_ram
        if gpu_fit or offload_fit:
            inference_size = size
            break
        rejected.append(
            f"Inference {size} rejected: needs about {est_vram:.1f} GB VRAM "
            f"or {est_ram:.1f} GB RAM headroom."
        )
    if requested != "auto":
        est_vram, est_ram = _inference_estimate(requested, options)
        if est_vram <= usable_vram or est_ram <= usable_ram:
            inference_size = requested
        else:
            rejected.append(
                f"Requested inference {requested} cannot preserve memory reserves; using {inference_size}."
            )
    infer_vram, infer_ram = _inference_estimate(inference_size, options)
    infer_offload = bool(available_vram and infer_vram > usable_vram)
    infer_method = "Ollama 4-bit inference" if available_vram else "Ollama 4-bit CPU inference"
    inference = Recommendation(
        enabled=True,
        model_size=inference_size,
        model=MODEL_SPECS[inference_size]["ollama"],
        method=infer_method,
        estimated_vram_gb=min(infer_vram, usable_vram) if usable_vram else 0.0,
        estimated_system_ram_gb=infer_ram if (infer_offload or not available_vram) else min(2.0, infer_ram),
        cpu_offload=infer_offload,
        reason=(
            f"{available_vram:.1f} GB currently free VRAM and {usable_ram:.1f} GB "
            "usable system RAM after independent reserves."
        ),
        rejected=list(rejected),
        settings={"context_length": options.context_length},
    )

    train_rejected = []
    training_size = ""
    runtime_supported = profile.cuda_available and profile.gpu_vendor == "nvidia"
    if not runtime_supported:
        train_rejected.append(
            "Local QLoRA disabled: this bitsandbytes path requires a supported NVIDIA CUDA runtime."
        )
    if options.allow_cpu_offload:
        train_rejected.append(TRAINING_CPU_OFFLOAD_REASON)
    candidates = [requested] if requested != "auto" else ["7b", "3b", "1.5b"]
    for size in candidates:
        est_vram, est_ram = _training_estimate(size, options)
        # Starting ranges prevent a technically close estimate from choosing a
        # much larger model before a smaller attended run proves the stack.
        range_ok = (
            (size == "1.5b" and available_vram >= 4.0)
            or (size == "3b" and available_vram >= 7.5)
            or (size == "7b" and available_vram >= 11.5 and usable_ram >= 16.0)
        )
        direct_fit = est_vram <= usable_vram and est_ram <= usable_ram
        if (
            runtime_supported
            and not options.allow_cpu_offload
            and range_ok
            and direct_fit
        ):
            training_size = size
            break
        reasons = []
        if not range_ok:
            reasons.append("outside the conservative free-VRAM starting range")
        if est_vram > usable_vram:
            reasons.append(f"~{est_vram:.1f} GB VRAM exceeds {usable_vram:.1f} GB budget")
        if est_ram > usable_ram:
            reasons.append(f"~{est_ram:.1f} GB RAM exceeds {usable_ram:.1f} GB budget")
        if options.allow_cpu_offload:
            reasons.append("requested CPU offload backend is unavailable")
        train_rejected.append(f"QLoRA {size} rejected: " + "; ".join(reasons or ["runtime unsupported"]) + ".")

    method = "QLoRA (4-bit NF4)"
    if options.full_finetune:
        dense_size = requested if requested != "auto" else "1.5b"
        dense_vram = round(MODEL_SPECS[dense_size]["params"] * 16 + 4, 1)
        dense_ram = round(MODEL_SPECS[dense_size]["params"] * 8 + 8, 1)
        if not runtime_supported or dense_vram > usable_vram or dense_ram > usable_ram:
            train_rejected.append(
                f"Dense {dense_size} rejected: estimated {dense_vram:.1f} GB VRAM/"
                f"{dense_ram:.1f} GB RAM; it is explicit opt-in and does not fit safely."
            )
            training_size = ""
        else:
            training_size, method = dense_size, "full-parameter bf16 (advanced opt-in)"

    if training_size:
        if method.startswith("full-parameter"):
            est_vram, est_ram = dense_vram, dense_ram
        else:
            est_vram, est_ram = _training_estimate(training_size, options)
        training = Recommendation(
            enabled=True,
            model_size=training_size,
            model=MODEL_SPECS[training_size]["hf"],
            method=method,
            estimated_vram_gb=est_vram,
            estimated_system_ram_gb=est_ram,
            cpu_offload=False,
            reason=(
                f"{available_vram:.1f} GB currently free VRAM; {usable_vram:.1f} GB GPU budget "
                f"and {usable_ram:.1f} GB RAM budget after desktop/OS reserves."
            ),
            rejected=train_rejected,
            settings={
                "quantization": "NF4" if method.startswith("QLoRA") else "none",
                "sequence_length": options.sequence_length,
                "batch_size": options.batch_size,
                "gradient_accumulation": options.gradient_accumulation,
                "gradient_checkpointing": True,
            },
        )
    else:
        training = Recommendation(
            enabled=False,
            model_size="",
            model="",
            method="disabled",
            estimated_vram_gb=0.0,
            estimated_system_ram_gb=0.0,
            cpu_offload=False,
            reason="No supported attended local weight-training plan fits the live memory budgets.",
            rejected=train_rejected,
            settings={},
        )
    return HardwarePlan(profile, inference, training, usable_vram, usable_ram, options)


def format_hardware(profile=None):
    p = profile or system_profile.detect_hardware()
    runtime = "CUDA" if p.cuda_available else "ROCm" if p.rocm_available else "none"
    ram_freshness = (
        "live" if p.system_ram_availability_live else "conservative fallback"
    )
    vram_freshness = (
        "live" if p.vram_availability_live else "conservative fallback"
    )
    return "\n".join([
        "Sonder Runtime hardware",
        f"  OS: {p.os_name} {p.architecture}",
        f"  system RAM: {p.system_ram_available_gb:.1f} GB available / {p.system_ram_total_gb:.1f} GB total ({ram_freshness})",
        f"  GPU: {p.gpu_vendor} {p.gpu_name or '(none)'} | runtime: {runtime}",
        f"  VRAM: {p.vram_free_gb:.1f} GB free / {p.vram_total_gb:.1f} GB total ({vram_freshness})",
        f"  compute capability: {p.compute_capability or 'n/a'}",
        f"  CPU offload hardware/runtime capability: {'yes' if p.cpu_offload_supported else 'no'}; QLoRA backend: disabled",
    ])


def format_plan(plan):
    t, i = plan.training, plan.inference
    lines = [
        format_hardware(plan.hardware),
        "",
        f"Memory budgets: {plan.usable_vram_gb:.1f} GB VRAM; {plan.usable_system_ram_gb:.1f} GB system RAM",
        f"Inference: {i.model} ({i.method})",
        f"  estimate: {i.estimated_vram_gb:.1f} GB VRAM; {i.estimated_system_ram_gb:.1f} GB RAM; CPU offload: {'yes' if i.cpu_offload else 'no'}",
        f"  reason: {i.reason}",
    ]
    if t.enabled:
        lines += [
            f"Training: {t.method} {t.model_size}, GPU {plan.options.gpu_index}, batch {t.settings['batch_size']}, gradient accumulation {t.settings['gradient_accumulation']}",
            f"  base: {t.model}",
            f"  estimate: {t.estimated_vram_gb:.1f} GB VRAM; {t.estimated_system_ram_gb:.1f} GB RAM; CPU offload: {'yes' if t.cpu_offload else 'no'}",
            f"  reason: {t.reason}",
        ]
    else:
        lines += ["Training: disabled", f"  reason: {t.reason}"]
    rejected = i.rejected + t.rejected
    if rejected:
        lines.append("Rejected alternatives:")
        lines.extend(f"  - {item}" for item in rejected)
    return "\n".join(lines)


def state_path():
    return Path(
        sonder_paths.state_path("training_state.json", "SONDER_TRAINING_STATE")
    ).expanduser().resolve()


def _read_state():
    path = state_path()
    if not path.exists():
        return {"status": "never_started", "rollback_model": ROLLBACK_MODEL}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "invalid", "error": "training state is unreadable"}
    if not isinstance(value, dict):
        return {"status": "invalid", "error": "training state must be a JSON object"}
    return value


def _write_state(payload):
    path = state_path()
    _write_json_atomic(path, payload)
    return path


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_export_manifest(path, inspection):
    """Validate and hash the privacy-selection receipt beside a memory export."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("training export selection manifest is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("training export selection manifest is unreadable") from exc
    expected = {
        "schema": 1,
        "format": "sonder-chat-jsonl",
        "accepted": len(inspection.examples),
        "characters": inspection.content_chars,
        "sha256": inspection.sha256,
        "privacy_policy": "exclude-shared-private-markers",
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("training export selection manifest does not match its dataset")
    return hashlib.sha256(raw).hexdigest()


def _resume_signature(plan_payload):
    options = dict((plan_payload or {}).get("options") or {})
    return {
        "model": ((plan_payload or {}).get("training") or {}).get("model"),
        "method": ((plan_payload or {}).get("training") or {}).get("method"),
        "sequence_length": options.get("sequence_length"),
        "batch_size": options.get("batch_size"),
        "gradient_accumulation": options.get("gradient_accumulation"),
        "gpu_index": options.get("gpu_index", 0),
    }


def _disk_ok(path, required_gb):
    probe = Path(path).expanduser().absolute()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        probe = ROOT
    free = shutil.disk_usage(probe).free / 1024**3
    return free >= required_gb, free


def start_training(plan, *, confirmed=False, dry_run=False, resume=False, runner=subprocess.run):
    if dry_run or not plan.training.enabled or not confirmed:
        return _start_training_locked(
            plan, confirmed=confirmed, dry_run=dry_run, resume=resume, runner=runner
        )
    try:
        with _deployment_lock():
            shared_ready, shared_detail = _prepare_shared_alias_lifecycle()
            if not shared_ready:
                return False, f"Training blocked: {shared_detail}."
            if _deployment_journal_path().exists() or _cleanup_pending_path().exists():
                return False, (
                    "Training blocked: deployment recovery/cleanup must be completed "
                    "with `training rollback` or `training deploy` first."
                )
            return _start_training_locked(
                plan, confirmed=confirmed, dry_run=dry_run, resume=resume, runner=runner
            )
    except RuntimeError as exc:
        return False, f"Training blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Training failed safely: {_bounded_error(exc)}"


def _start_training_locked(plan, *, confirmed=False, dry_run=False, resume=False, runner=subprocess.run):
    if dry_run:
        return True, format_plan(plan) + "\nDry run only: no training process started."
    if not plan.training.enabled:
        return False, format_plan(plan)
    if not plan.training.method.startswith("QLoRA"):
        return False, (
            "The dense plan is an advanced feasibility report only; the supported local "
            "weight-update/deployment workflow is QLoRA. Dense training was not started."
        )
    if not confirmed:
        return False, (
            "Training was not started. The first/next run must be attended. Re-run with "
            "`training start --confirm` while watching GPU memory."
        )
    if plan.options.gpu_index < 0:
        return False, "Training GPU index must be zero or greater."
    output_root = Path(os.environ.get("SONDER_LORA_OUT", ROOT / "sonder-personal-lora"))
    current = _read_state()
    if resume:
        if current.get("status") not in {"interrupted", "failed"}:
            return False, "Training resume requires an interrupted or failed run."
        run_id = str(current.get("run_id") or "")
        run_dir = Path(current.get("run_dir") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", run_id) or not run_dir.is_dir():
            return False, "Training resume provenance is missing; start a new run."
        expected_run_dir = (output_root / "runs" / run_id).resolve()
        if run_dir.resolve() != expected_run_dir:
            return False, "Training resume run directory is outside the configured output root."
        run_dir = expected_run_dir
        if current.get("base_hf") != plan.training.model:
            return False, "Training resume plan does not match the interrupted run base model."
        expected_revision = MODEL_SPECS[plan.training.model_size]["hf_revision"]
        if current.get("hf_revision") != expected_revision:
            return False, "Training resume base revision does not match the interrupted run."
        prior_plan_file = run_dir / "training-plan.json"
        try:
            recorded_plan_file = Path(current.get("plan_file") or "").resolve()
        except (OSError, TypeError, ValueError):
            return False, "Training resume plan provenance is invalid."
        recorded_plan_sha256 = str(current.get("plan_sha256") or "")
        if recorded_plan_file != prior_plan_file.resolve():
            return False, "Training resume plan path does not match the interrupted run."
        if (
            len(recorded_plan_sha256) != 64
            or not prior_plan_file.is_file()
            or not hmac.compare_digest(
                _sha256_file(prior_plan_file), recorded_plan_sha256,
            )
        ):
            return False, "Training resume plan changed after the interrupted run."
        try:
            prior = json.loads(prior_plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, "Training resume plan is missing or unreadable."
        if prior.get("hf_revision") != expected_revision:
            return False, "Training resume plan uses a different Hugging Face revision."
        if _resume_signature(prior.get("plan")) != _resume_signature(plan.to_dict()):
            return False, "Training resume settings do not match the interrupted run."
    else:
        run_id = uuid.uuid4().hex
        run_dir = output_root / "runs" / run_id

    def prelaunch_failure(message):
        if not resume:
            try:
                expected = (output_root / "runs" / run_id).resolve()
                if re.fullmatch(r"[0-9a-f]{32}", run_id) and run_dir.resolve() == expected:
                    shutil.rmtree(expected)
            except FileNotFoundError:
                pass
            except OSError:
                return False, message + " The incomplete run directory could not be removed."
        return False, message

    output = run_dir / "adapter"
    ok, free = _disk_ok(run_dir.parent, 3 + MODEL_SPECS[plan.training.model_size]["params"] * 2.2)
    if not ok:
        return False, f"Training not started: only {free:.1f} GB disk free."
    output.mkdir(parents=True, exist_ok=True)
    explicit_data = os.environ.get("SONDER_DATA")
    if resume:
        data_path = Path(prior.get("data_path") or "")
        expected_data_path = (run_dir / "training-data.jsonl").resolve()
        if data_path.resolve() != expected_data_path:
            return False, "Training resume snapshot is outside the immutable run directory."
        if not data_path.is_file():
            return False, "Training resume snapshot is missing or changed."
        try:
            data_inspection = training_data.inspect_jsonl(
                data_path, expected_sha256=str(prior.get("data_sha256") or ""),
            )
        except (OSError, training_data.TrainingDataError) as exc:
            return False, f"Training resume snapshot is invalid: {_bounded_error(exc)}"
        if not data_inspection.examples:
            return False, "Training resume snapshot contains no examples."
        data_source = str(prior.get("data_source") or "")
        if data_source not in {"explicit", "memory_export"}:
            return False, "Training resume dataset source is unsupported."
        if data_source == "memory_export":
            # A generated corpus is immutable for the lifetime of its run.
            # Resume must not silently retrain on a newer live-memory export.
            source_data_path = Path(prior.get("source_data_path") or "")
            if source_data_path.resolve() != data_path.resolve():
                return False, "Training resume generated-dataset provenance is invalid."
            source_data_sha256 = data_inspection.sha256
            if source_data_sha256 != str(prior.get("source_data_sha256") or ""):
                return False, "Training resume dataset changed since the interrupted run."
            selection_manifest_path = Path(
                prior.get("selection_manifest_path") or ""
            )
            expected_selection_path = Path(str(data_path) + ".manifest.json").resolve()
            if selection_manifest_path.resolve() != expected_selection_path:
                return False, "Training resume selection manifest is outside the run."
            try:
                selection_manifest_sha256 = _inspect_export_manifest(
                    selection_manifest_path, data_inspection,
                )
            except (OSError, ValueError) as exc:
                return False, f"Training resume selection evidence is invalid: {_bounded_error(exc)}"
            if selection_manifest_sha256 != str(
                prior.get("selection_manifest_sha256") or ""
            ):
                return False, "Training resume selection manifest changed."
        else:
            source_data_path = Path(explicit_data or (ROOT / "training_data.jsonl"))
            if not source_data_path.is_file():
                return False, "Training resume source dataset is missing."
            source_data_path = source_data_path.resolve()
            try:
                source_inspection = training_data.inspect_jsonl(
                    source_data_path,
                    expected_sha256=str(prior.get("source_data_sha256") or ""),
                )
            except (OSError, training_data.TrainingDataError) as exc:
                return False, f"Training resume source dataset is invalid: {_bounded_error(exc)}"
            source_data_sha256 = source_inspection.sha256
            if str(prior.get("source_data_path") or "") != str(source_data_path):
                return False, "Training resume dataset path does not match the interrupted run."
            selection_manifest_path = ""
            selection_manifest_sha256 = ""
        trusted_fields = {
            "data_path": str(data_path),
            "data_sha256": data_inspection.sha256,
            "data_examples": len(data_inspection.examples),
            "data_bytes": data_inspection.file_bytes,
            "data_content_chars": data_inspection.content_chars,
            "data_source": data_source,
            "source_data_path": str(source_data_path),
            "source_data_sha256": source_data_sha256,
            "selection_manifest_path": str(selection_manifest_path),
            "selection_manifest_sha256": selection_manifest_sha256,
        }
        if any(current.get(key) != value for key, value in trusted_fields.items()):
            return False, "Training resume dataset provenance does not match trusted state."
        claim = run_dir / ".launch-claimed"
        if claim.exists():
            try:
                claimed_pid = int(claim.read_text(encoding="ascii").strip())
            except (OSError, TypeError, ValueError):
                return False, "Training resume launch owner is unreadable."
            if _pid_alive(claimed_pid):
                return False, "Training resume blocked: the prior training child is still running."
            with contextlib.suppress(OSError):
                claim.unlink()
    else:
        data_path = run_dir / "training-data.jsonl"
        if explicit_data:
            data_source = "explicit"
            source_data_path = Path(explicit_data)
            if not source_data_path.is_file():
                return prelaunch_failure("Explicit training dataset does not exist.")
            source_data_path = source_data_path.resolve()
            try:
                source_inspection = training_data.inspect_jsonl(source_data_path)
            except (OSError, training_data.TrainingDataError) as exc:
                return prelaunch_failure(
                    f"Explicit training dataset is invalid: {_bounded_error(exc)}"
                )
            if not source_inspection.examples:
                return prelaunch_failure("Explicit training dataset contains no examples.")
            source_data_sha256 = source_inspection.sha256
            try:
                shutil.copyfile(source_data_path, data_path)
                data_inspection = training_data.inspect_jsonl(
                    data_path, expected_sha256=source_data_sha256,
                )
            except (OSError, training_data.TrainingDataError) as exc:
                return prelaunch_failure(
                    "Training dataset changed while creating the immutable run "
                    f"snapshot: {_bounded_error(exc)}"
                )
            selection_manifest_path = ""
            selection_manifest_sha256 = ""
        else:
            # Never reuse a stale default export.  Each new run gets a fresh,
            # policy-filtered export directly inside its immutable run folder.
            data_source = "memory_export"
            try:
                import export_training_data
                exported = export_training_data.main(str(data_path))
            except Exception as exc:
                return prelaunch_failure(
                    f"Training data preparation failed: {_bounded_error(exc)}"
                )
            if not exported:
                return prelaunch_failure(
                    "Training data preparation produced no eligible examples."
                )
            source_data_path = data_path.resolve()
            try:
                data_inspection = training_data.inspect_jsonl(source_data_path)
            except (OSError, training_data.TrainingDataError) as exc:
                return prelaunch_failure(
                    f"Generated training dataset is invalid: {_bounded_error(exc)}"
                )
            source_data_sha256 = data_inspection.sha256
            selection_manifest_path = Path(
                str(source_data_path) + ".manifest.json"
            ).resolve()
            try:
                selection_manifest_sha256 = _inspect_export_manifest(
                    selection_manifest_path, data_inspection,
                )
            except (OSError, ValueError) as exc:
                return prelaunch_failure(
                    f"Training selection evidence is invalid: {_bounded_error(exc)}"
                )
    data_path = data_path.resolve()
    data_sha256 = data_inspection.sha256
    launch_token = secrets.token_urlsafe(32)
    manifest = {
        "schema": 2,
        "run_id": run_id,
        "base_hf": plan.training.model,
        "hf_revision": MODEL_SPECS[plan.training.model_size]["hf_revision"],
        "base_ollama": MODEL_SPECS[plan.training.model_size]["ollama"],
        "model_size": plan.training.model_size,
        "method": plan.training.method,
        "created_ts": int(time.time()),
        "data_path": str(data_path),
        "data_sha256": data_sha256,
        "data_examples": len(data_inspection.examples),
        "data_bytes": data_inspection.file_bytes,
        "data_content_chars": data_inspection.content_chars,
        "data_source": data_source,
        "source_data_path": str(source_data_path),
        "source_data_sha256": source_data_sha256,
        "selection_manifest_path": str(selection_manifest_path),
        "selection_manifest_sha256": selection_manifest_sha256,
        "adapter_dir": str(output.resolve()),
        "gpu_index": plan.options.gpu_index,
        "resume": bool(resume),
        "launch_token_sha256": hashlib.sha256(launch_token.encode("utf-8")).hexdigest(),
        "plan": plan.to_dict(),
    }
    plan_file = run_dir / "training-plan.json"
    _write_json_atomic(plan_file, manifest)
    state = {
        "status": "running",
        "started_ts": int(time.time()),
        "adapter_dir": str(output),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "plan_file": str(plan_file),
        "plan_sha256": _sha256_file(plan_file),
        "data_path": manifest["data_path"],
        "data_sha256": manifest["data_sha256"],
        "data_examples": manifest["data_examples"],
        "data_bytes": manifest["data_bytes"],
        "data_content_chars": manifest["data_content_chars"],
        "data_source": manifest["data_source"],
        "source_data_path": manifest["source_data_path"],
        "source_data_sha256": manifest["source_data_sha256"],
        "selection_manifest_path": manifest["selection_manifest_path"],
        "selection_manifest_sha256": manifest["selection_manifest_sha256"],
        "base_hf": manifest["base_hf"],
        "hf_revision": manifest["hf_revision"],
        "base_ollama": manifest["base_ollama"],
        "rollback_model": ROLLBACK_MODEL,
    }
    _write_state(state)
    env = os.environ.copy()
    env.update({
        "SONDER_BASE": manifest["base_hf"],
        "SONDER_HF_REVISION": manifest["hf_revision"],
        "SONDER_DATA": str(data_path),
        "SONDER_LORA_OUT": str(output),
        "SONDER_MAX_LEN": str(plan.options.sequence_length),
        "SONDER_BATCH_SIZE": str(plan.options.batch_size),
        "SONDER_GRAD_ACCUM": str(plan.options.gradient_accumulation),
        # Defense in depth: the supported Trainer path must stay GPU-resident.
        "SONDER_ALLOW_CPU_OFFLOAD": "0",
        "SONDER_TRAIN_GPU_BUDGET_GB": str(plan.usable_vram_gb),
        "SONDER_TRAIN_RAM_BUDGET_GB": str(plan.usable_system_ram_gb),
        "SONDER_TRAINING_MANIFEST": str(plan_file),
        "SONDER_TRAINING_LAUNCH_TOKEN": launch_token,
        "SONDER_RESUME": "1" if resume else "0",
    })
    # Bind the selected physical GPU before torch initializes. Inside the child
    # it is intentionally device 0 because CUDA_VISIBLE_DEVICES remaps it.
    env["CUDA_VISIBLE_DEVICES"] = str(plan.options.gpu_index)
    try:
        result = runner([sys.executable, str(ROOT / "qlora_train.py")], cwd=ROOT, env=env)
    except KeyboardInterrupt:
        with contextlib.suppress(OSError):
            state["plan_sha256"] = _sha256_file(plan_file)
        state.update(status="interrupted", ended_ts=int(time.time()))
        _write_state(state)
        return False, "Training interrupted cleanly; checkpoints were preserved for resume."
    except OSError as exc:
        state.update(status="failed", ended_ts=int(time.time()), error=str(exc))
        _write_state(state)
        return False, "Training process could not start; the run is preserved for an explicit resume."
    with contextlib.suppress(OSError):
        state["plan_sha256"] = _sha256_file(plan_file)
    with contextlib.suppress(OSError):
        (run_dir / ".launch-claimed").unlink()
    if result.returncode:
        state.update(status="failed", ended_ts=int(time.time()), returncode=result.returncode)
        _write_state(state)
        return False, "Training failed; checkpoints were preserved. Check the output above before resuming."
    adapter_ok, detail = validate_adapter(output, manifest["base_hf"])
    if not adapter_ok:
        state.update(status="failed_validation", ended_ts=int(time.time()), error=detail)
        _write_state(state)
        return False, f"Training process exited successfully but adapter validation failed: {detail}"
    state.update(
        status="trained",
        ended_ts=int(time.time()),
        manifest=str(output / "training-manifest.json"),
        data_sha256=str(detail["data_sha256"]),
        source_data_sha256=str(detail["source_data_sha256"]),
        plan_sha256=_sha256_file(plan_file),
        manifest_sha256=_sha256_file(output / "training-manifest.json"),
        artifact_sha256=dict(detail["artifact_sha256"]),
        artifact_sizes=dict(detail["artifact_sizes"]),
    )
    _write_state(state)
    return True, f"Training completed; adapter saved at {output}. Run `training deploy`."


def training_status():
    return json.dumps(_read_state(), indent=2, sort_keys=True)


def _path_has_control_chars(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in str(value))


def _path_uses_symlink(path):
    """Return True when any existing component of *path* is a symlink."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            return True
    return False


def _json_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected a JSON object")
    return value


def validate_adapter(adapter_dir, expected_base=""):
    if _path_has_control_chars(adapter_dir):
        return False, "adapter path contains control characters"
    adapter_dir = Path(adapter_dir)
    config_path = adapter_dir / "adapter_config.json"
    manifest_path = adapter_dir / "training-manifest.json"
    weights_path = adapter_dir / "adapter_model.safetensors"
    required = (config_path, manifest_path, weights_path)
    if any(not path.is_file() for path in required):
        return False, (
            "adapter_config.json, adapter_model.safetensors, and "
            "training-manifest.json are required"
        )
    if any(path.is_symlink() for path in required) or _path_uses_symlink(adapter_dir):
        return False, "adapter artifacts must not use symbolic links"
    try:
        config = _json_object(config_path, "adapter_config.json")
        manifest = _json_object(manifest_path, "training-manifest.json")
    except ValueError as exc:
        return False, str(exc)
    configured = str(config.get("base_model_name_or_path") or "").rstrip("/")
    trained = str(manifest.get("base_hf") or "").rstrip("/")
    if not configured or configured != trained:
        return False, f"adapter/base mismatch: PEFT={configured!r}, manifest={trained!r}"
    if expected_base and trained != expected_base.rstrip("/"):
        return False, f"adapter base {trained!r} does not match expected {expected_base!r}"
    if config.get("invocation_string") or config.get("alora_invocation_tokens"):
        return False, (
            "aLoRA invocation adapters are not supported by the offline pinned-base "
            "conversion path"
        )
    ollama_base = str(manifest.get("base_ollama") or "")
    size = str(manifest.get("model_size") or "")
    if size not in MODEL_SPECS or ollama_base != MODEL_SPECS[size]["ollama"]:
        return False, "manifest Ollama base is not the exact mapped Qwen2.5-Coder base"
    if (
        trained != MODEL_SPECS[size]["hf"]
        or manifest.get("hf_revision") != MODEL_SPECS[size]["hf_revision"]
    ):
        return False, "manifest Hugging Face base is not the reviewed pinned commit"
    if manifest.get("schema") != 2:
        return False, "adapter manifest schema is unsupported"
    try:
        completed = int(manifest.get("completed_ts") or 0)
        consumed = int(manifest.get("launch_consumed_ts") or 0)
    except (TypeError, ValueError):
        return False, "adapter manifest completion timestamps are invalid"
    if completed <= 0 or consumed <= 0:
        return False, "adapter manifest does not prove a completed authorized run"
    hashes = manifest.get("artifact_sha256")
    sizes = manifest.get("artifact_sizes")
    if not isinstance(hashes, dict) or not isinstance(sizes, dict):
        return False, "manifest is missing adapter artifact hashes"
    for name, path in (
        ("adapter_config.json", config_path),
        ("adapter_model.safetensors", weights_path),
    ):
        expected_hash = str(hashes.get(name) or "")
        expected_size = sizes.get(name)
        if len(expected_hash) != 64 or not isinstance(expected_size, int) or expected_size <= 0:
            return False, f"manifest has invalid integrity metadata for {name}"
        if path.stat().st_size != expected_size or _sha256_file(path) != expected_hash:
            return False, f"adapter artifact integrity check failed for {name}"
    return True, manifest


def _validate_deployment_adapter(adapter_dir, state):
    """Bind deployment to one completed run issued by this lifecycle controller."""
    if state.get("status") not in {"trained", "deployed", "rolled_back"}:
        return False, "training state must identify a completed trusted run"
    run_id = str(state.get("run_id") or "")
    raw_run_dir = str(state.get("run_dir") or "")
    raw_state_adapter = str(state.get("adapter_dir") or "")
    raw_plan = str(state.get("plan_file") or "")
    if not run_id or any(
        not value or _path_has_control_chars(value)
        for value in (raw_run_dir, raw_state_adapter, raw_plan)
    ):
        return False, "trusted training provenance is incomplete"
    try:
        requested = Path(adapter_dir).resolve(strict=True)
        run_dir = Path(raw_run_dir).resolve(strict=True)
        state_adapter = Path(raw_state_adapter).resolve(strict=True)
        plan_path = Path(raw_plan).resolve(strict=True)
    except OSError as exc:
        return False, f"trusted training provenance is unavailable: {exc}"
    if any(_path_uses_symlink(path) for path in (adapter_dir, raw_run_dir, raw_state_adapter, raw_plan)):
        return False, "trusted training provenance must not use symbolic links"
    if run_dir.name != run_id or requested != state_adapter or requested != run_dir / "adapter":
        return False, "adapter path does not match the completed trusted run"
    if plan_path != run_dir / "training-plan.json":
        return False, "training plan is outside the completed trusted run"
    manifest_path = requested / "training-manifest.json"
    if state.get("manifest") and Path(state["manifest"]).resolve() != manifest_path:
        return False, "training state manifest path does not match the trusted adapter"
    integrity_files = (
        ("plan_sha256", plan_path),
        ("manifest_sha256", manifest_path),
    )
    for key, path in integrity_files:
        expected_hash = str(state.get(key) or "")
        if len(expected_hash) != 64 or _sha256_file(path) != expected_hash:
            return False, f"trusted training provenance integrity failed for {path.name}"
    try:
        plan = _json_object(plan_path, "training-plan.json")
        manifest = _json_object(manifest_path, "training-manifest.json")
    except ValueError as exc:
        return False, str(exc)
    if plan.get("schema") != 2 or manifest.get("schema") != 2:
        return False, "training provenance schema is unsupported"
    try:
        completed = int(manifest.get("completed_ts") or 0)
        consumed = int(manifest.get("launch_consumed_ts") or 0)
    except (TypeError, ValueError):
        return False, "adapter manifest completion timestamps are invalid"
    if completed <= 0 or consumed <= 0:
        return False, "adapter manifest does not prove a completed authorized run"
    immutable = (
        "run_id", "base_hf", "hf_revision", "base_ollama", "model_size", "method",
        "data_path", "data_sha256", "data_examples", "data_bytes",
        "data_content_chars", "data_source", "source_data_path",
        "source_data_sha256", "selection_manifest_path",
        "selection_manifest_sha256",
        "adapter_dir", "gpu_index",
    )
    for key in immutable:
        if plan.get(key) != manifest.get(key):
            return False, f"adapter manifest does not match training plan field {key}"
    expected = {
        "run_id": run_id,
        "base_hf": state.get("base_hf"),
        "hf_revision": state.get("hf_revision"),
        "base_ollama": state.get("base_ollama"),
        "adapter_dir": str(requested),
        "data_sha256": state.get("data_sha256"),
        "data_examples": state.get("data_examples"),
        "data_bytes": state.get("data_bytes"),
        "data_content_chars": state.get("data_content_chars"),
        "selection_manifest_sha256": state.get("selection_manifest_sha256"),
    }
    for key, value in expected.items():
        if str(manifest.get(key) or "") != str(value or ""):
            return False, f"training state does not match adapter field {key}"
    try:
        snapshot_path = Path(manifest["data_path"]).resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        return False, f"trusted training dataset snapshot is unavailable: {exc}"
    if snapshot_path != run_dir / "training-data.jsonl":
        return False, "trusted training dataset snapshot is outside the run"
    try:
        inspection = training_data.inspect_jsonl(
            snapshot_path, expected_sha256=str(manifest.get("data_sha256") or ""),
        )
    except (OSError, training_data.TrainingDataError):
        return False, "trusted training dataset snapshot integrity failed"
    dataset_fields = {
        "data_examples": len(inspection.examples),
        "data_bytes": inspection.file_bytes,
        "data_content_chars": inspection.content_chars,
    }
    if any(manifest.get(key) != value for key, value in dataset_fields.items()):
        return False, "trusted training dataset snapshot metadata changed"
    if manifest.get("data_source") == "memory_export":
        try:
            selection_path = Path(
                manifest.get("selection_manifest_path") or ""
            ).resolve(strict=True)
        except OSError:
            return False, "trusted training selection manifest is unavailable"
        if selection_path != Path(str(snapshot_path) + ".manifest.json"):
            return False, "trusted training selection manifest is outside the run"
        try:
            selection_hash = _inspect_export_manifest(selection_path, inspection)
        except (OSError, ValueError):
            return False, "trusted training selection manifest integrity failed"
        if selection_hash != str(manifest.get("selection_manifest_sha256") or ""):
            return False, "trusted training selection manifest changed"
    elif (
        manifest.get("selection_manifest_path")
        or manifest.get("selection_manifest_sha256")
    ):
        return False, "explicit training data must not claim export selection evidence"
    for key in ("artifact_sha256", "artifact_sizes"):
        if state.get(key) != manifest.get(key):
            return False, f"training state does not match adapter {key}"
    ok, detail = validate_adapter(requested, state.get("base_hf", ""))
    if not ok:
        return False, detail
    return True, detail


def _converter_path(explicit=""):
    roots = [explicit, os.environ.get("SONDER_LLAMA_CPP", ""), ROOT / "llama.cpp", ROOT / "third_party" / "llama.cpp"]
    for root in roots:
        if not root:
            continue
        candidate = Path(root)
        if candidate.is_file() and candidate.name == "convert_lora_to_gguf.py":
            return candidate
        candidate = candidate / "convert_lora_to_gguf.py"
        if candidate.exists():
            return candidate
    return None


def _stage_reviewed_converter(converter_path, destination, runner=subprocess.run):
    """Export exact reviewed Git objects; never execute the mutable checkout."""
    converter_path = Path(converter_path).resolve()
    destination = Path(destination).resolve()
    git = shutil.which("git") or "git"
    git_env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    git_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    # `git archive` otherwise honors the user's Windows autocrlf setting and
    # transforms reviewed LF blobs before extraction, defeating exact object
    # verification. Keep command argv stable while applying an isolated config.
    git_env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.autocrlf",
        "GIT_CONFIG_VALUE_0": "false",
    })

    def run(arguments, *, timeout=30):
        return _run_external(
            runner,
            [git, "-C", str(converter_path.parent), *arguments],
            capture_output=True,
            text=True,
            env=git_env,
            timeout=timeout,
        )

    root_result = run(["rev-parse", "--show-toplevel"])
    if root_result.returncode:
        return False, "llama.cpp converter is not in a reviewed Git checkout"
    try:
        root = Path((root_result.stdout or "").strip()).resolve(strict=True)
    except OSError:
        return False, "llama.cpp checkout root could not be verified"
    if converter_path != root / "convert_lora_to_gguf.py":
        return False, "llama.cpp converter must be the reviewed checkout root script"

    revision = run(["cat-file", "-e", f"{LLAMA_CPP_REVISION}^{{commit}}"])
    if revision.returncode:
        return False, (
            "llama.cpp checkout does not contain reviewed converter commit "
            f"{LLAMA_CPP_REVISION}"
        )
    protected = ["--", "convert_lora_to_gguf.py", "conversion", "gguf-py"]
    tree = run([
        "ls-tree", "-r", "--full-tree", LLAMA_CPP_REVISION, *protected,
    ])
    if tree.returncode or not (tree.stdout or "").strip():
        return False, "llama.cpp converter dependency tree could not be sealed"
    sealed_blobs = {}
    try:
        for line in (tree.stdout or "").splitlines():
            metadata, relative = line.split("\t", 1)
            mode, object_type, object_id = metadata.split()
            relative_path = Path(relative)
            if (
                object_type != "blob"
                or mode not in {"100644", "100755"}
                or not re.fullmatch(r"[0-9a-f]{40}", object_id)
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative in sealed_blobs
            ):
                raise ValueError("unsafe tree entry")
            sealed_blobs[relative_path.as_posix()] = (mode, object_id)
    except (TypeError, ValueError):
        return False, "llama.cpp converter tree listing is invalid"
    tree_sha256 = hashlib.sha256(
        (tree.stdout or "").encode("utf-8", "strict")
    ).hexdigest()
    if tree_sha256 != LLAMA_CPP_TREE_SHA256:
        return False, "llama.cpp converter dependency tree seal does not match"

    archive = destination.with_name(destination.name + ".zip")
    if destination.exists() or archive.exists():
        return False, "llama.cpp converter staging path already exists"
    destination.parent.mkdir(parents=True, exist_ok=True)
    archived = run([
        "archive", "--format=zip", f"--output={archive}",
        LLAMA_CPP_REVISION, *protected,
    ], timeout=120)
    if archived.returncode or not archive.is_file():
        with contextlib.suppress(OSError):
            archive.unlink()
        return False, "reviewed llama.cpp converter archive could not be materialized"
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            for member in members:
                name = member.filename.replace("\\", "/")
                parts = tuple(part for part in name.split("/") if part)
                file_type = (member.external_attr >> 16) & 0o170000
                if (
                    not parts
                    or name.startswith(("/", "\\"))
                    or ":" in parts[0]
                    or ".." in parts
                    or file_type == 0o120000
                ):
                    return False, "reviewed llama.cpp archive contains an unsafe path"
            destination.mkdir()
            bundle.extractall(destination)
    except (OSError, ValueError, zipfile.BadZipFile):
        shutil.rmtree(destination, ignore_errors=True)
        return False, "reviewed llama.cpp converter archive is invalid"
    finally:
        with contextlib.suppress(OSError):
            archive.unlink()

    staged_converter = destination / "convert_lora_to_gguf.py"
    if not staged_converter.is_file() or staged_converter.is_symlink():
        shutil.rmtree(destination, ignore_errors=True)
        return False, "reviewed llama.cpp converter is missing from staged archive"
    manifest = hashlib.sha256()
    files = sorted(
        (path for path in destination.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(destination).as_posix(),
    )
    extracted_paths = {
        path.relative_to(destination).as_posix() for path in files
    }
    if extracted_paths != set(sealed_blobs) or any(path.is_symlink() for path in files):
        shutil.rmtree(destination, ignore_errors=True)
        return False, "reviewed llama.cpp staged source manifest is incomplete"
    for path in files:
        relative = path.relative_to(destination).as_posix()
        size = path.stat().st_size
        blob_digest = hashlib.sha1()
        blob_digest.update(f"blob {size}\0".encode("ascii"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                blob_digest.update(block)
        if not hmac.compare_digest(blob_digest.hexdigest(), sealed_blobs[relative][1]):
            shutil.rmtree(destination, ignore_errors=True)
            return False, "reviewed llama.cpp staged blob does not match sealed Git object"
        manifest.update(relative.encode("utf-8", "strict") + b"\0")
        manifest.update(str(size).encode("ascii") + b"\0")
        manifest.update(_sha256_file(path).encode("ascii") + b"\n")
    return True, {
        "root": str(root),
        "converter": str(staged_converter),
        "revision": LLAMA_CPP_REVISION,
        "tree_sha256": tree_sha256,
        "staged_manifest_sha256": manifest.hexdigest(),
    }


def _run_external(runner, command, *, ollama_command=False, **kwargs):
    if ollama_command:
        try:
            kwargs["env"] = ollama_endpoint.client_environment(kwargs.get("env"))
        except ValueError as exc:
            return subprocess.CompletedProcess(
                command, 125, stdout="", stderr="Ollama endpoint blocked: %s" % exc,
            )
    try:
        return runner(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command, 124, stdout=exc.stdout or "", stderr="command timed out"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 126, stdout="", stderr=str(exc))


def _model_show_status(result, model):
    if result.returncode == 0:
        return "exists"
    output = "%s\n%s" % (result.stdout or "", result.stderr or "")
    lowered = output.lower()
    model_root = str(model).split(":", 1)[0].lower()
    if "not found" in lowered and model_root in lowered:
        return "missing"
    return "error"


def _show_identity(result):
    if result.returncode:
        return ""
    normalized = (result.stdout or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest() if normalized else ""


def _bounded_error(exc, limit=300):
    value = " ".join(str(exc).split())
    return value[:limit] or type(exc).__name__


def _ollama_executable(explicit=""):
    return (
        explicit or os.environ.get("SONDER_OLLAMA_EXE", "").strip()
        or shutil.which("ollama") or "ollama"
    )


def _shared_alias_paths():
    """Stable per-user namespace for one Ollama endpoint's global aliases."""
    transport_origin = promotion_eval._local_ollama_origin()
    parsed = urllib.parse.urlparse(transport_origin)
    namespace_id = f"loopback:{parsed.port or 11434}"
    namespace = hashlib.sha256(namespace_id.encode("utf-8")).hexdigest()[:24]
    root = (Path.home() / ".sonder" / "locks").resolve()
    prefix = root / f"ollama-{namespace}"
    return {
        "origin": namespace_id,
        "transport_origin": transport_origin,
        "lock": prefix.with_name(prefix.name + ".alias.lock"),
        "transition": prefix.with_name(prefix.name + ".alias-transition.json"),
        "owner": prefix.with_name(prefix.name + ".alias-owner.json"),
    }


def _read_shared_alias_record(kind):
    paths = _shared_alias_paths()
    path = paths[kind]
    if not path.exists():
        return None
    record = _json_object(path, f"shared alias {kind}")
    if record.get("schema") != 1 or record.get("origin") != paths["origin"]:
        raise ValueError(f"shared alias {kind} is invalid")
    return record


def _clear_shared_alias_transition(deployment_id, policy_token):
    paths = _shared_alias_paths()
    record = _read_shared_alias_record("transition")
    if record is None:
        return False
    if (
        record.get("deployment_id") != deployment_id
        or not hmac.compare_digest(
            str(record.get("policy_token") or ""), str(policy_token or "")
        )
        or os.path.normcase(str(record.get("policy_path") or ""))
        != os.path.normcase(str(runtime_policy.policy_path().resolve()))
    ):
        return False
    paths["transition"].unlink()
    return True


def _claim_shared_alias_transition(deployment_id, policy_token):
    paths = _shared_alias_paths()
    if paths["transition"].exists():
        raise RuntimeError("another policy owns an unfinished personal-alias transition")
    payload = {
        "schema": 1,
        "origin": paths["origin"],
        "deployment_id": deployment_id,
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "journal_path": str(_deployment_journal_path()),
        "policy_token": policy_token,
        "phase": "claiming",
        "created_ts": int(time.time()),
    }
    _write_json_atomic(paths["transition"], payload)
    return payload


def _advance_shared_alias_transition(payload, phase):
    payload = dict(payload)
    payload.update(phase=phase, updated_ts=int(time.time()))
    _write_json_atomic(_shared_alias_paths()["transition"], payload)
    return payload


def _prepare_shared_alias_lifecycle(*, require_owner=False):
    """Block cross-policy recovery and clear only a pre-reservation orphan."""
    owner = _read_shared_alias_record("owner")
    owner_is_foreign = owner is not None and os.path.normcase(
        str(owner.get("policy_path") or "")
    ) != os.path.normcase(str(runtime_policy.policy_path().resolve()))
    if require_owner and owner_is_foreign:
        return False, "sonder-personal:latest is owned by another runtime policy"
    record = _read_shared_alias_record("transition")
    if record is None:
        local_journal = _deployment_journal_path()
        if owner_is_foreign and local_journal.exists():
            try:
                pending = _json_object(local_journal, "deployment journal")
            except ValueError:
                return False, "pending policy transition is unreadable"
            if (pending.get("operation") or "deploy") == "deploy":
                return False, (
                    "pending deployment conflicts with a foreign personal-alias owner"
                )
        return True, ""
    if owner_is_foreign:
        return False, (
            "unfinished personal-alias transition conflicts with the persistent owner"
        )
    current_policy = str(runtime_policy.policy_path().resolve())
    if os.path.normcase(str(record.get("policy_path") or "")) != os.path.normcase(
        current_policy
    ):
        return False, "unfinished personal-alias transition belongs to another runtime policy"
    journal_path = Path(str(record.get("journal_path") or ""))
    if journal_path == _deployment_journal_path() and journal_path.exists():
        return True, ""
    if record.get("phase") in {"claiming", "clearing"} and not journal_path.exists():
        try:
            if _clear_shared_alias_transition(
                str(record.get("deployment_id") or ""),
                str(record.get("policy_token") or ""),
            ):
                return True, "orphaned alias claim cleared"
        except (OSError, TypeError, ValueError):
            pass
    return False, "personal-alias transition recovery evidence is incomplete"


def _validate_shared_alias_owner(
    *, prior_policy, personal_status, personal_identity, personal_digest, state,
):
    del prior_policy, state  # Unowned aliases require an explicit migration/removal.
    owner = _read_shared_alias_record("owner")
    current_policy = str(runtime_policy.policy_path().resolve())
    if owner is not None:
        if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
            current_policy
        ):
            return False, "sonder-personal:latest is owned by another runtime policy"
        if (
            personal_status != "exists"
            or owner.get("model") != PERSONAL_MODEL
            or owner.get("identity") != personal_identity
            or owner.get("digest") != personal_digest
        ):
            return False, "owned personal alias identity no longer matches its record"
        return True, owner
    if personal_status == "exists":
        return False, (
            "existing unowned sonder-personal:latest cannot be adopted implicitly; "
            "remove or explicitly migrate the legacy alias before deployment"
        )
    return True, None


def _write_shared_alias_owner(*, deployment_id, identity, digest):
    if not re.fullmatch(r"[0-9a-f]{64}", str(identity or "")):
        raise ValueError("shared alias owner identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
        raise ValueError("shared alias owner digest is invalid")
    paths = _shared_alias_paths()
    _write_json_atomic(paths["owner"], {
        "schema": 1,
        "origin": paths["origin"],
        "model": PERSONAL_MODEL,
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "deployment_id": deployment_id,
        "identity": identity,
        "digest": digest,
        "updated_ts": int(time.time()),
    })


def _deployment_journal_path():
    # The transition marker is bound to the protected policy, not to the
    # caller's SONDER_HOME.  Processes with different homes can still share a
    # policy override and must observe the same deployment reservation.
    return runtime_policy.transition_path()


def _cleanup_pending_path():
    policy = runtime_policy.policy_path().resolve()
    return policy.with_name(policy.name + ".model-cleanup.json")


def _validated_pending_cleanup_models(payload):
    models = payload.get("models") if isinstance(payload, dict) else None
    if payload.get("schema") != 1 or not isinstance(models, list) or len(models) > 32:
        raise ValueError("pending model cleanup is invalid")
    if any(
        not isinstance(model, str)
        or len(model) > 160
        or not model.startswith(("sonder-personal-candidate:", "sonder-personal-previous:"))
        or not runtime_policy._MODEL_RE.fullmatch(model)
        for model in models
    ):
        raise ValueError("pending model cleanup contains unsafe aliases")
    return models


def _record_pending_model_cleanup(model):
    model = str(model)
    if (
        len(model) > 160
        or not model.startswith(("sonder-personal-candidate:", "sonder-personal-previous:"))
        or not runtime_policy._MODEL_RE.fullmatch(model)
    ):
        return False
    path = _cleanup_pending_path()
    with runtime_policy._policy_file_lock():
        models = []
        if path.exists():
            payload = _json_object(path, "pending model cleanup")
            models = _validated_pending_cleanup_models(payload)
        models = sorted(set(models) | {model})
        if len(models) > 32:
            raise ValueError("pending model cleanup is full")
        _write_json_atomic(path, {"schema": 1, "models": models, "updated_ts": int(time.time())})
    return True


def _forget_pending_model_cleanup(model):
    """Release cleanup ownership only after an alias is proved absent."""
    path = _cleanup_pending_path()
    with runtime_policy._policy_file_lock():
        if not path.exists():
            return True
        payload = _json_object(path, "pending model cleanup")
        models = _validated_pending_cleanup_models(payload)
        remaining = sorted({item for item in models if isinstance(item, str) and item != model})
        if remaining:
            _write_json_atomic(
                path,
                {"schema": 1, "models": remaining, "updated_ts": int(time.time())},
            )
        else:
            path.unlink()
    return True


def _reconcile_pending_model_cleanup(*, ollama="", runner=subprocess.run):
    path = _cleanup_pending_path()
    try:
        with runtime_policy._policy_file_lock():
            if not path.exists():
                return True, ""
            payload = _json_object(path, "pending model cleanup")
    except ValueError as exc:
        return False, f"pending model cleanup is unreadable: {_bounded_error(exc)}"
    try:
        models = _validated_pending_cleanup_models(payload)
    except ValueError as exc:
        return False, str(exc)

    def run(command, **kwargs):
        return _run_external(runner, command, ollama_command=True, **kwargs)

    ollama = _ollama_executable(ollama)
    removed = []
    for model in models:
        run([ollama, "rm", model], capture_output=True, text=True, timeout=30)
        probe = run([ollama, "show", model], capture_output=True, text=True, timeout=30)
        if _model_show_status(probe, model) == "missing":
            removed.append(model)
    try:
        with runtime_policy._policy_file_lock():
            current = _json_object(path, "pending model cleanup") if path.exists() else {
                "schema": 1, "models": []
            }
            current_models = _validated_pending_cleanup_models(current)
            remaining = sorted({
                item for item in current_models
                if isinstance(item, str) and item not in set(removed)
            })
            if remaining:
                _write_json_atomic(
                    path,
                    {"schema": 1, "models": remaining, "updated_ts": int(time.time())},
                )
            elif path.exists():
                path.unlink()
    except (OSError, ValueError) as exc:
        return False, f"pending model cleanup ledger could not be updated: {exc}"
    if any(model not in removed for model in models):
        return False, "pending model aliases could not be removed"
    return True, "pending model cleanup completed"


def _write_deployment_journal(payload):
    _write_json_atomic(_deployment_journal_path(), payload)


def _clear_deployment_journal(deployment_id):
    path = _deployment_journal_path()
    try:
        current = _json_object(path, "deployment journal")
    except ValueError:
        return False
    if current.get("deployment_id") != deployment_id:
        return False
    try:
        shared = (
            (current.get("operation") or "deploy") == "deploy"
            and current.get("shared_alias_transition") is True
        )
        if shared:
            transition = _read_shared_alias_record("transition")
            if transition is None:
                return False
            transition = _advance_shared_alias_transition(transition, "clearing")
        finished = runtime_policy.finish_transition(
            deployment_id, current.get("policy_token", "")
        )
        if not finished:
            return False
        if shared and not _clear_shared_alias_transition(
            deployment_id, current.get("policy_token", "")
        ):
            return False
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _reconcile_pending_deployment(*, ollama="", runner=subprocess.run):
    """Recover a policy-bound deployment or rollback transition."""
    path = _deployment_journal_path()
    if not path.exists():
        return True, ""
    try:
        journal = _json_object(path, "deployment journal")
    except ValueError as exc:
        return False, f"pending deployment journal is unreadable: {_bounded_error(exc)}"
    operation = str(journal.get("operation") or "deploy")
    required = {
        "schema", "deployment_id", "state_path", "prior_models",
        "prior_policy_revision", "last_policy_revision", "phase",
        "policy_path", "policy_token",
    }
    if journal.get("schema") != 1 or not required.issubset(journal):
        return False, "pending deployment journal is incomplete"
    if operation not in {"deploy", "rollback"}:
        return False, "pending deployment journal has invalid operation"
    string_fields = (
        "deployment_id", "state_path", "phase", "policy_path", "policy_token",
    )
    if any(
        not isinstance(journal.get(key), str)
        or not journal[key]
        or len(journal[key]) > 1024
        or _path_has_control_chars(journal[key])
        for key in string_fields
    ):
        return False, "pending deployment journal has invalid text fields"
    allowed_phases = {
        "reserved", "prepared", "backed_up", "quiesced", "publishing",
        "published", "verified", "activated", "policy_updated", "committed",
    }
    if journal.get("phase") not in allowed_phases:
        return False, "pending deployment journal has invalid phase"
    for key in ("prior_policy_revision", "last_policy_revision"):
        if type(journal.get(key)) is not int or journal[key] < 0:
            return False, "pending deployment journal has invalid policy revision"
    prior_models_value = journal.get("prior_models")
    if (
        not isinstance(prior_models_value, dict)
        or set(prior_models_value) != set(runtime_policy.LOCAL_TIERS)
        or any(
            not isinstance(model, str) or not runtime_policy._MODEL_RE.fullmatch(model)
            for model in prior_models_value.values()
        )
    ):
        return False, "pending deployment journal has invalid prior models"
    deployment_id = str(journal.get("deployment_id") or "")
    if not deployment_id:
        return False, "pending deployment journal has invalid transition identity"
    try:
        journal_policy_path = Path(journal["policy_path"]).resolve()
        current_policy_path = runtime_policy.policy_path().resolve()
    except (OSError, TypeError, ValueError) as exc:
        return False, f"pending deployment policy path is invalid: {exc}"
    if os.path.normcase(str(journal_policy_path)) != os.path.normcase(str(current_policy_path)):
        return False, "pending deployment belongs to a different runtime policy"
    if operation == "deploy" and journal.get("shared_alias_transition") is True:
        try:
            shared_transition = _read_shared_alias_record("transition")
            shared_owner = _read_shared_alias_record("owner")
        except ValueError as exc:
            return False, f"shared personal-alias recovery record is invalid: {exc}"
        expected_policy = str(current_policy_path)
        if (
            shared_transition is None
            or shared_transition.get("deployment_id") != deployment_id
            or not hmac.compare_digest(
                str(shared_transition.get("policy_token") or ""),
                str(journal.get("policy_token") or ""),
            )
            or os.path.normcase(str(shared_transition.get("policy_path") or ""))
            != os.path.normcase(expected_policy)
            or Path(str(shared_transition.get("journal_path") or "")) != path
        ):
            return False, "shared personal-alias recovery ownership is missing"
        if shared_owner is not None and os.path.normcase(
            str(shared_owner.get("policy_path") or "")
        ) != os.path.normcase(expected_policy):
            return False, "shared personal-alias recovery conflicts with its owner"

    try:
        recorded_state = _json_object(journal["state_path"], "training state")
    except ValueError:
        recorded_state = {}

    def run(command, **kwargs):
        return _run_external(runner, command, ollama_command=True, **kwargs)

    ollama = _ollama_executable(ollama)

    def remove_or_handoff(models):
        failed = []
        for model in models:
            if not model:
                continue
            run([ollama, "rm", model], capture_output=True, text=True, timeout=30)
            probe = run(
                [ollama, "show", model], capture_output=True, text=True, timeout=30,
            )
            if _model_show_status(probe, model) == "missing":
                with contextlib.suppress(OSError, TypeError, ValueError):
                    _forget_pending_model_cleanup(model)
                continue
            try:
                if not _record_pending_model_cleanup(model):
                    return False, [model]
            except (OSError, TypeError, ValueError):
                return False, [model]
            failed.append(model)
        return True, failed

    prior_models = dict(journal["prior_models"])
    current = runtime_policy.load(create=True)

    if operation == "rollback":
        rollback_identity = str(journal.get("rollback_identity") or "")
        rollback_digest = str(journal.get("rollback_digest") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", rollback_identity)
            or not re.fullmatch(r"[0-9a-f]{64}", rollback_digest)
        ):
            return False, "pending rollback journal has invalid model identity"
        witness = recorded_state.get("last_policy_transition") if isinstance(recorded_state, dict) else None
        committed = (
            isinstance(witness, dict)
            and witness.get("id") == deployment_id
            and witness.get("operation") == "rollback"
            and witness.get("model_digest") == rollback_digest
            and recorded_state.get("policy_revision") == current.get("revision")
        )
        if committed:
            rollback_probe = run(
                [ollama, "show", ROLLBACK_MODEL],
                capture_output=True, text=True, timeout=30,
            )
            try:
                digest_matches = (
                    promotion_eval.local_model_digest(ROLLBACK_MODEL) == rollback_digest
                )
            except (OSError, TypeError, ValueError):
                digest_matches = False
            if (
                current.get("error")
                or current.get("revision") != journal["last_policy_revision"]
                or current.get("local_models", {}).get("code") != ROLLBACK_MODEL
                or current.get("local_models", {}).get("general") != ROLLBACK_MODEL
                or _model_show_status(rollback_probe, ROLLBACK_MODEL) != "exists"
                or _show_identity(rollback_probe) != rollback_identity
                or not digest_matches
            ):
                return False, "committed rollback policy could not be verified"
            if not _clear_deployment_journal(deployment_id):
                return False, "committed rollback journal cleanup failed"
            return True, "committed rollback finalized"

        policy_restored = current.get("local_models") == prior_models
        if not policy_restored and not current.get("error"):
            current_revision = int(current.get("revision") or 0)
            target_matches = (
                current.get("source") == "training rollback"
                and current.get("local_models", {}).get("code") == ROLLBACK_MODEL
                and current.get("local_models", {}).get("general") == ROLLBACK_MODEL
                and current_revision in {
                    int(journal["last_policy_revision"]),
                    int(journal["prior_policy_revision"]) + 1,
                }
            )
            if target_matches:
                try:
                    restored = runtime_policy.update(
                        local_models=prior_models,
                        source="interrupted rollback policy restore",
                        expected_revision=current_revision,
                        transition_token=journal["policy_token"],
                    )
                    journal["last_policy_revision"] = restored["revision"]
                    _write_deployment_journal(journal)
                    policy_restored = True
                except (OSError, RuntimeError, TypeError, ValueError):
                    policy_restored = False
        if not policy_restored:
            return False, "interrupted rollback policy recovery needs attention"
        if not _clear_deployment_journal(deployment_id):
            return False, "interrupted rollback recovered but journal cleanup failed"
        return True, "interrupted rollback recovered"

    if type(journal.get("personal_existed")) is not bool:
        return False, "pending deployment journal has invalid alias state"
    candidate = str(journal.get("candidate_model") or "")
    candidate_identity = str(journal.get("candidate_identity") or "")
    candidate_digest = str(journal.get("candidate_digest") or "")
    previous_alias = str(journal.get("previous_alias") or "")
    if (
        candidate != f"sonder-personal-candidate:{deployment_id}"
        or not re.fullmatch(r"[0-9a-f]{64}", candidate_identity)
        or not re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
        or (previous_alias and previous_alias != f"sonder-personal-previous:{deployment_id}")
    ):
        return False, "pending deployment journal contains unsafe model identities"

    # A crash after the state commit but before marker deletion is a completed
    # deployment. The previous alias is intentional rollback state; only the
    # temporary candidate is handed to verified cleanup.
    committed_id = (
        (recorded_state.get("last_deployment_eval") or {}).get("deployment_id")
        if isinstance(recorded_state, dict) else None
    )
    if committed_id == deployment_id:
        personal_probe = run(
            [ollama, "show", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        try:
            committed_digest_matches = (
                promotion_eval.local_model_digest(PERSONAL_MODEL) == candidate_digest
            )
        except (OSError, TypeError, ValueError):
            committed_digest_matches = False
        committed_policy_matches = (
            not current.get("error")
            and recorded_state.get("policy_revision") == current.get("revision")
            and current.get("local_models", {}).get("code") == PERSONAL_MODEL
            and current.get("local_models", {}).get("general") == PERSONAL_MODEL
        )
        committed_alias_matches = (
            _model_show_status(personal_probe, PERSONAL_MODEL) == "exists"
            and _show_identity(personal_probe) == candidate_identity
            and committed_digest_matches
        )
        if not committed_policy_matches or not committed_alias_matches:
            if not current.get("error"):
                safe_models = {
                    tier: ROLLBACK_MODEL if model == PERSONAL_MODEL else model
                    for tier, model in current["local_models"].items()
                }
                safe_models.update({"code": ROLLBACK_MODEL, "general": ROLLBACK_MODEL})
                if safe_models != current["local_models"]:
                    try:
                        safe_policy = runtime_policy.update(
                            local_models=safe_models,
                            source="committed deployment recovery fail-safe",
                            expected_revision=current["revision"],
                            transition_token=journal["policy_token"],
                        )
                        journal["last_policy_revision"] = safe_policy["revision"]
                        _write_deployment_journal(journal)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        pass
            return False, "committed deployment identity recovery needs attention"
        if journal.get("shared_alias_transition") is True:
            try:
                _write_shared_alias_owner(
                    deployment_id=deployment_id,
                    identity=candidate_identity,
                    digest=candidate_digest,
                )
            except (OSError, TypeError, ValueError):
                return False, "committed deployment alias ownership could not be recorded"
        old_recovery = str(journal.get("old_recovery_alias") or "")
        if old_recovery and (
            not old_recovery.startswith("sonder-personal-previous:")
            or not runtime_policy._MODEL_RE.fullmatch(old_recovery)
        ):
            return False, "completed deployment journal has unsafe cleanup identity"
        owned, failed = remove_or_handoff([candidate, old_recovery])
        if not owned:
            return False, "completed deployment candidate cleanup ownership failed"
        if not _clear_deployment_journal(deployment_id):
            return False, "completed deployment journal cleanup failed"
        if failed:
            return False, "completed deployment candidate cleanup remains pending"
        return True, "completed deployment journal finalized"

    personal_existed = journal.get("personal_existed") is True
    previous_identity = str(journal.get("previous_identity") or "")
    previous_digest = str(journal.get("previous_digest") or "")
    if (
        personal_existed
        and journal.get("shared_alias_transition") is True
        and not re.fullmatch(r"[0-9a-f]{64}", previous_digest)
    ):
        return False, "pending deployment journal has invalid previous alias digest"
    alias_restored = False
    current_personal = run(
        [ollama, "show", PERSONAL_MODEL], capture_output=True, text=True, timeout=30,
    )
    current_status = _model_show_status(current_personal, PERSONAL_MODEL)
    current_identity = _show_identity(current_personal) if current_status == "exists" else ""
    if current_status == "exists":
        try:
            current_digest = promotion_eval.local_model_digest(PERSONAL_MODEL)
        except (OSError, TypeError, ValueError):
            current_digest = ""
    else:
        current_digest = ""
    may_have_published = journal["phase"] in {
        "publishing", "published", "verified", "activated", "committed",
    }
    if personal_existed and previous_identity:
        alias_restored = (
            current_status == "exists"
            and current_identity == previous_identity
            and (not previous_digest or current_digest == previous_digest)
        )
        if (
            not alias_restored
            and may_have_published
            and current_status == "exists"
            and current_identity == candidate_identity
            and current_digest == candidate_digest
            and previous_alias
        ):
            backup_probe = run(
                [ollama, "show", previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            try:
                backup_digest_matches = (
                    not previous_digest
                    or promotion_eval.local_model_digest(previous_alias) == previous_digest
                )
            except (OSError, TypeError, ValueError):
                backup_digest_matches = False
            if (
                _model_show_status(backup_probe, previous_alias) == "exists"
                and _show_identity(backup_probe) == previous_identity
                and backup_digest_matches
            ):
                copied = run(
                    [ollama, "cp", previous_alias, PERSONAL_MODEL],
                    capture_output=True, text=True, timeout=30,
                )
                verified = run(
                    [ollama, "show", PERSONAL_MODEL],
                    capture_output=True, text=True, timeout=30,
                ) if copied.returncode == 0 else copied
                try:
                    restored_digest_matches = (
                        not previous_digest
                        or promotion_eval.local_model_digest(PERSONAL_MODEL)
                        == previous_digest
                    )
                except (OSError, TypeError, ValueError):
                    restored_digest_matches = False
                alias_restored = (
                    copied.returncode == 0
                    and _model_show_status(verified, PERSONAL_MODEL) == "exists"
                    and _show_identity(verified) == previous_identity
                    and restored_digest_matches
                )
    elif not personal_existed:
        alias_restored = current_status == "missing"
        if (
            not alias_restored
            and may_have_published
            and current_status == "exists"
            and current_identity == candidate_identity
            and current_digest == candidate_digest
        ):
            run([ollama, "rm", PERSONAL_MODEL], capture_output=True, text=True, timeout=30)
            verified = run(
                [ollama, "show", PERSONAL_MODEL],
                capture_output=True, text=True, timeout=30,
            )
            alias_restored = _model_show_status(verified, PERSONAL_MODEL) == "missing"

    allowed_revisions = {
        int(journal.get("prior_policy_revision") or 0),
        int(journal.get("last_policy_revision") or 0),
    }
    target_models = prior_models if alias_restored else {
        **{
            tier: ROLLBACK_MODEL if model == PERSONAL_MODEL else model
            for tier, model in prior_models.items()
        },
        "code": ROLLBACK_MODEL,
        "general": ROLLBACK_MODEL,
    }
    policy_restored = current.get("local_models") == target_models
    if not policy_restored and not current.get("error"):
        current_revision = int(current.get("revision") or 0)
        known_source = current.get("source") in {
            "safe personal model deployment transition",
            "behavior-validated personal QLoRA deployment",
        }
        allowed = current_revision in allowed_revisions or (
            known_source and current_revision - 1 in allowed_revisions
        )
        if allowed:
            try:
                runtime_policy.update(
                    local_models=target_models,
                    source="interrupted deployment recovery",
                    expected_revision=current_revision,
                    transition_token=journal["policy_token"],
                )
                policy_restored = True
            except (OSError, RuntimeError, ValueError):
                policy_restored = False

    if not alias_restored or not policy_restored:
        return False, (
            "interrupted deployment recovery needs attention; preserved recovery "
            f"alias: {previous_alias or '(none)'}"
        )
    owned, failed = remove_or_handoff([candidate, previous_alias])
    if not owned:
        return False, "interrupted deployment cleanup ownership failed"
    if not _clear_deployment_journal(deployment_id):
        return False, "interrupted deployment recovered but journal cleanup failed"
    if failed:
        return False, "interrupted deployment recovered; alias cleanup remains pending"
    return True, "interrupted deployment recovered"


def _pid_alive(pid):
    return _process_pid_alive(pid)


def _recorded_training_child_alive(lock_payload):
    try:
        state_file = Path(lock_payload["state_path"])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        claim = Path(state["run_dir"]) / ".launch-claimed"
        child_pid = int(claim.read_text(encoding="ascii").strip())
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return _pid_alive(child_pid)


@contextlib.contextmanager
def _exclusive_byte_lock(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RuntimeError(
                "another training lifecycle operation is already running"
            ) from exc
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextlib.contextmanager
def _deployment_lock():
    home_lock = Path(sonder_paths.state_path("training-lifecycle.lock")).resolve()
    policy = runtime_policy.policy_path().resolve()
    policy_lock = policy.with_name(policy.name + ".lifecycle.lock")
    training_state = state_path()
    state_lock = training_state.with_name(training_state.name + ".lifecycle.lock")
    alias_lock = _shared_alias_paths()["lock"]
    lock_paths = sorted(
        {home_lock, policy_lock, state_lock, alias_lock},
        key=lambda value: os.path.normcase(str(value)),
    )
    owner_path = home_lock.with_name(home_lock.name + ".owner.json")
    token = uuid.uuid4().hex
    payload = {
        "token": token, "pid": os.getpid(), "host": socket.gethostname(),
        "created_ts": time.time(),
        "state_path": str(state_path()),
    }
    with contextlib.ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(_exclusive_byte_lock(lock_path))
        try:
            old_owner = _json_object(owner_path, "training lifecycle owner")
        except ValueError:
            old_owner = {}
        current_owner = {"state_path": str(state_path())}
        if (
            (old_owner and _recorded_training_child_alive(old_owner))
            or _recorded_training_child_alive(current_owner)
        ):
            raise RuntimeError("an orphaned training child is still running")
        _write_json_atomic(owner_path, payload)
        try:
            yield
        finally:
            try:
                current = _json_object(owner_path, "training lifecycle owner")
            except ValueError:
                current = {}
            if current.get("token") == token:
                with contextlib.suppress(OSError):
                    owner_path.unlink()


def deploy(adapter_dir="", *, converter="", ollama="", runner=subprocess.run):
    try:
        with _deployment_lock():
            ollama = _ollama_executable(ollama)
            shared_ready, detail = _prepare_shared_alias_lifecycle(require_owner=True)
            if not shared_ready:
                return False, f"Deployment blocked: {detail}"
            cleaned, detail = _reconcile_pending_model_cleanup(
                ollama=ollama, runner=runner,
            )
            if not cleaned:
                return False, f"Deployment blocked: {detail}"
            recovered, detail = _reconcile_pending_deployment(
                ollama=ollama, runner=runner,
            )
            if not recovered:
                return False, f"Deployment blocked: {detail}"
            return _deploy_locked(
                adapter_dir, converter=converter, ollama=ollama, runner=runner
            )
    except RuntimeError as exc:
        return False, f"Deployment blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Deployment failed safely: {_bounded_error(exc)}"


def _deploy_locked(adapter_dir="", *, converter="", ollama="", runner=subprocess.run):
    def run(command, **kwargs):
        is_ollama = bool(command) and os.path.normcase(str(command[0])) == os.path.normcase(
            str(ollama)
        )
        return _run_external(
            runner, command, ollama_command=is_ollama, **kwargs,
        )

    state = _read_state()
    adapter_dir = Path(adapter_dir or state.get("adapter_dir") or ROOT / "sonder-personal-lora")
    ok, manifest = _validate_deployment_adapter(adapter_dir, state)
    if not ok:
        return False, f"Deployment blocked: {manifest}"
    converter_path = _converter_path(converter)
    if not converter_path:
        return False, (
            "Deployment blocked: llama.cpp/convert_lora_to_gguf.py was not found. "
            f"Set SONDER_LLAMA_CPP to a checkout containing reviewed commit "
            f"{LLAMA_CPP_REVISION}. Raw PEFT "
            "Safetensors are not used for Qwen deployment."
        )
    run_dir = Path(state["run_dir"]).resolve()
    stale_staging_root = run_dir / "deployment-staging"
    if stale_staging_root.exists():
        for stale in stale_staging_root.iterdir():
            if stale.is_dir() and stale.parent == stale_staging_root:
                shutil.rmtree(stale, ignore_errors=True)
        if any(stale_staging_root.iterdir()):
            return False, "Deployment blocked: stale deployment staging cleanup failed."
    required = MODEL_SPECS[manifest["model_size"]]["params"] * 1.5 + 2
    disk_ok, free = _disk_ok(run_dir, required)
    if not disk_ok:
        return False, f"Deployment blocked: {free:.1f} GB disk free; about {required:.1f} GB required."
    deployment_id = "%s-%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    staging = run_dir / "deployment-staging" / deployment_id
    staging.mkdir(parents=True, exist_ok=False)
    gguf = staging / "sonder-personal-lora.gguf"
    candidate = f"sonder-personal-candidate:{deployment_id}"
    candidate_created = False
    ollama = ollama or os.environ.get("SONDER_OLLAMA_EXE", "").strip() or shutil.which("ollama") or "ollama"
    report_path = run_dir / "deployment-evaluations" / f"{deployment_id}.json"
    previous_alias = ""
    previous_identity = ""
    previous_digest = ""
    candidate_identity = ""
    prior_policy = None
    prior_models = None
    policy_revision = None
    policy_mutated = False
    journal = None
    shared_transition = None
    policy_token = secrets.token_urlsafe(32)

    def write_receipt(payload):
        _write_json_atomic(report_path, payload)
        return _sha256_file(report_path)

    def advance_journal(phase):
        if journal is None:
            raise RuntimeError("deployment journal was not prepared")
        journal.update(
            phase=phase,
            last_policy_revision=policy_revision,
            updated_ts=int(time.time()),
        )
        _write_deployment_journal(journal)

    def restore_prior_policy():
        nonlocal policy_revision, policy_mutated
        if not policy_mutated:
            return True
        try:
            restored_policy = runtime_policy.update(
                local_models=prior_models,
                source="failed personal deployment policy restore",
                expected_revision=policy_revision,
                transition_token=policy_token,
            )
            policy_revision = restored_policy["revision"]
            policy_mutated = False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def restore_previous_alias():
        if previous_alias:
            backup_probe = run(
                [ollama, "show", previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            try:
                backup_digest_matches = (
                    promotion_eval.local_model_digest(previous_alias) == previous_digest
                )
            except (OSError, TypeError, ValueError):
                backup_digest_matches = False
            if (
                _model_show_status(backup_probe, previous_alias) != "exists"
                or _show_identity(backup_probe) != previous_identity
                or not backup_digest_matches
            ):
                return False
            restored = run(
                [ollama, "cp", previous_alias, PERSONAL_MODEL],
                capture_output=True, text=True, timeout=30,
            )
            if restored.returncode:
                return False
        else:
            run(
                [ollama, "rm", PERSONAL_MODEL],
                capture_output=True, text=True, timeout=30,
            )
        verified = run(
            [ollama, "show", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        expected = "exists" if previous_alias else "missing"
        if _model_show_status(verified, PERSONAL_MODEL) != expected:
            return False
        if not previous_alias:
            return True
        try:
            restored_digest_matches = (
                promotion_eval.local_model_digest(PERSONAL_MODEL) == previous_digest
            )
        except (OSError, TypeError, ValueError):
            restored_digest_matches = False
        return (
            _show_identity(verified) == previous_identity
            and restored_digest_matches
        )

    def discard_previous_alias():
        if not previous_alias:
            return True
        run(
            [ollama, "rm", previous_alias],
            capture_output=True, text=True, timeout=30,
        )
        verified = run(
            [ollama, "show", previous_alias],
            capture_output=True, text=True, timeout=30,
        )
        return _model_show_status(verified, previous_alias) == "missing"

    def finalize_failed_recovery(alias_restored, policy_restored):
        backup_cleaned = discard_previous_alias() if alias_restored else False
        if alias_restored and policy_restored and backup_cleaned and journal is not None:
            return _clear_deployment_journal(deployment_id)
        return alias_restored and policy_restored and backup_cleaned

    def recover_policy(alias_restored):
        nonlocal policy_revision, policy_mutated
        if alias_restored:
            return restore_prior_policy()
        if not policy_mutated:
            return True
        current = runtime_policy.load(create=False)
        if current.get("error") or current.get("revision") != policy_revision:
            return False
        safe_models = {
            tier: ROLLBACK_MODEL if model == PERSONAL_MODEL else model
            for tier, model in current["local_models"].items()
        }
        safe_models.update({"code": ROLLBACK_MODEL, "general": ROLLBACK_MODEL})
        if safe_models == current["local_models"]:
            policy_mutated = False
            return True
        try:
            safe_policy = runtime_policy.update(
                local_models=safe_models,
                source="failed personal alias fail-safe routing",
                expected_revision=policy_revision,
                transition_token=policy_token,
            )
            policy_revision = safe_policy["revision"]
            policy_mutated = False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    try:
        converter_ok, converter_provenance = _stage_reviewed_converter(
            converter_path, staging / "llama-cpp", runner=runner,
        )
        if not converter_ok:
            return False, f"Deployment blocked: {converter_provenance}."
        staged_converter = Path(converter_provenance["converter"])
        staged_converter_root = staged_converter.parent
        verified_adapter = staging / "verified-adapter"
        verified_adapter.mkdir()
        for name in ("adapter_config.json", "adapter_model.safetensors"):
            source = adapter_dir / name
            target = verified_adapter / name
            shutil.copyfile(source, target)
            expected_hash = state["artifact_sha256"][name]
            expected_size = state["artifact_sizes"][name]
            if target.stat().st_size != expected_size or _sha256_file(target) != expected_hash:
                return False, (
                    f"Trusted adapter changed while staging {name}; conversion was not started."
                )
        verified_base = staging / "verified-base"
        verified_base.mkdir()
        stage_script = (
            "import shutil,sys\n"
            "from huggingface_hub import hf_hub_download\n"
            "from pathlib import Path\n"
            "source=hf_hub_download(repo_id=sys.argv[1],filename='config.json',revision=sys.argv[2])\n"
            "target=Path(sys.argv[3])/'config.json'\n"
            "shutil.copyfile(source,target)\n"
        )
        staged_base = run(
            [
                sys.executable, "-I", "-c", stage_script, manifest["base_hf"],
                manifest["hf_revision"], str(verified_base),
            ],
            timeout=300,
        )
        base_config = verified_base / "config.json"
        if (
            staged_base.returncode
            or not base_config.is_file()
            or base_config.is_symlink()
            or base_config.stat().st_size <= 0
        ):
            return False, (
                "Pinned Hugging Face base config staging failed; conversion was not started."
            )
        base_config_sha256 = _sha256_file(base_config)
        isolated_launcher = (
            "import runpy,sys\n"
            "from pathlib import Path\n"
            "root=Path(sys.argv[1]).resolve()\n"
            "script=root/'convert_lora_to_gguf.py'\n"
            "sys.path[:0]=[str(root),str(root/'gguf-py')]\n"
            "sys.argv=[str(script),*sys.argv[2:]]\n"
            "runpy.run_path(str(script),run_name='__main__')\n"
        )
        command = [
            sys.executable, "-I", "-c", isolated_launcher,
            str(staged_converter_root), str(verified_adapter),
            "--outfile", str(gguf), "--outtype", "f16",
            "--base", str(verified_base),
        ]
        converter_env = os.environ.copy()
        converter_env.pop("PYTHONPATH", None)
        converter_env.pop("PYTHONHOME", None)
        converter_env.update({
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        converted = run(
            command,
            cwd=staged_converter_root,
            env=converter_env,
            timeout=1800,
        )
        if converted.returncode or not gguf.exists() or gguf.stat().st_size < 1024:
            return False, "GGUF adapter conversion failed; the runtime policy was not changed."

        base_probe = run(
            [ollama, "show", manifest["base_ollama"]],
            capture_output=True, text=True, timeout=30,
        )
        base_status = _model_show_status(base_probe, manifest["base_ollama"])
        if base_status != "exists":
            detail = "is not installed" if base_status == "missing" else "could not be verified"
            return False, (
                f"Deployment blocked: exact Ollama base {manifest['base_ollama']} {detail}. "
                "No substitute base was selected."
            )
        try:
            base_digest = promotion_eval.local_model_digest(manifest["base_ollama"])
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Deployment blocked: exact base digest could not be verified: {exc}"

        modelfile = staging / "Modelfile.personal"
        modelfile.write_text(
            f"FROM {manifest['base_ollama']}\nADAPTER {gguf.resolve()}\n"
            "PARAMETER temperature 0\nPARAMETER seed 424242\nPARAMETER num_ctx 2048\n",
            encoding="utf-8",
        )
        try:
            if not _record_pending_model_cleanup(candidate):
                return False, "Candidate cleanup ownership could not be recorded."
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Candidate cleanup ownership could not be recorded: {exc}"
        candidate_created = True
        created = run(
            [ollama, "create", candidate, "-f", str(modelfile)],
            capture_output=True, text=True, timeout=600,
        )
        if created.returncode:
            return False, "Ollama candidate creation failed; existing models and policy were preserved."
        candidate_probe = run(
            [ollama, "show", candidate], capture_output=True, text=True, timeout=30,
        )
        if _model_show_status(candidate_probe, candidate) != "exists":
            return False, "Ollama candidate could not be verified after creation."
        candidate_identity = _show_identity(candidate_probe)
        if not candidate_identity:
            return False, "Ollama candidate identity could not be read after creation."
        try:
            candidate_digest = promotion_eval.local_model_digest(candidate)
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Ollama candidate digest could not be verified: {exc}"

        receipt = {
            "schema": 1,
            "deployment_id": deployment_id,
            "run_id": state["run_id"],
            "created_ts": int(time.time()),
            "candidate_model": candidate,
            "base_digest": base_digest,
            "candidate_digest": candidate_digest,
            "adapter_sha256": state["artifact_sha256"]["adapter_model.safetensors"],
            "base_config_sha256": base_config_sha256,
            "llama_cpp_revision": converter_provenance["revision"],
            "llama_cpp_tree_sha256": converter_provenance["tree_sha256"],
            "llama_cpp_staged_manifest_sha256": converter_provenance[
                "staged_manifest_sha256"
            ],
            "gguf_sha256": _sha256_file(gguf),
            "base_show_sha256": hashlib.sha256(
                (base_probe.stdout or "").encode("utf-8", "replace")
            ).hexdigest(),
            "candidate_show_sha256": hashlib.sha256(
                (candidate_probe.stdout or "").encode("utf-8", "replace")
            ).hexdigest(),
            "status": "evaluating",
        }
        try:
            behavior = promotion_eval.evaluate_pair(
                manifest["base_ollama"], candidate, challenge=deployment_id,
            )
            passed, reason = promotion_eval.promotion_decision(
                behavior,
                expected_base=manifest["base_ollama"],
                expected_candidate=candidate,
                expected_challenge=deployment_id,
            )
            receipt.update(
                status="passed_candidate_gate" if passed else "rejected_candidate",
                decision=reason,
                behavior=behavior,
            )
            receipt_sha256 = write_receipt(receipt)
        except Exception as exc:
            receipt.update(
                status="evaluation_error",
                decision=f"{type(exc).__name__}: {_bounded_error(exc)}",
            )
            with contextlib.suppress(OSError, TypeError, ValueError):
                write_receipt(receipt)
            return False, "Candidate behavior evaluation failed closed; runtime policy was not changed."
        if not passed:
            return False, f"Candidate behavior evaluation rejected promotion: {reason}"
        try:
            if promotion_eval.local_model_digest(manifest["base_ollama"]) != base_digest:
                return False, "Exact base alias changed during behavior evaluation."
            if promotion_eval.local_model_digest(candidate) != candidate_digest:
                return False, "Candidate alias changed during behavior evaluation."
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Model digest revalidation failed closed: {exc}"

        previous_probe = run(
            [ollama, "show", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        previous_status = _model_show_status(previous_probe, PERSONAL_MODEL)
        if previous_status == "error":
            return False, (
                "Deployment blocked: could not determine existing personal model state; "
                "the alias and runtime policy were preserved."
            )
        if previous_status == "exists":
            previous_identity = _show_identity(previous_probe)
            if not previous_identity:
                return False, "Deployment blocked: existing personal model identity is unreadable."
            try:
                previous_digest = promotion_eval.local_model_digest(PERSONAL_MODEL)
            except (OSError, TypeError, ValueError) as exc:
                return False, f"Deployment blocked: personal alias digest is unreadable: {exc}"
            previous_alias = f"sonder-personal-previous:{deployment_id}"

        try:
            shared_transition = _claim_shared_alias_transition(
                deployment_id, policy_token,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"Deployment blocked: could not claim personal alias: {exc}"

        journal = {
            "schema": 1,
            "operation": "deploy",
            "deployment_id": deployment_id,
            "state_path": str(state_path()),
            "candidate_model": candidate,
            "candidate_identity": candidate_identity,
            "candidate_digest": candidate_digest,
            "previous_alias": previous_alias,
            "previous_identity": previous_identity,
            "previous_digest": previous_digest,
            "personal_existed": previous_status == "exists",
            "shared_alias_transition": True,
            "phase": "reserved",
            "created_ts": int(time.time()),
            "policy_token": policy_token,
        }
        try:
            prior_policy, journal = runtime_policy.reserve_transition(journal)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with contextlib.suppress(OSError, TypeError, ValueError):
                _clear_shared_alias_transition(deployment_id, policy_token)
            return False, f"Deployment blocked: could not reserve runtime policy transition: {exc}"
        prior_models = dict(prior_policy["local_models"])
        policy_revision = prior_policy["revision"]
        try:
            shared_transition = _advance_shared_alias_transition(
                shared_transition, "reserved",
            )
        except (OSError, TypeError, ValueError) as exc:
            _clear_deployment_journal(deployment_id)
            return False, f"Deployment blocked: shared alias reservation failed: {exc}"

        # The alias probe happened before the atomic policy reservation. An
        # external Ollama mutation in that gap must fail closed before backup or
        # publication; ordinary policy writers are already blocked here.
        reserved_probe = run(
            [ollama, "show", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        reserved_status = _model_show_status(reserved_probe, PERSONAL_MODEL)
        alias_unchanged = reserved_status == previous_status
        if previous_status == "exists":
            try:
                reserved_digest = promotion_eval.local_model_digest(PERSONAL_MODEL)
            except (OSError, TypeError, ValueError):
                reserved_digest = ""
            alias_unchanged = (
                alias_unchanged
                and _show_identity(reserved_probe) == previous_identity
                and reserved_digest == previous_digest
            )
        if not alias_unchanged:
            _clear_deployment_journal(deployment_id)
            return False, "Deployment blocked: personal alias changed before transition reservation."
        owner_ok, owner_detail = _validate_shared_alias_owner(
            prior_policy=prior_policy,
            personal_status=previous_status,
            personal_identity=previous_identity,
            personal_digest=previous_digest,
            state=state,
        )
        if not owner_ok:
            _clear_deployment_journal(deployment_id)
            return False, f"Deployment blocked: {owner_detail}."

        if previous_status == "exists":
            preserved = run(
                [ollama, "cp", PERSONAL_MODEL, previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            verified_backup = run(
                [ollama, "show", previous_alias],
                capture_output=True, text=True, timeout=30,
            ) if preserved.returncode == 0 else preserved
            try:
                backup_digest = (
                    promotion_eval.local_model_digest(previous_alias)
                    if preserved.returncode == 0 else ""
                )
            except (OSError, TypeError, ValueError):
                backup_digest = ""
            if (
                preserved.returncode
                or _model_show_status(verified_backup, previous_alias) != "exists"
                or _show_identity(verified_backup) != previous_identity
                or backup_digest != previous_digest
            ):
                run([ollama, "rm", previous_alias], capture_output=True, text=True, timeout=30)
                removed_backup = run(
                    [ollama, "show", previous_alias],
                    capture_output=True, text=True, timeout=30,
                )
                cleanup_owned = _model_show_status(removed_backup, previous_alias) == "missing"
                if not cleanup_owned:
                    try:
                        cleanup_owned = _record_pending_model_cleanup(previous_alias)
                    except (OSError, TypeError, ValueError):
                        cleanup_owned = False
                if cleanup_owned:
                    _clear_deployment_journal(deployment_id)
                return False, (
                    "Deployment blocked: the existing personal model could not be "
                    "preserved and verified before promotion."
                    + ("" if cleanup_owned else " Recovery cleanup needs attention.")
                )
            try:
                advance_journal("backed_up")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # The reserved journal already names the backup. Preserve it so
                # the next lifecycle command can recover safely.
                return False, f"Deployment backup journal update failed: {exc}"
        active_personal = any(model == PERSONAL_MODEL for model in prior_models.values())
        if active_personal:
            safe_models = {
                tier: ROLLBACK_MODEL if model == PERSONAL_MODEL else model
                for tier, model in prior_models.items()
            }
            try:
                quiesced = runtime_policy.update(
                    local_models=safe_models,
                    source="safe personal model deployment transition",
                    expected_revision=policy_revision,
                    transition_token=policy_token,
                )
                policy_revision = quiesced["revision"]
                policy_mutated = True
                advance_journal("quiesced")
            except (OSError, RuntimeError, ValueError) as exc:
                restored = restore_previous_alias()
                policy_restored = recover_policy(restored)
                finalize_failed_recovery(restored, policy_restored)
                return False, f"Deployment blocked: could not quiesce active personal alias: {exc}"

        try:
            # Journal the intent before the personal alias can change; recovery
            # therefore handles a hard stop between cp and its return.
            advance_journal("publishing")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Personal alias publication was not started: {exc}"

        promoted = run(
            [ollama, "cp", candidate, PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        if promoted.returncode:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                "Personal alias promotion failed; the previous alias and policy were restored."
                if restored and policy_restored else
                f"Personal alias promotion failed and recovery needs attention; preserved alias: "
                f"{previous_alias or '(none)'}."
            )

        try:
            advance_journal("published")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Published alias recovery journal update failed: {exc}"

        published_probe = run(
            [ollama, "show", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        if (
            _model_show_status(published_probe, PERSONAL_MODEL) != "exists"
            or _show_identity(published_probe) != candidate_identity
        ):
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                "Published personal alias identity did not match the candidate; "
                "the previous alias and policy were restored."
                if restored and policy_restored else
                "Published personal alias identity mismatch and recovery needs attention; "
                f"preserved recovery alias: {previous_alias or '(none)'} ."
            )
        try:
            if promotion_eval.local_model_digest(PERSONAL_MODEL) != candidate_digest:
                raise ValueError("published alias digest differs from candidate")
        except (OSError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Published personal alias digest validation failed: {exc}"

        candidate_tasks = behavior.get("candidate", {}).get("tasks", [])
        candidate_results = {
            task.get("id"): task.get("passed") for task in candidate_tasks
        }
        try:
            final_behavior = promotion_eval.evaluate_model(
                PERSONAL_MODEL, challenge=deployment_id,
            )
            final_results = {
                task.get("id"): task.get("passed")
                for task in final_behavior.get("tasks", [])
            }
            final_ok = (
                final_behavior.get("total") == behavior["candidate"].get("total")
                and final_behavior.get("score", 0) >= behavior["candidate"].get("score", 0)
                and set(final_results) == set(candidate_results)
                and all(
                    not passed or final_results.get(task_id) is True
                    for task_id, passed in candidate_results.items()
                )
            )
            report_valid, _report_reason = promotion_eval.validate_model_report(
                final_behavior,
                expected_model=PERSONAL_MODEL,
                challenge=deployment_id,
            )
            final_ok = final_ok and report_valid
        except Exception as exc:
            final_behavior = {
                "model": PERSONAL_MODEL,
                "score": 0,
                "total": len(candidate_results),
                "error": f"{type(exc).__name__}: {_bounded_error(exc)}",
            }
            final_ok = False
        post_eval_digest_verified = False
        if final_ok:
            try:
                post_eval_digest_verified = (
                    promotion_eval.local_model_digest(PERSONAL_MODEL) == candidate_digest
                )
            except (OSError, TypeError, ValueError):
                post_eval_digest_verified = False
            final_ok = post_eval_digest_verified
        receipt.update(
            status="passed_final_suite" if final_ok else "failed_final_suite",
            final_evaluation=final_behavior,
            post_eval_digest_verified=post_eval_digest_verified,
            evaluated_ts=int(time.time()),
        )
        try:
            receipt_sha256 = write_receipt(receipt)
        except (OSError, TypeError, ValueError):
            final_ok = False
        if not final_ok:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                "Final behavior evaluation failed; the previous personal alias and policy were restored."
                if restored and policy_restored else
                "Final behavior evaluation failed and automatic recovery needs attention; "
                f"preserved recovery alias: {previous_alias or '(none)'}."
            )

        try:
            advance_journal("verified")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Verified alias recovery journal update failed: {exc}"

        try:
            if promotion_eval.local_model_digest(PERSONAL_MODEL) != candidate_digest:
                raise ValueError("published alias digest changed before policy activation")
            desired_models = dict(prior_models)
            desired_models.update({"code": PERSONAL_MODEL, "general": PERSONAL_MODEL})
            policy = runtime_policy.update(
                local_models=desired_models,
                source="behavior-validated personal QLoRA deployment",
                expected_revision=policy_revision,
                transition_token=policy_token,
            )
            policy_revision = policy["revision"]
            policy_mutated = True
            advance_journal("activated")
        except (OSError, RuntimeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                f"Personal model validated, but runtime policy activation failed: {exc}. "
                + (
                    "The previous personal alias and policy were restored."
                    if restored and policy_restored else
                    f"Recovery needs attention; preserved alias: {previous_alias or '(none)'} ."
                )
            )

        old_recovery = str(state.get("previous_personal_model") or "")
        if old_recovery and not (
            old_recovery.startswith("sonder-personal-previous:")
            and runtime_policy._MODEL_RE.fullmatch(old_recovery)
        ):
            old_recovery = ""
        journal["old_recovery_alias"] = old_recovery
        try:
            _write_deployment_journal(journal)
        except (OSError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Deployment recovery snapshot update failed: {exc}"
        state.update(
            status="deployed", deployed_ts=int(time.time()), model=PERSONAL_MODEL,
            previous_personal_model=previous_alias,
            policy_revision=policy["revision"],
            last_deployment_eval={
                "path": str(report_path), "sha256": receipt_sha256,
                "deployment_id": deployment_id,
                "suite_version": behavior.get("suite_version"),
                "base_score": behavior.get("base", {}).get("score"),
                "candidate_score": behavior.get("candidate", {}).get("score"),
                "total": behavior.get("candidate", {}).get("total"),
            },
        )
        try:
            _write_state(state)
        except (OSError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                f"Deployment state commit failed: {_bounded_error(exc)}. "
                + (
                    "The previous personal alias and policy were restored."
                    if restored and policy_restored else
                    f"Recovery needs attention; preserved alias: {previous_alias or '(none)'} ."
                )
            )
        policy_mutated = False
        try:
            _write_shared_alias_owner(
                deployment_id=deployment_id,
                identity=candidate_identity,
                digest=candidate_digest,
            )
        except (OSError, TypeError, ValueError):
            return False, (
                "Deployment committed, but personal-alias ownership could not be "
                "recorded; the transition marker was preserved."
            )
        old_cleanup_pending = False
        if (
            old_recovery.startswith("sonder-personal-previous:")
            and old_recovery != previous_alias
        ):
            run([ollama, "rm", old_recovery], capture_output=True, text=True, timeout=30)
            old_probe = run(
                [ollama, "show", old_recovery],
                capture_output=True, text=True, timeout=30,
            )
            if _model_show_status(old_probe, old_recovery) != "missing":
                try:
                    old_cleanup_pending = _record_pending_model_cleanup(old_recovery)
                except (OSError, TypeError, ValueError):
                    return False, (
                        "Deployment committed, but old recovery-alias cleanup "
                        "ownership failed; the transition marker was preserved."
                    )
        if not _clear_deployment_journal(deployment_id):
            return False, (
                "Deployment committed, but transition cleanup remains pending; "
                "the next lifecycle command will finalize it."
            )
        if old_cleanup_pending:
            return False, (
                "Deployment committed; old recovery-alias cleanup remains pending."
            )
        return True, (
            f"Behavior-validated and deployed {PERSONAL_MODEL}; "
            f"{ROLLBACK_MODEL} remains available for rollback."
        )
    finally:
        if candidate_created:
            run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
            candidate_probe = run(
                [ollama, "show", candidate],
                capture_output=True, text=True, timeout=30,
            )
            if _model_show_status(candidate_probe, candidate) == "missing":
                with contextlib.suppress(OSError, TypeError, ValueError):
                    _forget_pending_model_cleanup(candidate)
            else:
                with contextlib.suppress(OSError, TypeError, ValueError):
                    _record_pending_model_cleanup(candidate)
        shutil.rmtree(staging, ignore_errors=True)


def rollback(*, ollama="", runner=subprocess.run):
    try:
        with _deployment_lock():
            ollama = _ollama_executable(ollama)
            shared_ready, detail = _prepare_shared_alias_lifecycle()
            if not shared_ready:
                return False, f"Rollback blocked: {detail}"
            cleaned, detail = _reconcile_pending_model_cleanup(
                ollama=ollama, runner=runner,
            )
            if not cleaned:
                return False, f"Rollback blocked: {detail}"
            recovered, detail = _reconcile_pending_deployment(
                ollama=ollama, runner=runner,
            )
            if not recovered:
                return False, f"Rollback blocked: {detail}"
            return _rollback_locked(ollama=ollama, runner=runner)
    except RuntimeError as exc:
        return False, f"Rollback blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Rollback failed safely: {_bounded_error(exc)}"


def adopt_legacy_personal(*, confirmed=False, ollama="", runner=subprocess.run):
    """Explicitly bind one pre-ownership personal alias to the current policy."""
    if not confirmed:
        return False, (
            "Legacy personal-alias adoption was not confirmed. Re-run with "
            "`training adopt-legacy --confirm` after verifying the active policy."
        )
    try:
        with _deployment_lock():
            ready, detail = _prepare_shared_alias_lifecycle()
            if not ready:
                return False, f"Legacy adoption blocked: {detail}"
            owner = _read_shared_alias_record("owner")
            current_policy = str(runtime_policy.policy_path().resolve())
            if owner is not None:
                if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
                    current_policy
                ):
                    return False, "Legacy adoption blocked: alias is owned by another policy."
                return True, "The personal alias is already owned by the current policy."
            ollama = _ollama_executable(ollama)
            probe = _run_external(
                runner,
                [ollama, "show", PERSONAL_MODEL],
                ollama_command=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = _model_show_status(probe, PERSONAL_MODEL)
            identity = _show_identity(probe)
            if status != "exists" or not identity:
                detail = "is missing" if status == "missing" else "could not be verified"
                return False, f"Legacy adoption blocked: {PERSONAL_MODEL} {detail}."
            try:
                digest = promotion_eval.local_model_digest(PERSONAL_MODEL)
            except (OSError, TypeError, ValueError) as exc:
                return False, f"Legacy adoption blocked: exact alias digest failed: {exc}"
            _write_shared_alias_owner(
                deployment_id="legacy-adoption-" + uuid.uuid4().hex,
                identity=identity,
                digest=digest,
            )
            return True, (
                f"Adopted exact legacy {PERSONAL_MODEL} identity for policy "
                f"{current_policy}. Future deployments will preserve it before promotion."
            )
    except RuntimeError as exc:
        return False, f"Legacy adoption blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Legacy adoption failed safely: {_bounded_error(exc)}"


def release_personal_owner(*, confirmed=False):
    """Release policy ownership only after routing no longer uses the alias."""
    if not confirmed:
        return False, (
            "Personal-alias ownership release was not confirmed. Roll back all "
            "personal routing, then re-run `training release-alias --confirm`."
        )
    try:
        with _deployment_lock():
            ready, detail = _prepare_shared_alias_lifecycle()
            if not ready:
                return False, f"Alias ownership release blocked: {detail}"
            owner = _read_shared_alias_record("owner")
            if owner is None:
                return True, "The endpoint has no persistent personal-alias owner."
            current_policy_path = str(runtime_policy.policy_path().resolve())
            if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
                current_policy_path
            ):
                return False, "Alias ownership release blocked: another policy owns it."
            policy = runtime_policy.load(create=True)
            if policy.get("error"):
                return False, "Alias ownership release blocked: runtime policy is unreadable."
            if any(model == PERSONAL_MODEL for model in policy["local_models"].values()):
                return False, (
                    "Alias ownership release blocked: roll every tier off "
                    f"{PERSONAL_MODEL} first."
                )
            _shared_alias_paths()["owner"].unlink()
            return True, (
                "Released personal-alias ownership without deleting or rerouting the model. "
                "Another policy may now adopt it explicitly."
            )
    except RuntimeError as exc:
        return False, f"Alias ownership release blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Alias ownership release failed safely: {_bounded_error(exc)}"


def _rollback_locked(*, ollama="", runner=subprocess.run):
    def run(command, **kwargs):
        return _run_external(runner, command, ollama_command=True, **kwargs)

    ollama = (
        ollama or os.environ.get("SONDER_OLLAMA_EXE", "").strip()
        or shutil.which("ollama") or "ollama"
    )
    probe = run(
        [ollama, "show", ROLLBACK_MODEL],
        capture_output=True, text=True, timeout=30,
    )
    rollback_identity = _show_identity(probe)
    if _model_show_status(probe, ROLLBACK_MODEL) != "exists" or not rollback_identity:
        return False, (
            f"Rollback blocked: {ROLLBACK_MODEL} could not be verified; "
            "runtime policy was not changed."
        )
    try:
        rollback_digest = promotion_eval.local_model_digest(ROLLBACK_MODEL)
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Rollback blocked: exact model digest could not be verified: {exc}"
    transition_id = "rollback-%s-%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    policy_token = secrets.token_urlsafe(32)
    journal = {
        "schema": 1,
        "operation": "rollback",
        "deployment_id": transition_id,
        "state_path": str(state_path()),
        "phase": "reserved",
        "created_ts": int(time.time()),
        "policy_token": policy_token,
        "rollback_identity": rollback_identity,
        "rollback_digest": rollback_digest,
    }
    policy = None
    reserved = False
    try:
        before, journal = runtime_policy.reserve_transition(journal)
        reserved = True
        reserved_probe = run(
            [ollama, "show", ROLLBACK_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        if (
            _model_show_status(reserved_probe, ROLLBACK_MODEL) != "exists"
            or _show_identity(reserved_probe) != rollback_identity
            or promotion_eval.local_model_digest(ROLLBACK_MODEL) != rollback_digest
        ):
            raise RuntimeError("rollback model changed after transition reservation")
        policy = runtime_policy.update(
            local_models={"code": ROLLBACK_MODEL, "general": ROLLBACK_MODEL},
            source="training rollback",
            expected_revision=before["revision"],
            transition_token=policy_token,
        )
        journal.update(
            phase="policy_updated",
            last_policy_revision=policy["revision"],
            updated_ts=int(time.time()),
        )
        _write_deployment_journal(journal)
        committed_probe = run(
            [ollama, "show", ROLLBACK_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        if (
            _model_show_status(committed_probe, ROLLBACK_MODEL) != "exists"
            or _show_identity(committed_probe) != rollback_identity
            or promotion_eval.local_model_digest(ROLLBACK_MODEL) != rollback_digest
        ):
            raise RuntimeError("rollback model changed before state commit")
        state = _read_state()
        state.update(
            status="rolled_back", rollback_ts=int(time.time()),
            policy_revision=policy["revision"],
            last_policy_transition={
                "id": transition_id,
                "operation": "rollback",
                "model_digest": rollback_digest,
            },
        )
        _write_state(state)
        journal.update(phase="committed", updated_ts=int(time.time()))
        _write_deployment_journal(journal)
        if not _clear_deployment_journal(transition_id):
            return False, (
                "Rollback committed, but transition cleanup remains pending; "
                "the next lifecycle command will finalize it."
            )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        if not reserved:
            return False, f"Rollback blocked: {_bounded_error(exc)}."
        restored = False
        restored, _detail = _reconcile_pending_deployment(
            ollama=ollama, runner=runner,
        )
        suffix = "policy was restored" if restored else "policy recovery needs attention"
        return False, f"Rollback failed: {_bounded_error(exc)}; {suffix}."
    return True, (
        f"Rolled code/general back to {ROLLBACK_MODEL}. Personal models and "
        "checkpoints were not deleted."
    )


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hardware")
    for name in ("plan", "start"):
        item = sub.add_parser(name)
        item.add_argument("--dry-run", action="store_true")
        item.add_argument("--model", default=os.environ.get("SONDER_TRAIN_MODEL", "auto"))
        item.add_argument(
            "--allow-cpu-offload",
            action="store_true",
            default=os.environ.get("SONDER_ALLOW_CPU_OFFLOAD") == "1",
            help="request training CPU offload (currently rejected: this Trainer backend only supports GPU-resident QLoRA)",
        )
        item.add_argument("--max-vram", type=float, default=_env_optional("SONDER_MAX_VRAM_GB"))
        item.add_argument("--max-system-ram", type=float, default=_env_optional("SONDER_MAX_SYSTEM_RAM_GB"))
        item.add_argument("--context-length", type=lambda value: parse_length(value, 8192), default=parse_length(os.environ.get("SONDER_CONTEXT_SIZE"), 8192))
        item.add_argument("--sequence-length", type=lambda value: parse_length(value, 1024), default=parse_length(os.environ.get("SONDER_MAX_LEN"), 1024))
        item.add_argument("--batch-size", type=int, default=int(os.environ.get("SONDER_BATCH_SIZE", "1")))
        item.add_argument("--gradient-accumulation", type=int, default=int(os.environ.get("SONDER_GRAD_ACCUM", "8")))
        item.add_argument("--gpu-index", type=int, default=int(os.environ.get("SONDER_TRAIN_GPU_INDEX", "0")))
        item.add_argument(
            "--full-finetune",
            action="store_true",
            default=os.environ.get("SONDER_FULL_FINETUNE") == "1",
        )
        if name == "start":
            item.add_argument("--confirm", action="store_true")
            item.add_argument("--resume", action="store_true")
    sub.add_parser("status")
    deploy_parser = sub.add_parser("deploy")
    deploy_parser.add_argument("--adapter-dir", default="")
    deploy_parser.add_argument("--llama-cpp", default="")
    adopt_parser = sub.add_parser("adopt-legacy")
    adopt_parser.add_argument("--confirm", action="store_true")
    release_parser = sub.add_parser("release-alias")
    release_parser.add_argument("--confirm", action="store_true")
    sub.add_parser("rollback")
    return parser


def _env_optional(name):
    try:
        return float(os.environ[name]) if os.environ.get(name, "").strip() else None
    except ValueError:
        return None


def parse_length(value, default):
    text = str(value or "").strip().lower().replace("_", "")
    if not text:
        return default
    multiplier = 1
    if text.endswith("k"):
        text, multiplier = text[:-1], 1024
    elif text.endswith("m"):
        text, multiplier = text[:-1], 1024 * 1024
    try:
        return max(1, int(float(text) * multiplier))
    except ValueError:
        return default


def _options(args):
    return PlanOptions(
        model=args.model,
        allow_cpu_offload=args.allow_cpu_offload,
        max_vram_gb=args.max_vram,
        max_system_ram_gb=args.max_system_ram,
        context_length=max(512, args.context_length),
        sequence_length=max(128, args.sequence_length),
        batch_size=max(1, args.batch_size),
        gradient_accumulation=max(1, args.gradient_accumulation),
        full_finetune=args.full_finetune,
        gpu_index=max(0, args.gpu_index),
    )


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "hardware":
        print(format_hardware())
        return 0
    if args.command in {"plan", "start"}:
        plan = build_plan(options=_options(args))
        if args.command == "plan":
            print(format_plan(plan))
            return 0
        ok, message = start_training(
            plan, confirmed=args.confirm, dry_run=args.dry_run, resume=args.resume
        )
        print(message)
        return 0 if ok else 2
    if args.command == "status":
        print(training_status())
        return 0
    if args.command == "deploy":
        ok, message = deploy(args.adapter_dir, converter=args.llama_cpp)
    elif args.command == "adopt-legacy":
        ok, message = adopt_legacy_personal(confirmed=args.confirm)
    elif args.command == "release-alias":
        ok, message = release_personal_owner(confirmed=args.confirm)
    else:
        ok, message = rollback()
    print(message)
    return 0 if ok else 2


def command_text(arg=""):
    """Run a lifecycle command for slash-command surfaces and return its text."""
    argv = shlex.split(str(arg or ""), posix=os.name != "nt")
    if not argv:
        argv = ["plan"]
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            main(argv)
    except SystemExit as exc:
        if not output.getvalue():
            return f"training command failed (exit {exc.code})"
    return output.getvalue().rstrip()


if __name__ == "__main__":
    raise SystemExit(main())
