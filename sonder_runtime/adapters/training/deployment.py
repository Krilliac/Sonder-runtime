"""Deployment, rollback, and personal-alias adoption/release phase execution."""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, cast

import sonder_runtime.adapters.runtime_policy as runtime_policy
import sonder_runtime.adapters.training_model_catalog as _training_catalog
from sonder_runtime.adapters.training.artifacts import (
    RunnerFn,
    bounded_error,
    model_show_status,
    ollama_executable,
    sha256_file,
    show_identity,
    write_json_atomic,
)


_policy_load = cast(Callable[..., dict[str, Any]], runtime_policy.load)
_policy_update = cast(Callable[..., dict[str, Any]], runtime_policy.update)
_reserve_transition = cast(
    Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
    runtime_policy.reserve_transition,
)
_MODEL_RE = runtime_policy._MODEL_RE


@dataclass(frozen=True)
class DeploymentHooks:
    """Injectable host callbacks for :func:`deploy_locked`."""

    repo_root: Path
    llama_cpp_revision: str
    list_value: Callable[[str], Any]
    state_path: Callable[[], Path]
    read_state: Callable[[], dict[str, Any]]
    write_state: Callable[[dict[str, Any]], Path]
    disk_ok: Callable[[str | Path, float], tuple[bool, float]]
    validate_deployment_adapter: Callable[[str | Path, Mapping[str, Any]], tuple[bool, Any]]
    converter_path: Callable[[str], Path | None]
    stage_reviewed_converter: Callable[..., tuple[bool, Any]]
    run_external: Callable[..., subprocess.CompletedProcess[Any]]
    record_pending_model_cleanup: Callable[[str], bool]
    forget_pending_model_cleanup: Callable[[str], bool]
    claim_shared_alias_transition: Callable[[str, str], dict[str, Any]]
    advance_shared_alias_transition: Callable[[Mapping[str, Any], str], dict[str, Any]]
    clear_shared_alias_transition: Callable[[str, str], bool]
    validate_shared_alias_owner: Callable[..., tuple[bool, Any]]
    write_shared_alias_owner: Callable[..., None]
    write_deployment_journal: Callable[[Mapping[str, Any]], None]
    clear_deployment_journal: Callable[[str], bool]
    local_model_digest: Callable[[str], str]
    evaluate_pair: Callable[..., dict[str, Any]]
    promotion_decision: Callable[..., tuple[bool, str]]
    evaluate_model: Callable[..., dict[str, Any]]
    validate_model_report: Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class RollbackHooks:
    """Injectable host callbacks for :func:`rollback_locked`."""

    list_value: Callable[[str], Any]
    state_path: Callable[[], Path]
    read_state: Callable[[], dict[str, Any]]
    write_state: Callable[[dict[str, Any]], Path]
    run_external: Callable[..., subprocess.CompletedProcess[Any]]
    local_model_digest: Callable[[str], str]
    write_deployment_journal: Callable[[Mapping[str, Any]], None]
    clear_deployment_journal: Callable[[str], bool]
    reconcile_pending_deployment: Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class AliasOwnershipHooks:
    """Injectable host callbacks for legacy alias adoption and release."""

    list_value: Callable[[str], Any]
    deployment_lock: Callable[[], ContextManager[Any]]
    prepare_shared_alias_lifecycle: Callable[..., tuple[bool, str]]
    read_shared_alias_record: Callable[[str], dict[str, Any] | None]
    shared_alias_paths: Callable[[], Mapping[str, Any]]
    write_shared_alias_owner: Callable[..., None]
    run_external: Callable[..., subprocess.CompletedProcess[Any]]
    local_model_digest: Callable[[str], str]


