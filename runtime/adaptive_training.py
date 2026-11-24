"""Hardware-aware inference/training planning and attended training lifecycle.

This module is stdlib-only. Heavy ML dependencies remain isolated in
``qlora_train.py`` so hardware/status/dry-run commands work on normal installs.
"""
from __future__ import annotations

import sys as _sonder_sys
from pathlib import Path as _SonderPath
_SONDER_REPO_ROOT = _SonderPath(__file__).resolve().parents[1]
for _sonder_p in (str(_SONDER_REPO_ROOT / "scripts"), str(_SONDER_REPO_ROOT / "runtime"), str(_SONDER_REPO_ROOT)):
    if _sonder_p not in _sonder_sys.path:
        _sonder_sys.path.insert(0, _sonder_p)
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
import uuid
from pathlib import Path

import sonder_runtime.adapters.runtime_policy as runtime_policy
import promotion_eval
from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive
from sonder_runtime.adapters.process_liveness import PROCESS_DEAD, probe_process, process_identity
from sonder_runtime.adapters.training import (
    alias_lifecycle as _alias_lifecycle,
    artifacts as _artifacts,
    deployment as _deployment,
)
from sonder_runtime.platform import system_profile
import sonder_paths
import training_data


_MAX_TRAINING_JOBS: int | None = None


def configure_training_capacity(max_jobs: int) -> None:
    global _MAX_TRAINING_JOBS
    if max_jobs < 0:
        raise ValueError("training_jobs must be >= 0")
    _MAX_TRAINING_JOBS = int(max_jobs)


ROOT = Path(__file__).resolve().parents[1]
LLAMA_CPP_REVISION = "99f3dc32296f825fec94f202da1e9fede1e78cf9"
LLAMA_CPP_TREE_SHA256 = "3cb23bb624453dc3511df618bc1445169d44b8fb635bd04a1b3eabc45db2d4df"

from sonder_runtime.application.training.hardware_planning import (
    MODEL_ALIASES as MODEL_ALIASES,
    TRAINING_CPU_OFFLOAD_REASON as TRAINING_CPU_OFFLOAD_REASON,
    TRAINING_CPU_OFFLOAD_SUPPORTED as TRAINING_CPU_OFFLOAD_SUPPORTED,
    HardwarePlan as HardwarePlan,
    PlanOptions,
    Recommendation as Recommendation,
    build_plan as _application_build_plan,
    format_hardware as _application_format_hardware,
    format_plan as _application_format_plan,
    memory_budgets as _application_memory_budgets,
)

from sonder_runtime.adapters.model_list_consumers import training_attr, training_value as _list_value
import sonder_runtime.adapters.training_model_catalog as _training_catalog
__getattr__ = training_attr


def memory_budgets(profile, options):
    return _application_memory_budgets(profile, options)


def build_plan(profile=None, options=None):
    profile = profile or system_profile.detect_hardware(
        gpu_index=(options or PlanOptions()).gpu_index
    )
    return _application_build_plan(profile, options, specs=_list_value("MODEL_SPECS"))


def format_hardware(profile=None):
    return _application_format_hardware(profile or system_profile.detect_hardware())


def format_plan(plan):
    return _application_format_plan(plan)


def state_path():
    return Path(
        sonder_paths.state_path("training_state.json", "SONDER_TRAINING_STATE")
    ).expanduser().resolve()


def _read_state():
    path = state_path()
    if not path.exists():
        return {"status": "never_started", "rollback_model": _list_value('ROLLBACK_MODEL')}
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
    return _artifacts.write_json_atomic(path, payload)


def _sha256_file(path):
    return _artifacts.sha256_file(path)


def _launch_authorization_hmac(token, plan_sha256, data_sha256):
    return _artifacts.launch_authorization_hmac(token, plan_sha256, data_sha256)


def _inspect_launch_receipt(
    path, *, plan_sha256, data_sha256, authorization_hmac,
):
    return _artifacts.inspect_launch_receipt(
        path,
        plan_sha256=plan_sha256,
        data_sha256=data_sha256,
        authorization_hmac=authorization_hmac,
    )


