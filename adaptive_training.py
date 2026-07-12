"""Hardware-aware inference/training planning and attended training lifecycle.

This module is stdlib-only. Heavy ML dependencies remain isolated in
``qlora_train.py`` so hardware/status/dry-run commands work on normal installs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import secrets
import shutil
import shlex
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import runtime_policy
import system_profile
import sonder_paths


ROOT = Path(__file__).resolve().parent
PERSONAL_MODEL = "sonder-personal:latest"
ROLLBACK_MODEL = "sonder:latest"
MODEL_SPECS = {
    "1.5b": {
        "params": 1.5,
        "hf": "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        "ollama": "qwen2.5-coder:1.5b",
        "train_vram": 2.8,
        "train_ram": 6.0,
        "infer_vram": 1.6,
        "infer_ram": 3.0,
    },
    "3b": {
        "params": 3.0,
        "hf": "Qwen/Qwen2.5-Coder-3B-Instruct",
        "ollama": "qwen2.5-coder:3b",
        "train_vram": 5.0,
        "train_ram": 10.0,
        "infer_vram": 2.8,
        "infer_ram": 5.0,
    },
    "7b": {
        "params": 7.0,
        "hf": "Qwen/Qwen2.5-Coder-7B-Instruct",
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
    profile = profile or system_profile.detect_hardware()
    options = options or PlanOptions()
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
    return Path(sonder_paths.state_path("training_state.json", "SONDER_TRAINING_STATE"))


def _read_state():
    path = state_path()
    if not path.exists():
        return {"status": "never_started", "rollback_model": ROLLBACK_MODEL}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "invalid", "error": "training state is unreadable"}


def _write_state(payload):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
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
        if not run_id or not run_dir.is_dir():
            return False, "Training resume provenance is missing; start a new run."
        if current.get("base_hf") != plan.training.model:
            return False, "Training resume plan does not match the interrupted run base model."
        prior_plan_file = run_dir / "training-plan.json"
        try:
            prior = json.loads(prior_plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, "Training resume plan is missing or unreadable."
        if _resume_signature(prior.get("plan")) != _resume_signature(plan.to_dict()):
            return False, "Training resume settings do not match the interrupted run."
    else:
        run_id = uuid.uuid4().hex
        run_dir = output_root / "runs" / run_id
    output = run_dir / "adapter"
    ok, free = _disk_ok(run_dir.parent, 3 + MODEL_SPECS[plan.training.model_size]["params"] * 2.2)
    if not ok:
        return False, f"Training not started: only {free:.1f} GB disk free."
    output.mkdir(parents=True, exist_ok=True)
    data_path = Path(os.environ.get("SONDER_DATA", ROOT / "training_data.jsonl"))
    if not data_path.exists():
        try:
            import export_training_data
            exported = export_training_data.main(str(data_path))
        except Exception as exc:
            return False, f"Training data preparation failed: {exc}"
        if not exported:
            return False, "Training data preparation produced no good-outcome examples."
    data_path = data_path.resolve()
    data_sha256 = _sha256_file(data_path)
    if resume:
        if str(prior.get("data_path") or "") != str(data_path):
            return False, "Training resume dataset path does not match the interrupted run."
        if str(prior.get("data_sha256") or "") != data_sha256:
            return False, "Training resume dataset changed since the interrupted run."
        with contextlib.suppress(OSError):
            (run_dir / ".launch-claimed").unlink()
    launch_token = secrets.token_urlsafe(32)
    manifest = {
        "schema": 2,
        "run_id": run_id,
        "base_hf": plan.training.model,
        "base_ollama": MODEL_SPECS[plan.training.model_size]["ollama"],
        "model_size": plan.training.model_size,
        "method": plan.training.method,
        "created_ts": int(time.time()),
        "data_path": str(data_path),
        "data_sha256": data_sha256,
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
        "base_hf": manifest["base_hf"],
        "base_ollama": manifest["base_ollama"],
        "rollback_model": ROLLBACK_MODEL,
    }
    _write_state(state)
    env = os.environ.copy()
    env.update({
        "SONDER_BASE": manifest["base_hf"],
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
        state.update(status="interrupted", ended_ts=int(time.time()))
        _write_state(state)
        return False, "Training interrupted cleanly; checkpoints were preserved for resume."
    except OSError as exc:
        state.update(status="failed", ended_ts=int(time.time()), error=str(exc))
        _write_state(state)
        return False, "Training process could not start; the run is preserved for an explicit resume."
    if result.returncode:
        state.update(status="failed", ended_ts=int(time.time()), returncode=result.returncode)
        _write_state(state)
        return False, "Training failed; checkpoints were preserved. Check the output above before resuming."
    adapter_ok, detail = validate_adapter(output, manifest["base_hf"])
    if not adapter_ok:
        state.update(status="failed_validation", ended_ts=int(time.time()), error=detail)
        _write_state(state)
        return False, f"Training process exited successfully but adapter validation failed: {detail}"
    state.update(status="trained", ended_ts=int(time.time()), manifest=str(output / "training-manifest.json"))
    _write_state(state)
    return True, f"Training completed; adapter saved at {output}. Run `training deploy`."


def training_status():
    return json.dumps(_read_state(), indent=2, sort_keys=True)


def validate_adapter(adapter_dir, expected_base=""):
    adapter_dir = Path(adapter_dir)
    config_path = adapter_dir / "adapter_config.json"
    manifest_path = adapter_dir / "training-manifest.json"
    if not config_path.exists() or not manifest_path.exists():
        return False, "adapter_config.json and training-manifest.json are required"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"invalid adapter metadata: {exc}"
    configured = str(config.get("base_model_name_or_path") or "").rstrip("/")
    trained = str(manifest.get("base_hf") or "").rstrip("/")
    if not configured or configured != trained:
        return False, f"adapter/base mismatch: PEFT={configured!r}, manifest={trained!r}"
    if expected_base and trained != expected_base.rstrip("/"):
        return False, f"adapter base {trained!r} does not match expected {expected_base!r}"
    ollama_base = str(manifest.get("base_ollama") or "")
    size = str(manifest.get("model_size") or "")
    if size not in MODEL_SPECS or ollama_base != MODEL_SPECS[size]["ollama"]:
        return False, "manifest Ollama base is not the exact mapped Qwen2.5-Coder base"
    return True, manifest


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


def _validated_output(result):
    return result.returncode == 0 and (result.stdout or "").strip() == "SONDER_VALID"


def _run_external(runner, command, **kwargs):
    try:
        return runner(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command, 124, stdout=exc.stdout or "", stderr="command timed out"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 126, stdout="", stderr=str(exc))


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def _deployment_lock():
    path = state_path().with_name("training-deployment.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {
        "token": token, "pid": os.getpid(), "host": socket.gethostname(),
        "created_ts": time.time(),
    }
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            break
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
            local_live = (
                current.get("host") == socket.gethostname()
                and _pid_alive(current.get("pid"))
            )
            recent = time.time() - float(current.get("created_ts") or 0) < 7200
            if local_live or recent:
                raise RuntimeError("another training deployment is already running")
            with contextlib.suppress(OSError):
                path.unlink()
    else:
        raise RuntimeError("could not acquire the training deployment lock")
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        if current.get("token") == token:
            with contextlib.suppress(OSError):
                path.unlink()


def deploy(adapter_dir="", *, converter="", ollama="", runner=subprocess.run):
    try:
        with _deployment_lock():
            return _deploy_locked(
                adapter_dir, converter=converter, ollama=ollama, runner=runner
            )
    except RuntimeError as exc:
        return False, f"Deployment blocked: {exc}"


def _deploy_locked(adapter_dir="", *, converter="", ollama="", runner=subprocess.run):
    run = lambda command, **kwargs: _run_external(runner, command, **kwargs)
    state = _read_state()
    adapter_dir = Path(adapter_dir or state.get("adapter_dir") or ROOT / "sonder-personal-lora")
    ok, manifest = validate_adapter(adapter_dir, state.get("base_hf", ""))
    if not ok:
        return False, f"Deployment blocked: {manifest}"
    converter_path = _converter_path(converter)
    if not converter_path:
        return False, (
            "Deployment blocked: llama.cpp/convert_lora_to_gguf.py was not found. "
            "Set SONDER_LLAMA_CPP to a current llama.cpp checkout. Raw PEFT "
            "Safetensors are not used for Qwen deployment."
        )
    required = MODEL_SPECS[manifest["model_size"]]["params"] * 1.5 + 2
    disk_ok, free = _disk_ok(adapter_dir, required)
    if not disk_ok:
        return False, f"Deployment blocked: {free:.1f} GB disk free; about {required:.1f} GB required."
    gguf = adapter_dir / "sonder-personal-lora.gguf"
    base_dir = os.environ.get("SONDER_HF_BASE_DIR", "").strip()
    command = [sys.executable, str(converter_path), str(adapter_dir), "--outfile", str(gguf), "--outtype", "f16"]
    if base_dir:
        command.extend(["--base", base_dir])
    else:
        # Pin the converter to the identity already checked in PEFT metadata;
        # do not let a stale cache or renamed local folder choose the base.
        command.extend(["--base-model-id", manifest["base_hf"]])
    converted = run(command, cwd=converter_path.parent, timeout=1800)
    if converted.returncode or not gguf.exists() or gguf.stat().st_size < 1024:
        return False, "GGUF adapter conversion failed; the runtime policy was not changed."
    ollama = ollama or os.environ.get("SONDER_OLLAMA_EXE", "").strip() or shutil.which("ollama") or "ollama"
    base_probe = run(
        [ollama, "show", manifest["base_ollama"]],
        capture_output=True, text=True, timeout=30,
    )
    if base_probe.returncode:
        return False, (
            f"Deployment blocked: exact Ollama base {manifest['base_ollama']} is not installed. "
            "No substitute base was selected."
        )
    deployment_id = "%s-%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    candidate = f"sonder-personal-candidate:{deployment_id}"
    modelfile = adapter_dir / "Modelfile.personal"
    modelfile.write_text(
        f"FROM {manifest['base_ollama']}\nADAPTER {gguf.resolve()}\nPARAMETER temperature 0.2\n",
        encoding="utf-8",
    )
    created = run(
        [ollama, "create", candidate, "-f", str(modelfile)],
        capture_output=True, text=True, timeout=600,
    )
    if created.returncode:
        run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
        return False, "Ollama candidate creation failed; existing models and policy were preserved."
    probe = run(
        [ollama, "run", candidate, "Reply with only: SONDER_VALID"],
        capture_output=True, text=True, timeout=120,
    )
    if not _validated_output(probe):
        run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
        return False, (
            "Candidate inference validation failed: expected the exact marker "
            "SONDER_VALID; runtime policy was not changed."
        )

    previous_exists = run(
        [ollama, "show", PERSONAL_MODEL],
        capture_output=True, text=True, timeout=30,
    ).returncode == 0
    previous_alias = ""
    if previous_exists:
        previous_alias = f"sonder-personal-previous:{deployment_id}"
        preserved = run(
            [ollama, "cp", PERSONAL_MODEL, previous_alias],
            capture_output=True, text=True, timeout=30,
        )
        if preserved.returncode:
            run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
            return False, (
                "Deployment blocked: the existing personal model could not be "
                "preserved before promotion."
            )

    prior_policy = runtime_policy.load(create=True)
    prior_models = dict(prior_policy["local_models"])
    active_personal = any(model == PERSONAL_MODEL for model in prior_models.values())

    def restore_prior_policy():
        try:
            runtime_policy.update(
                local_models=prior_models,
                source="failed personal deployment policy restore",
            )
            return True
        except (OSError, ValueError):
            return False

    if active_personal:
        safe_models = {
            tier: ROLLBACK_MODEL if model == PERSONAL_MODEL else model
            for tier, model in prior_models.items()
        }
        try:
            runtime_policy.update(
                local_models=safe_models,
                source="safe personal model deployment transition",
            )
        except (OSError, ValueError) as exc:
            run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
            return False, f"Deployment blocked: could not quiesce active personal alias: {exc}"

    promoted = run(
        [ollama, "cp", candidate, PERSONAL_MODEL],
        capture_output=True, text=True, timeout=30,
    )
    if promoted.returncode:
        run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
        if active_personal:
            restore_prior_policy()
        return False, "Personal alias promotion failed; runtime policy was not changed."

    def restore_previous_alias():
        if previous_alias:
            restored = run(
                [ollama, "cp", previous_alias, PERSONAL_MODEL],
                capture_output=True, text=True, timeout=30,
            )
            return restored.returncode == 0
        removed = run(
            [ollama, "rm", PERSONAL_MODEL],
            capture_output=True, text=True, timeout=30,
        )
        return removed.returncode == 0

    final_probe = run(
        [ollama, "run", PERSONAL_MODEL, "Reply with only: SONDER_VALID"],
        capture_output=True, text=True, timeout=120,
    )
    run([ollama, "rm", candidate], capture_output=True, text=True, timeout=30)
    if not _validated_output(final_probe):
        restored = restore_previous_alias()
        policy_restored = restore_prior_policy() if active_personal else True
        return False, (
            "Final model validation failed; the previous personal alias and policy were restored."
            if restored and policy_restored else
            "Final model validation failed and automatic personal-alias restoration failed; "
            f"preserved recovery alias: {previous_alias or '(none)'}."
        )
    try:
        desired_models = dict(prior_models)
        desired_models.update({"code": PERSONAL_MODEL, "general": PERSONAL_MODEL})
        policy = runtime_policy.update(
            local_models=desired_models,
            source="validated personal QLoRA deployment",
        )
    except (OSError, ValueError) as exc:
        restored = restore_previous_alias()
        policy_restored = restore_prior_policy()
        return False, (
            f"Personal model validated, but runtime policy activation failed: {exc}. "
            + (
                "The previous personal alias and policy were restored. "
                if restored and policy_restored else
                f"Alias restoration failed; preserved recovery alias: {previous_alias or '(none)'}. "
            )
            + f"Code/general policy remains unchanged; {ROLLBACK_MODEL} is available."
        )
    state.update(
        status="deployed", deployed_ts=int(time.time()), model=PERSONAL_MODEL,
        previous_personal_model=previous_alias,
        policy_revision=policy["revision"],
    )
    _write_state(state)
    return True, f"Validated and deployed {PERSONAL_MODEL}; {ROLLBACK_MODEL} remains available for rollback."


def rollback():
    policy = runtime_policy.update(
        local_models={"code": ROLLBACK_MODEL, "general": ROLLBACK_MODEL},
        source="training rollback",
    )
    state = _read_state()
    state.update(status="rolled_back", rollback_ts=int(time.time()), policy_revision=policy["revision"])
    _write_state(state)
    return True, f"Rolled code/general back to {ROLLBACK_MODEL}. Personal models and checkpoints were not deleted."


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