def deploy_locked(
    adapter_dir: str | Path = "",
    *,
    converter: str = "",
    ollama: str = "",
    runner: RunnerFn = subprocess.run,
    hooks: DeploymentHooks,
) -> tuple[bool, str]:
    """Convert, gate, publish, verify, and activate a QLoRA personal adapter."""

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        is_ollama = bool(command) and os.path.normcase(
            str(command[0])
        ) == os.path.normcase(str(ollama_bin))
        return hooks.run_external(
            runner, command, ollama_command=is_ollama, **kwargs,
        )

    state = hooks.read_state()
    adapter_path = Path(
        adapter_dir or state.get("adapter_dir") or hooks.repo_root / "sonder-personal-lora"
    )
    ok, manifest = hooks.validate_deployment_adapter(adapter_path, state)
    if not ok:
        return False, f"Deployment blocked: {manifest}"
    converter_file = hooks.converter_path(converter)
    if not converter_file:
        return False, (
            "Deployment blocked: llama.cpp/convert_lora_to_gguf.py was not found. "
            f"Set SONDER_LLAMA_CPP to a checkout containing reviewed commit "
            f"{hooks.llama_cpp_revision}. Raw PEFT "
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
    required = float(hooks.list_value("MODEL_SPECS")[manifest["model_size"]]["params"]) * 1.5 + 2
    disk_ok, free = hooks.disk_ok(run_dir, required)
    if not disk_ok:
        return False, (
            f"Deployment blocked: {free:.1f} GB disk free; about {required:.1f} GB required."
        )
    deployment_id = "%s-%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    staging = run_dir / "deployment-staging" / deployment_id
    staging.mkdir(parents=True, exist_ok=False)
    gguf = staging / "sonder-personal-lora.gguf"
    candidate = f"sonder-personal-candidate:{deployment_id}"
    candidate_created = False
    ollama_bin = ollama_executable(ollama)
    report_path = run_dir / "deployment-evaluations" / f"{deployment_id}.json"
    previous_alias = ""
    previous_identity = ""
    previous_digest = ""
    candidate_identity = ""
    prior_policy: dict[str, Any] | None = None
    prior_models: dict[str, Any] | None = None
    policy_revision: int | None = None
    policy_mutated = False
    journal: dict[str, Any] | None = None
    shared_transition: dict[str, Any] | None = None
    policy_token = secrets.token_urlsafe(32)

    def write_receipt(payload: dict[str, Any]) -> str:
        write_json_atomic(report_path, payload)
        return sha256_file(report_path)

    def advance_journal(phase: str) -> None:
        if journal is None:
            raise RuntimeError("deployment journal was not prepared")
        journal.update(
            phase=phase,
            last_policy_revision=policy_revision,
            updated_ts=int(time.time()),
        )
        hooks.write_deployment_journal(journal)

    def restore_prior_policy() -> bool:
        nonlocal policy_revision, policy_mutated
        if not policy_mutated:
            return True
        try:
            restored_policy = _policy_update(
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

    def restore_previous_alias() -> bool:
        personal_model = str(hooks.list_value("PERSONAL_MODEL"))
        if previous_alias:
            backup_probe = run(
                [ollama_bin, "show", previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            try:
                backup_digest_matches = (
                    hooks.local_model_digest(previous_alias) == previous_digest
                )
            except (OSError, TypeError, ValueError):
                backup_digest_matches = False
            if (
                model_show_status(backup_probe, previous_alias) != "exists"
                or show_identity(backup_probe) != previous_identity
                or not backup_digest_matches
            ):
                return False
            restored = run(
                [ollama_bin, "cp", previous_alias, personal_model],
                capture_output=True, text=True, timeout=30,
            )
            if restored.returncode:
                return False
        else:
            run(
                [ollama_bin, "rm", personal_model],
                capture_output=True, text=True, timeout=30,
            )
        verified = run(
            [ollama_bin, "show", personal_model],
            capture_output=True, text=True, timeout=30,
        )
        expected = "exists" if previous_alias else "missing"
        if model_show_status(verified, personal_model) != expected:
            return False
        if not previous_alias:
            return True
        try:
            restored_digest_matches = (
                hooks.local_model_digest(personal_model) == previous_digest
            )
        except (OSError, TypeError, ValueError):
            restored_digest_matches = False
        return (
            show_identity(verified) == previous_identity
            and restored_digest_matches
        )

    def discard_previous_alias() -> bool:
        if not previous_alias:
            return True
        run(
            [ollama_bin, "rm", previous_alias],
            capture_output=True, text=True, timeout=30,
        )
        verified = run(
            [ollama_bin, "show", previous_alias],
            capture_output=True, text=True, timeout=30,
        )
        return model_show_status(verified, previous_alias) == "missing"

    def finalize_failed_recovery(alias_restored: bool, policy_restored: bool) -> bool:
        backup_cleaned = discard_previous_alias() if alias_restored else False
        if alias_restored and policy_restored and backup_cleaned and journal is not None:
            return hooks.clear_deployment_journal(deployment_id)
        return alias_restored and policy_restored and backup_cleaned

    def recover_policy(alias_restored: bool) -> bool:
        nonlocal policy_revision, policy_mutated
        if alias_restored:
            return restore_prior_policy()
        if not policy_mutated:
            return True
        current = _policy_load(create=False)
        if current.get("error") or current.get("revision") != policy_revision:
            return False
        personal_model = str(hooks.list_value("PERSONAL_MODEL"))
        rollback_model = str(hooks.list_value("ROLLBACK_MODEL"))
        safe_models = {
            tier: rollback_model if model == personal_model else model
            for tier, model in current["local_models"].items()
        }
        safe_models.update({"code": rollback_model, "general": rollback_model})
        if safe_models == current["local_models"]:
            policy_mutated = False
            return True
        try:
            safe_policy = _policy_update(
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
        converter_ok, converter_provenance = hooks.stage_reviewed_converter(
            converter_file, staging / "llama-cpp", runner=runner,
        )
        if not converter_ok:
            return False, f"Deployment blocked: {converter_provenance}."
        staged_converter = Path(converter_provenance["converter"])
        staged_converter_root = staged_converter.parent
        verified_adapter = staging / "verified-adapter"
        verified_adapter.mkdir()
        for name in ("adapter_config.json", "adapter_model.safetensors"):
            source = adapter_path / name
            target = verified_adapter / name
            shutil.copyfile(source, target)
            expected_hash = state["artifact_sha256"][name]
            expected_size = state["artifact_sizes"][name]
            if target.stat().st_size != expected_size or sha256_file(target) != expected_hash:
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
        base_config_sha256 = sha256_file(base_config)
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
            [ollama_bin, "show", manifest["base_ollama"]],
            capture_output=True, text=True, timeout=30,
        )
        base_status = model_show_status(base_probe, manifest["base_ollama"])
        if base_status != "exists":
            detail = "is not installed" if base_status == "missing" else "could not be verified"
            return False, (
                f"Deployment blocked: exact Ollama base {manifest['base_ollama']} {detail}. "
                "No substitute base was selected."
            )
        try:
            base_digest = hooks.local_model_digest(manifest["base_ollama"])
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Deployment blocked: exact base digest could not be verified: {exc}"

        modelfile = staging / "Modelfile.personal"
        modelfile.write_text(
            f"FROM {manifest['base_ollama']}\nADAPTER {gguf.resolve()}\n"
            "PARAMETER temperature 0\nPARAMETER seed 424242\nPARAMETER num_ctx 2048\n",
            encoding="utf-8",
        )
        try:
            if not hooks.record_pending_model_cleanup(candidate):
                return False, "Candidate cleanup ownership could not be recorded."
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Candidate cleanup ownership could not be recorded: {exc}"
        candidate_created = True
        created = run(
            [ollama_bin, "create", candidate, "-f", str(modelfile)],
            capture_output=True, text=True, timeout=600,
        )
        if created.returncode:
            return False, (
                "Ollama candidate creation failed; existing models and policy were preserved."
            )
        candidate_probe = run(
            [ollama_bin, "show", candidate], capture_output=True, text=True, timeout=30,
        )
        if model_show_status(candidate_probe, candidate) != "exists":
            return False, "Ollama candidate could not be verified after creation."
        candidate_identity = show_identity(candidate_probe)
        if not candidate_identity:
            return False, "Ollama candidate identity could not be read after creation."
        try:
            candidate_digest = hooks.local_model_digest(candidate)
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Ollama candidate digest could not be verified: {exc}"

        receipt: dict[str, Any] = {
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
            "gguf_sha256": sha256_file(gguf),
            "base_show_sha256": hashlib.sha256(
                (base_probe.stdout or "").encode("utf-8", "replace")
            ).hexdigest(),
            "candidate_show_sha256": hashlib.sha256(
                (candidate_probe.stdout or "").encode("utf-8", "replace")
            ).hexdigest(),
            "status": "evaluating",
        }
        try:
            behavior = hooks.evaluate_pair(
                manifest["base_ollama"], candidate, challenge=deployment_id,
            )
            passed, reason = hooks.promotion_decision(
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
                decision=f"{type(exc).__name__}: {bounded_error(exc)}",
            )
            with contextlib.suppress(OSError, TypeError, ValueError):
                write_receipt(receipt)
            return False, (
                "Candidate behavior evaluation failed closed; runtime policy was not changed."
            )
        if not passed:
            return False, f"Candidate behavior evaluation rejected promotion: {reason}"
        try:
            if hooks.local_model_digest(manifest["base_ollama"]) != base_digest:
                return False, "Exact base alias changed during behavior evaluation."
            if hooks.local_model_digest(candidate) != candidate_digest:
                return False, "Candidate alias changed during behavior evaluation."
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Model digest revalidation failed closed: {exc}"

        personal_model = str(hooks.list_value("PERSONAL_MODEL"))
        previous_probe = run(
            [ollama_bin, "show", personal_model],
            capture_output=True, text=True, timeout=30,
        )
        previous_status = model_show_status(previous_probe, personal_model)
        if previous_status == "error":
            return False, (
                "Deployment blocked: could not determine existing personal model state; "
                "the alias and runtime policy were preserved."
            )
        if previous_status == "exists":
            previous_identity = show_identity(previous_probe)
            if not previous_identity:
                return False, "Deployment blocked: existing personal model identity is unreadable."
            try:
                previous_digest = hooks.local_model_digest(personal_model)
            except (OSError, TypeError, ValueError) as exc:
                return False, f"Deployment blocked: personal alias digest is unreadable: {exc}"
            previous_alias = f"sonder-personal-previous:{deployment_id}"

        try:
            shared_transition = hooks.claim_shared_alias_transition(
                deployment_id, policy_token,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"Deployment blocked: could not claim personal alias: {exc}"

        journal = {
            "schema": 1,
            "operation": "deploy",
            **_training_catalog.record(),
            "deployment_id": deployment_id,
            "state_path": str(hooks.state_path()),
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
            prior_policy, journal = _reserve_transition(journal)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with contextlib.suppress(OSError, TypeError, ValueError):
                hooks.clear_shared_alias_transition(deployment_id, policy_token)
            return False, f"Deployment blocked: could not reserve runtime policy transition: {exc}"
        prior_models = dict(prior_policy["local_models"])
        policy_revision = int(prior_policy["revision"])
        try:
            shared_transition = hooks.advance_shared_alias_transition(
                shared_transition, "reserved",
            )
        except (OSError, TypeError, ValueError) as exc:
            hooks.clear_deployment_journal(deployment_id)
            return False, f"Deployment blocked: shared alias reservation failed: {exc}"

        personal_model = str(hooks.list_value("PERSONAL_MODEL"))
        reserved_probe = run(
            [ollama_bin, "show", personal_model],
            capture_output=True, text=True, timeout=30,
        )
        reserved_status = model_show_status(reserved_probe, personal_model)
        alias_unchanged = reserved_status == previous_status
        if previous_status == "exists":
            try:
                reserved_digest = hooks.local_model_digest(personal_model)
            except (OSError, TypeError, ValueError):
                reserved_digest = ""
            alias_unchanged = (
                alias_unchanged
                and show_identity(reserved_probe) == previous_identity
                and reserved_digest == previous_digest
            )
        if not alias_unchanged:
            hooks.clear_deployment_journal(deployment_id)
            return False, "Deployment blocked: personal alias changed before transition reservation."
        owner_ok, owner_detail = hooks.validate_shared_alias_owner(
            prior_policy=prior_policy,
            personal_status=previous_status,
            personal_identity=previous_identity,
            personal_digest=previous_digest,
            state=state,
        )
        if not owner_ok:
            hooks.clear_deployment_journal(deployment_id)
            return False, f"Deployment blocked: {owner_detail}."

        if previous_status == "exists":
            preserved = run(
                [ollama_bin, "cp", str(hooks.list_value("PERSONAL_MODEL")), previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            verified_backup = (
                run(
                    [ollama_bin, "show", previous_alias],
                    capture_output=True, text=True, timeout=30,
                )
                if preserved.returncode == 0
                else preserved
            )
            try:
                backup_digest = (
                    hooks.local_model_digest(previous_alias)
                    if preserved.returncode == 0
                    else ""
                )
            except (OSError, TypeError, ValueError):
                backup_digest = ""
            if (
                preserved.returncode
                or model_show_status(verified_backup, previous_alias) != "exists"
                or show_identity(verified_backup) != previous_identity
                or backup_digest != previous_digest
            ):
                run([ollama_bin, "rm", previous_alias], capture_output=True, text=True, timeout=30)
                removed_backup = run(
                    [ollama_bin, "show", previous_alias],
                    capture_output=True, text=True, timeout=30,
                )
                cleanup_owned = model_show_status(removed_backup, previous_alias) == "missing"
                if not cleanup_owned:
                    try:
                        cleanup_owned = hooks.record_pending_model_cleanup(previous_alias)
                    except (OSError, TypeError, ValueError):
                        cleanup_owned = False
                if cleanup_owned:
                    hooks.clear_deployment_journal(deployment_id)
                return False, (
                    "Deployment blocked: the existing personal model could not be "
                    "preserved and verified before promotion."
                    + ("" if cleanup_owned else " Recovery cleanup needs attention.")
                )
            try:
                advance_journal("backed_up")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return False, f"Deployment backup journal update failed: {exc}"

        active_personal = any(
            model == hooks.list_value("PERSONAL_MODEL") for model in prior_models.values()
        )
        if active_personal:
            rollback_model = str(hooks.list_value("ROLLBACK_MODEL"))
            personal_model = str(hooks.list_value("PERSONAL_MODEL"))
            safe_models = {
                tier: rollback_model if model == personal_model else model
                for tier, model in prior_models.items()
            }
            try:
                quiesced = _policy_update(
                    local_models=safe_models,
                    source="safe personal model deployment transition",
                    expected_revision=policy_revision,
                    transition_token=policy_token,
                )
                policy_revision = int(quiesced["revision"])
                policy_mutated = True
                advance_journal("quiesced")
            except (OSError, RuntimeError, ValueError) as exc:
                restored = restore_previous_alias()
                policy_restored = recover_policy(restored)
                finalize_failed_recovery(restored, policy_restored)
                return False, f"Deployment blocked: could not quiesce active personal alias: {exc}"

        try:
            advance_journal("publishing")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Personal alias publication was not started: {exc}"

        promoted = run(
            [ollama_bin, "cp", candidate, str(hooks.list_value("PERSONAL_MODEL"))],
            capture_output=True, text=True, timeout=30,
        )
        if promoted.returncode:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                "Personal alias promotion failed; the previous alias and policy were restored."
                if restored and policy_restored
                else (
                    "Personal alias promotion failed and recovery needs attention; preserved alias: "
                    f"{previous_alias or '(none)'}."
                )
            )

        try:
            advance_journal("published")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Published alias recovery journal update failed: {exc}"

        personal_model = str(hooks.list_value("PERSONAL_MODEL"))
        published_probe = run(
            [ollama_bin, "show", personal_model],
            capture_output=True, text=True, timeout=30,
        )
        if (
            model_show_status(published_probe, personal_model) != "exists"
            or show_identity(published_probe) != candidate_identity
        ):
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                "Published personal alias identity did not match the candidate; "
                "the previous alias and policy were restored."
                if restored and policy_restored
                else (
                    "Published personal alias identity mismatch and recovery needs attention; "
                    f"preserved recovery alias: {previous_alias or '(none)'} ."
                )
            )
        try:
            if hooks.local_model_digest(personal_model) != candidate_digest:
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
            final_behavior = hooks.evaluate_model(
                personal_model, challenge=deployment_id,
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
                    not passed_item or final_results.get(task_id) is True
                    for task_id, passed_item in candidate_results.items()
                )
            )
            report_valid, _report_reason = hooks.validate_model_report(
                final_behavior,
                expected_model=personal_model,
                challenge=deployment_id,
            )
            final_ok = final_ok and report_valid
        except Exception as exc:
            final_behavior = {
                "model": personal_model,
                "score": 0,
                "total": len(candidate_results),
                "error": f"{type(exc).__name__}: {bounded_error(exc)}",
            }
            final_ok = False
        post_eval_digest_verified = False
        if final_ok:
            try:
                post_eval_digest_verified = (
                    hooks.local_model_digest(personal_model) == candidate_digest
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
                if restored and policy_restored
                else (
                    "Final behavior evaluation failed and automatic recovery needs attention; "
                    f"preserved recovery alias: {previous_alias or '(none)'}."
                )
            )

        try:
            advance_journal("verified")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Verified alias recovery journal update failed: {exc}"

        try:
            if hooks.local_model_digest(personal_model) != candidate_digest:
                raise ValueError("published alias digest changed before policy activation")
            desired_models = dict(prior_models)
            desired_models.update({"code": personal_model, "general": personal_model})
            policy = _policy_update(
                local_models=desired_models,
                source="behavior-validated personal QLoRA deployment",
                expected_revision=policy_revision,
                transition_token=policy_token,
            )
            policy_revision = int(policy["revision"])
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
                    if restored and policy_restored
                    else f"Recovery needs attention; preserved alias: {previous_alias or '(none)'} ."
                )
            )

        old_recovery = str(state.get("previous_personal_model") or "")
        if old_recovery and not (
            old_recovery.startswith("sonder-personal-previous:")
            and _MODEL_RE.fullmatch(old_recovery)
        ):
            old_recovery = ""
        journal["old_recovery_alias"] = old_recovery
        try:
            hooks.write_deployment_journal(journal)
        except (OSError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, f"Deployment recovery snapshot update failed: {exc}"
        state.update(
            status="deployed",
            deployed_ts=int(time.time()),
            model=personal_model,
            previous_personal_model=previous_alias,
            policy_revision=policy["revision"],
            last_deployment_eval={
                "path": str(report_path),
                "sha256": receipt_sha256,
                "deployment_id": deployment_id,
                "suite_version": behavior.get("suite_version"),
                "base_score": behavior.get("base", {}).get("score"),
                "candidate_score": behavior.get("candidate", {}).get("score"),
                "total": behavior.get("candidate", {}).get("total"),
            },
        )
        try:
            hooks.write_state(state)
        except (OSError, TypeError, ValueError) as exc:
            restored = restore_previous_alias()
            policy_restored = recover_policy(restored)
            finalize_failed_recovery(restored, policy_restored)
            return False, (
                f"Deployment state commit failed: {bounded_error(exc)}. "
                + (
                    "The previous personal alias and policy were restored."
                    if restored and policy_restored
                    else f"Recovery needs attention; preserved alias: {previous_alias or '(none)'} ."
                )
            )
        policy_mutated = False
        try:
            hooks.write_shared_alias_owner(
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
            run([ollama_bin, "rm", old_recovery], capture_output=True, text=True, timeout=30)
            old_probe = run(
                [ollama_bin, "show", old_recovery],
                capture_output=True, text=True, timeout=30,
            )
            if model_show_status(old_probe, old_recovery) != "missing":
                try:
                    old_cleanup_pending = hooks.record_pending_model_cleanup(old_recovery)
                except (OSError, TypeError, ValueError):
                    return False, (
                        "Deployment committed, but old recovery-alias cleanup "
                        "ownership failed; the transition marker was preserved."
                    )
        if not hooks.clear_deployment_journal(deployment_id):
            return False, (
                "Deployment committed, but transition cleanup remains pending; "
                "the next lifecycle command will finalize it."
            )
        if old_cleanup_pending:
            return False, (
                "Deployment committed; old recovery-alias cleanup remains pending."
            )
        return True, (
            f"Behavior-validated and deployed {hooks.list_value('PERSONAL_MODEL')}; "
            f"{hooks.list_value('ROLLBACK_MODEL')} remains available for rollback."
        )
    finally:
        if candidate_created:
            run([ollama_bin, "rm", candidate], capture_output=True, text=True, timeout=30)
            candidate_probe = run(
                [ollama_bin, "show", candidate],
                capture_output=True, text=True, timeout=30,
            )
            if model_show_status(candidate_probe, candidate) == "missing":
                with contextlib.suppress(OSError, TypeError, ValueError):
                    hooks.forget_pending_model_cleanup(candidate)
            else:
                with contextlib.suppress(OSError, TypeError, ValueError):
                    hooks.record_pending_model_cleanup(candidate)
        shutil.rmtree(staging, ignore_errors=True)


def rollback_locked(
    *,
    ollama: str = "",
    runner: RunnerFn = subprocess.run,
    hooks: RollbackHooks,
) -> tuple[bool, str]:
    """Roll code and general tiers back to the verified rollback model."""

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return hooks.run_external(runner, command, ollama_command=True, **kwargs)

    ollama_bin = ollama_executable(ollama)
    rollback_model = str(hooks.list_value("ROLLBACK_MODEL"))
    probe = run(
        [ollama_bin, "show", rollback_model],
        capture_output=True, text=True, timeout=30,
    )
    rollback_identity = show_identity(probe)
    if model_show_status(probe, rollback_model) != "exists" or not rollback_identity:
        return False, (
            f"Rollback blocked: {rollback_model} could not be verified; "
            "runtime policy was not changed."
        )
    try:
        rollback_digest = hooks.local_model_digest(rollback_model)
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Rollback blocked: exact model digest could not be verified: {exc}"
    transition_id = "rollback-%s-%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    policy_token = secrets.token_urlsafe(32)
    journal: dict[str, Any] = {
        "schema": 1,
        "operation": "rollback",
        **_training_catalog.record(),
        "deployment_id": transition_id,
        "state_path": str(hooks.state_path()),
        "phase": "reserved",
        "created_ts": int(time.time()),
        "policy_token": policy_token,
        "rollback_identity": rollback_identity,
        "rollback_digest": rollback_digest,
    }
    reserved = False
    try:
        before, journal = _reserve_transition(journal)
        reserved = True
        reserved_probe = run(
            [ollama_bin, "show", rollback_model],
            capture_output=True, text=True, timeout=30,
        )
        if (
            model_show_status(reserved_probe, rollback_model) != "exists"
            or show_identity(reserved_probe) != rollback_identity
            or hooks.local_model_digest(rollback_model) != rollback_digest
        ):
            raise RuntimeError("rollback model changed after transition reservation")
        policy = _policy_update(
            local_models={"code": rollback_model, "general": rollback_model},
            source="training rollback",
            expected_revision=before["revision"],
            transition_token=policy_token,
        )
        journal.update(
            phase="policy_updated",
            last_policy_revision=policy["revision"],
            updated_ts=int(time.time()),
        )
        hooks.write_deployment_journal(journal)
        committed_probe = run(
            [ollama_bin, "show", rollback_model],
            capture_output=True, text=True, timeout=30,
        )
        if (
            model_show_status(committed_probe, rollback_model) != "exists"
            or show_identity(committed_probe) != rollback_identity
            or hooks.local_model_digest(rollback_model) != rollback_digest
        ):
            raise RuntimeError("rollback model changed before state commit")
        state = hooks.read_state()
        state.update(
            status="rolled_back",
            rollback_ts=int(time.time()),
            policy_revision=policy["revision"],
            last_policy_transition={
                "id": transition_id,
                "operation": "rollback",
                "model_digest": rollback_digest,
            },
        )
        hooks.write_state(state)
        journal.update(phase="committed", updated_ts=int(time.time()))
        hooks.write_deployment_journal(journal)
        if not hooks.clear_deployment_journal(transition_id):
            return False, (
                "Rollback committed, but transition cleanup remains pending; "
                "the next lifecycle command will finalize it."
            )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        if not reserved:
            return False, f"Rollback blocked: {bounded_error(exc)}."
        restored, _detail = hooks.reconcile_pending_deployment(
            ollama=ollama_bin, runner=runner,
        )
        suffix = "policy was restored" if restored else "policy recovery needs attention"
        return False, f"Rollback failed: {bounded_error(exc)}; {suffix}."
    return True, (
        f"Rolled code/general back to {rollback_model}. Personal models and "
        "checkpoints were not deleted."
    )


def adopt_legacy_personal(
    *,
    confirmed: bool = False,
    ollama: str = "",
    runner: RunnerFn = subprocess.run,
    hooks: AliasOwnershipHooks,
) -> tuple[bool, str]:
    """Explicitly bind one pre-ownership personal alias to the current policy."""
    if not confirmed:
        return False, (
            "Legacy personal-alias adoption was not confirmed. Re-run with "
            "`training adopt-legacy --confirm` after verifying the active policy."
        )
    try:
        with hooks.deployment_lock():
            ready, detail = hooks.prepare_shared_alias_lifecycle()
            if not ready:
                return False, f"Legacy adoption blocked: {detail}"
            owner = hooks.read_shared_alias_record("owner")
            current_policy = str(runtime_policy.policy_path().resolve())
            if owner is not None:
                if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
                    current_policy
                ):
                    return False, "Legacy adoption blocked: alias is owned by another policy."
                return True, "The personal alias is already owned by the current policy."
            ollama_bin = ollama_executable(ollama)
            personal_model = str(hooks.list_value("PERSONAL_MODEL"))
            probe = hooks.run_external(
                runner,
                [ollama_bin, "show", personal_model],
                ollama_command=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = model_show_status(probe, personal_model)
            identity = show_identity(probe)
            if status != "exists" or not identity:
                reason = "is missing" if status == "missing" else "could not be verified"
                return False, f"Legacy adoption blocked: {personal_model} {reason}."
            try:
                digest = hooks.local_model_digest(personal_model)
            except (OSError, TypeError, ValueError) as exc:
                return False, f"Legacy adoption blocked: exact alias digest failed: {exc}"
            hooks.write_shared_alias_owner(
                deployment_id="legacy-adoption-" + uuid.uuid4().hex,
                identity=identity,
                digest=digest,
            )
            return True, (
                f"Adopted exact legacy {personal_model} identity for policy "
                f"{current_policy}. Future deployments will preserve it before promotion."
            )
    except RuntimeError as exc:
        return False, f"Legacy adoption blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Legacy adoption failed safely: {bounded_error(exc)}"


def release_personal_owner(
    *,
    confirmed: bool = False,
    hooks: AliasOwnershipHooks,
) -> tuple[bool, str]:
    """Release policy ownership only after routing no longer uses the alias."""
    if not confirmed:
        return False, (
            "Personal-alias ownership release was not confirmed. Roll back all "
            "personal routing, then re-run `training release-alias --confirm`."
        )
    try:
        with hooks.deployment_lock():
            ready, detail = hooks.prepare_shared_alias_lifecycle()
            if not ready:
                return False, f"Alias ownership release blocked: {detail}"
            owner = hooks.read_shared_alias_record("owner")
            if owner is None:
                return True, "The endpoint has no persistent personal-alias owner."
            current_policy_path = str(runtime_policy.policy_path().resolve())
            if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
                current_policy_path
            ):
                return False, "Alias ownership release blocked: another policy owns it."
            policy = _policy_load(create=True)
            if policy.get("error"):
                return False, "Alias ownership release blocked: runtime policy is unreadable."
            if _training_catalog.owner_is_routed(owner, policy["local_models"]):
                return False, (
                    "Alias ownership release blocked: roll every tier off "
                    f"{owner['model']} first."
                )
            Path(hooks.shared_alias_paths()["owner"]).unlink()
            return True, (
                "Released personal-alias ownership without deleting or rerouting the model. "
                "Another policy may now adopt it explicitly."
            )
    except RuntimeError as exc:
        return False, f"Alias ownership release blocked: {exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"Alias ownership release failed safely: {bounded_error(exc)}"