def _inspect_export_manifest(path, inspection):
    return _artifacts.inspect_export_manifest(path, inspection)


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
    if _MAX_TRAINING_JOBS is not None and _MAX_TRAINING_JOBS < 1 and not dry_run:
        return False, "Training blocked: training_jobs capacity set to 0."
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
        expected_revision = _list_value('MODEL_SPECS')[plan.training.model_size]["hf_revision"]
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
    ok, free = _disk_ok(run_dir.parent, 3 + _list_value('MODEL_SPECS')[plan.training.model_size]["params"] * 2.2)
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
                claim_owner = _read_training_claim(claim)
            except (OSError, TypeError, ValueError):
                return False, "Training resume launch owner is unreadable."
            if _training_claim_alive(claim_owner):
                return False, (
                    "Training resume blocked: the prior training child is still running "
                    "or its identity is unknown; holder=%r" % claim_owner
                )
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
    launch_id = uuid.uuid4().hex
    manifest = {
        "schema": 2,
        "run_id": run_id,
        "base_hf": plan.training.model,
        "hf_revision": _list_value('MODEL_SPECS')[plan.training.model_size]["hf_revision"],
        "base_ollama": _list_value('MODEL_SPECS')[plan.training.model_size]["ollama"],
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
        "launch_id": launch_id,
        "launch_token_sha256": hashlib.sha256(launch_token.encode("utf-8")).hexdigest(),
        "plan": plan.to_dict(),
    }
    plan_file = run_dir / "training-plan.json"
    plan_sha256 = _write_json_atomic(plan_file, manifest)
    launch_authorization_hmac = _launch_authorization_hmac(
        launch_token, plan_sha256, data_sha256,
    )
    launch_receipt_path = run_dir / ("training-launch-%s.json" % launch_id)
    state = {
        "status": "running",
        "started_ts": int(time.time()),
        "adapter_dir": str(output),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "plan_file": str(plan_file),
        "plan_sha256": plan_sha256,
        "launch_id": launch_id,
        "launch_receipt_path": str(launch_receipt_path),
        "launch_authorization_hmac": launch_authorization_hmac,
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
        "rollback_model": _list_value('ROLLBACK_MODEL'),
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
        "SONDER_ALLOW_CPU_OFFLOAD": "0",
        "SONDER_TRAIN_GPU_BUDGET_GB": str(plan.usable_vram_gb),
        "SONDER_TRAIN_RAM_BUDGET_GB": str(plan.usable_system_ram_gb),
        "SONDER_TRAINING_MANIFEST": str(plan_file),
        "SONDER_TRAINING_LAUNCH_TOKEN": launch_token,
        "SONDER_TRAINING_PLAN_SHA256": plan_sha256,
        "SONDER_TRAINING_DATA_SHA256": data_sha256,
        "SONDER_TRAINING_RECEIPT": str(launch_receipt_path),
        "SONDER_RESUME": "1" if resume else "0",
    })
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
    return _artifacts.path_has_control_chars(value)


def _path_uses_symlink(path):
    return _artifacts.path_uses_symlink(path)


def _json_object(path, label):
    return _artifacts.json_object(path, label)


def validate_adapter(adapter_dir, expected_base=""):
    return _artifacts.validate_adapter(
        adapter_dir,
        expected_base,
        model_specs=_list_value("MODEL_SPECS"),
    )


def _validate_deployment_adapter(adapter_dir, state):
    return _artifacts.validate_deployment_adapter(
        adapter_dir,
        state,
        inspect_jsonl_fn=training_data.inspect_jsonl,
        training_data_error_cls=training_data.TrainingDataError,
        validate_adapter_fn=validate_adapter,
    )


def _converter_path(explicit=""):
    return _artifacts.converter_path(explicit, repo_root=ROOT)


def _stage_reviewed_converter(converter_path, destination, runner=subprocess.run):
    return _artifacts.stage_reviewed_converter(
        converter_path,
        destination,
        revision=LLAMA_CPP_REVISION,
        expected_tree_sha256=LLAMA_CPP_TREE_SHA256,
        runner=runner,
        run_external_fn=_run_external,
    )


def _run_external(runner, command, *, ollama_command=False, **kwargs):
    return _artifacts.run_external(
        runner, command, ollama_command=ollama_command, **kwargs
    )


def _model_show_status(result, model):
    return _artifacts.model_show_status(result, model)


def _show_identity(result):
    return _artifacts.show_identity(result)


def _bounded_error(exc, limit=300):
    return _artifacts.bounded_error(exc, limit)


def _ollama_executable(explicit=""):
    return _artifacts.ollama_executable(explicit)


def _shared_alias_paths():
    return _alias_lifecycle.shared_alias_paths(
        local_ollama_origin_fn=promotion_eval._local_ollama_origin,
    )


def _read_shared_alias_record(kind):
    return _alias_lifecycle.read_shared_alias_record(
        kind, shared_alias_paths_fn=_shared_alias_paths,
    )


def _clear_shared_alias_transition(deployment_id, policy_token):
    return _alias_lifecycle.clear_shared_alias_transition(
        deployment_id,
        policy_token,
        shared_alias_paths_fn=_shared_alias_paths,
        read_record_fn=_read_shared_alias_record,
    )


def _claim_shared_alias_transition(deployment_id, policy_token):
    return _alias_lifecycle.claim_shared_alias_transition(
        deployment_id,
        policy_token,
        shared_alias_paths_fn=_shared_alias_paths,
        read_record_fn=_read_shared_alias_record,
        deployment_journal_path_fn=_deployment_journal_path,
    )


def _advance_shared_alias_transition(payload, phase):
    return _alias_lifecycle.advance_shared_alias_transition(
        payload, phase, shared_alias_paths_fn=_shared_alias_paths,
    )


def _prepare_shared_alias_lifecycle(*, require_owner=False):
    return _alias_lifecycle.prepare_shared_alias_lifecycle(
        require_owner=require_owner,
        read_record_fn=_read_shared_alias_record,
        deployment_journal_path_fn=_deployment_journal_path,
        clear_transition_fn=_clear_shared_alias_transition,
    )


def _validate_shared_alias_owner(
    *, prior_policy, personal_status, personal_identity, personal_digest, state,
):
    return _alias_lifecycle.validate_shared_alias_owner(
        prior_policy=prior_policy,
        personal_status=personal_status,
        personal_identity=personal_identity,
        personal_digest=personal_digest,
        state=state,
        read_record_fn=_read_shared_alias_record,
        personal_model=_list_value("PERSONAL_MODEL"),
    )


def _write_shared_alias_owner(*, deployment_id, identity, digest, personal_model=None):
    _alias_lifecycle.write_shared_alias_owner(
        deployment_id=deployment_id,
        identity=identity,
        digest=digest,
        personal_model=(
            personal_model
            if personal_model is not None
            else _list_value("PERSONAL_MODEL")
        ),
        shared_alias_paths_fn=_shared_alias_paths,
    )


def _deployment_journal_path():
    return runtime_policy.transition_path()


def _cleanup_pending_path():
    return _alias_lifecycle.cleanup_pending_path()


def _validated_pending_cleanup_models(payload):
    return _alias_lifecycle.validated_pending_cleanup_models(payload)


def _record_pending_model_cleanup(model):
    return _alias_lifecycle.record_pending_model_cleanup(
        model, cleanup_pending_path_fn=_cleanup_pending_path,
    )


def _forget_pending_model_cleanup(model):
    return _alias_lifecycle.forget_pending_model_cleanup(
        model, cleanup_pending_path_fn=_cleanup_pending_path,
    )


def _reconcile_pending_model_cleanup(*, ollama="", runner=subprocess.run):
    return _alias_lifecycle.reconcile_pending_model_cleanup(
        ollama=ollama,
        runner=runner,
        cleanup_pending_path_fn=_cleanup_pending_path,
        run_external_fn=_run_external,
    )


def _write_deployment_journal(payload):
    _alias_lifecycle.write_deployment_journal(
        payload, deployment_journal_path_fn=_deployment_journal_path,
    )


def _clear_deployment_journal(deployment_id):
    return _alias_lifecycle.clear_deployment_journal(
        deployment_id,
        deployment_journal_path_fn=_deployment_journal_path,
        read_record_fn=_read_shared_alias_record,
        advance_transition_fn=_advance_shared_alias_transition,
        clear_transition_fn=_clear_shared_alias_transition,
    )


def _reconcile_pending_deployment(*, ollama="", runner=subprocess.run):
    hooks = _alias_lifecycle.ReconcileHooks(
        deployment_journal_path=_deployment_journal_path,
        read_shared_alias_record=_read_shared_alias_record,
        write_shared_alias_owner=_write_shared_alias_owner,
        write_deployment_journal=_write_deployment_journal,
        clear_deployment_journal=_clear_deployment_journal,
        record_pending_model_cleanup=_record_pending_model_cleanup,
        forget_pending_model_cleanup=_forget_pending_model_cleanup,
        local_model_digest=promotion_eval.local_model_digest,
        run_external=_run_external,
    )
    return _alias_lifecycle.reconcile_pending_deployment(
        ollama=ollama, runner=runner, hooks=hooks,
    )


def _pid_alive(pid):
    return _process_pid_alive(pid)


def _recorded_training_child_alive(lock_payload):
    try:
        state_file = Path(lock_payload["state_path"])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not state.get("run_dir"):
            return False
        claim = Path(state["run_dir"]) / ".launch-claimed"
        owner = _read_training_claim(claim)
    except FileNotFoundError:
        return False
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return True
    return _training_claim_alive(owner)


def _read_training_claim(path):
    with Path(path).open(encoding="ascii") as stream:
        raw = stream.read(4096).strip()
    value = json.loads(raw)
    if isinstance(value, int):
        return {"pid": value}
    if not isinstance(value, dict) or not isinstance(value.get("pid"), int):
        raise ValueError("invalid training child identity")
    return value


def _training_claim_alive(owner):
    if owner.get("host") not in (None, "", socket.gethostname()):
        return True
    state, _ = probe_process(owner["pid"], expected_identity=owner.get("process_identity"))
    return state != PROCESS_DEAD


@contextlib.contextmanager
def _exclusive_byte_lock(path):
    from durable_locks import LockTimeout, exclusive_file_lock

    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_file_lock(path, timeout=0, purpose="training lifecycle"):
            yield
    except LockTimeout as exc:
        raise RuntimeError(
            "another training lifecycle operation is already running; %s" % exc
        ) from exc


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
    owner_path = home_lock.with_name(home_lock.name + ".lifecycle-owner.json")
    try:
        legacy_owner = _json_object(
            home_lock.with_name(home_lock.name + ".owner.json"), "training lifecycle owner",
        )
    except ValueError:
        legacy_owner = {}
    token = uuid.uuid4().hex
    payload = {
        "token": token, "pid": os.getpid(), "host": socket.gethostname(),
        "created_ts": time.time(),
        "process_identity": process_identity(os.getpid()),
        "state_path": str(state_path()),
    }
    with contextlib.ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(_exclusive_byte_lock(lock_path))
        stack.enter_context(_training_catalog.operation())
        try:
            old_owner = _json_object(owner_path, "training lifecycle owner")
        except ValueError:
            old_owner = legacy_owner
        current_owner = {"state_path": str(state_path())}
        if (
            (old_owner and _recorded_training_child_alive(old_owner))
            or _recorded_training_child_alive(current_owner)
        ):
            raise RuntimeError(
                "an orphaned training child is still running or its identity is unknown; "
                "state_path=%s prior_state_path=%s"
                % (current_owner["state_path"], old_owner.get("state_path", "unknown"))
            )
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
    hooks = _deployment.DeploymentHooks(
        repo_root=ROOT,
        llama_cpp_revision=LLAMA_CPP_REVISION,
        list_value=_list_value,
        state_path=state_path,
        read_state=_read_state,
        write_state=_write_state,
        disk_ok=_disk_ok,
        validate_deployment_adapter=_validate_deployment_adapter,
        converter_path=_converter_path,
        stage_reviewed_converter=_stage_reviewed_converter,
        run_external=_run_external,
        record_pending_model_cleanup=_record_pending_model_cleanup,
        forget_pending_model_cleanup=_forget_pending_model_cleanup,
        claim_shared_alias_transition=_claim_shared_alias_transition,
        advance_shared_alias_transition=_advance_shared_alias_transition,
        clear_shared_alias_transition=_clear_shared_alias_transition,
        validate_shared_alias_owner=_validate_shared_alias_owner,
        write_shared_alias_owner=_write_shared_alias_owner,
        write_deployment_journal=_write_deployment_journal,
        clear_deployment_journal=_clear_deployment_journal,
        local_model_digest=promotion_eval.local_model_digest,
        evaluate_pair=promotion_eval.evaluate_pair,
        promotion_decision=promotion_eval.promotion_decision,
        evaluate_model=promotion_eval.evaluate_model,
        validate_model_report=promotion_eval.validate_model_report,
    )
    return _deployment.deploy_locked(
        adapter_dir, converter=converter, ollama=ollama, runner=runner, hooks=hooks,
    )


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


def _alias_ownership_hooks():
    return _deployment.AliasOwnershipHooks(
        list_value=_list_value,
        deployment_lock=_deployment_lock,
        prepare_shared_alias_lifecycle=_prepare_shared_alias_lifecycle,
        read_shared_alias_record=_read_shared_alias_record,
        shared_alias_paths=_shared_alias_paths,
        write_shared_alias_owner=_write_shared_alias_owner,
        run_external=_run_external,
        local_model_digest=promotion_eval.local_model_digest,
    )


def adopt_legacy_personal(*, confirmed=False, ollama="", runner=subprocess.run):
    return _deployment.adopt_legacy_personal(
        confirmed=confirmed,
        ollama=ollama,
        runner=runner,
        hooks=_alias_ownership_hooks(),
    )


def release_personal_owner(*, confirmed=False):
    return _deployment.release_personal_owner(
        confirmed=confirmed, hooks=_alias_ownership_hooks(),
    )


def _rollback_locked(*, ollama="", runner=subprocess.run):
    hooks = _deployment.RollbackHooks(
        list_value=_list_value,
        state_path=state_path,
        read_state=_read_state,
        write_state=_write_state,
        run_external=_run_external,
        local_model_digest=promotion_eval.local_model_digest,
        write_deployment_journal=_write_deployment_journal,
        clear_deployment_journal=_clear_deployment_journal,
        reconcile_pending_deployment=_reconcile_pending_deployment,
    )
    return _deployment.rollback_locked(ollama=ollama, runner=runner, hooks=hooks)


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
