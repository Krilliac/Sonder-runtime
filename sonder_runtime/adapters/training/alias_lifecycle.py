"""Shared personal-alias lifecycle, pending cleanup ledger, and crash reconciliation."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import socket
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, cast

import sonder_runtime.adapters.runtime_policy as runtime_policy
import sonder_runtime.adapters.training_model_catalog as _training_catalog
from sonder_runtime.adapters.process_liveness import process_identity
from sonder_runtime.adapters.training.artifacts import (
    RunnerFn,
    bounded_error,
    json_object,
    model_show_status,
    ollama_executable,
    path_has_control_chars,
    run_external,
    show_identity,
    write_json_atomic,
)


_policy_file_lock = cast(
    Callable[..., ContextManager[Any]],
    runtime_policy._policy_file_lock,
)
_finish_transition = cast(
    Callable[[str, str], bool],
    runtime_policy.finish_transition,
)
_policy_load = cast(
    Callable[..., dict[str, Any]],
    runtime_policy.load,
)
_policy_update = cast(
    Callable[..., dict[str, Any]],
    runtime_policy.update,
)
_MODEL_RE = runtime_policy._MODEL_RE
_LOCAL_TIERS: Sequence[str] = cast(Sequence[str], runtime_policy.LOCAL_TIERS)
_OPTIONAL_LOCAL_TIERS: Sequence[str] = cast(
    Sequence[str], runtime_policy.OPTIONAL_LOCAL_TIERS
)


def shared_alias_paths(
    *, local_ollama_origin_fn: Callable[[], str]
) -> dict[str, Any]:
    """Stable per-user namespace for one Ollama endpoint's global aliases."""
    transport_origin = local_ollama_origin_fn()
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


def read_shared_alias_record(
    kind: str,
    *,
    shared_alias_paths_fn: Callable[[], Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Read and validate one shared-alias JSON record (``owner`` or ``transition``)."""
    paths = shared_alias_paths_fn()
    path = Path(paths[kind])
    if not path.exists():
        return None
    record = json_object(path, f"shared alias {kind}")
    if record.get("schema") != 1 or record.get("origin") != paths["origin"]:
        raise ValueError(f"shared alias {kind} is invalid")
    return record


def clear_shared_alias_transition(
    deployment_id: str,
    policy_token: str,
    *,
    shared_alias_paths_fn: Callable[[], Mapping[str, Any]],
    read_record_fn: Callable[[str], dict[str, Any] | None],
) -> bool:
    """Remove the shared-alias transition marker when ownership matches."""
    paths = shared_alias_paths_fn()
    record = read_record_fn("transition")
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
    Path(paths["transition"]).unlink()
    return True


def claim_shared_alias_transition(
    deployment_id: str,
    policy_token: str,
    *,
    shared_alias_paths_fn: Callable[[], Mapping[str, Any]],
    read_record_fn: Callable[[str], dict[str, Any] | None],
    deployment_journal_path_fn: Callable[[], Path],
) -> dict[str, Any]:
    """Claim the cross-policy shared-alias transition file atomically."""
    paths = shared_alias_paths_fn()
    transition_path = Path(paths["transition"])
    if transition_path.exists():
        record = read_record_fn("transition") or {}
        raise RuntimeError(
            "another policy owns an unfinished personal-alias transition; "
            "created_ts=%s policy_path=%s; recover using that policy's training rollback"
            % (record.get("created_ts"), record.get("policy_path"))
        )
    payload: dict[str, Any] = {
        "schema": 1,
        "origin": paths["origin"],
        "deployment_id": deployment_id,
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "journal_path": str(deployment_journal_path_fn()),
        "policy_token": policy_token,
        "phase": "claiming",
        "created_ts": int(time.time()),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "process_identity": process_identity(os.getpid()),
    }
    write_json_atomic(transition_path, payload)
    return payload


def advance_shared_alias_transition(
    payload: Mapping[str, Any],
    phase: str,
    *,
    shared_alias_paths_fn: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Advance and persist the shared-alias transition phase."""
    updated = dict(payload)
    updated.update(phase=phase, updated_ts=int(time.time()))
    write_json_atomic(Path(shared_alias_paths_fn()["transition"]), updated)
    return updated


def prepare_shared_alias_lifecycle(
    *,
    require_owner: bool = False,
    read_record_fn: Callable[[str], dict[str, Any] | None],
    deployment_journal_path_fn: Callable[[], Path],
    clear_transition_fn: Callable[[str, str], bool],
) -> tuple[bool, str]:
    """Block cross-policy recovery and clear only a pre-reservation orphan."""
    owner = read_record_fn("owner")
    owner_is_foreign = owner is not None and os.path.normcase(
        str(owner.get("policy_path") or "")
    ) != os.path.normcase(str(runtime_policy.policy_path().resolve()))
    if require_owner and owner_is_foreign:
        return False, "sonder-personal:latest is owned by another runtime policy"
    record = read_record_fn("transition")
    if record is None:
        local_journal = deployment_journal_path_fn()
        if owner_is_foreign and local_journal.exists():
            try:
                pending = json_object(local_journal, "deployment journal")
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
        return False, (
            "unfinished personal-alias transition belongs to another runtime policy; "
            "created_ts=%s policy_path=%s; recover using that policy's training rollback"
            % (record.get("created_ts"), record.get("policy_path"))
        )
    journal_path = Path(str(record.get("journal_path") or ""))
    if journal_path == deployment_journal_path_fn() and journal_path.exists():
        return True, ""
    if record.get("phase") in {"claiming", "clearing"} and not journal_path.exists():
        try:
            if clear_transition_fn(
                str(record.get("deployment_id") or ""),
                str(record.get("policy_token") or ""),
            ):
                return True, "orphaned alias claim cleared"
        except (OSError, TypeError, ValueError):
            pass
    return False, "personal-alias transition recovery evidence is incomplete"


def validate_shared_alias_owner(
    *,
    prior_policy: Any,
    personal_status: str,
    personal_identity: str,
    personal_digest: str,
    state: Any,
    read_record_fn: Callable[[str], dict[str, Any] | None],
    personal_model: str,
) -> tuple[bool, Any]:
    """Verify persistent ownership and exact identity of ``sonder-personal:latest``."""
    del prior_policy, state
    owner = read_record_fn("owner")
    current_policy = str(runtime_policy.policy_path().resolve())
    if owner is not None:
        if os.path.normcase(str(owner.get("policy_path") or "")) != os.path.normcase(
            current_policy
        ):
            return False, "sonder-personal:latest is owned by another runtime policy"
        if (
            personal_status != "exists"
            or owner.get("model") != personal_model
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


def write_shared_alias_owner(
    *,
    deployment_id: str,
    identity: str,
    digest: str,
    personal_model: str,
    shared_alias_paths_fn: Callable[[], Mapping[str, Any]],
) -> None:
    """Persist the verified shared personal-alias ownership record."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(identity or "")):
        raise ValueError("shared alias owner identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
        raise ValueError("shared alias owner digest is invalid")
    paths = shared_alias_paths_fn()
    write_json_atomic(
        Path(paths["owner"]),
        {
            "schema": 1,
            "origin": paths["origin"],
            "model": personal_model,
            "policy_path": str(runtime_policy.policy_path().resolve()),
            "deployment_id": deployment_id,
            "identity": identity,
            "digest": digest,
            "updated_ts": int(time.time()),
        },
    )


def cleanup_pending_path() -> Path:
    """Return the path to the policy-bound pending model cleanup ledger."""
    policy = runtime_policy.policy_path().resolve()
    return policy.with_name(policy.name + ".model-cleanup.json")


def validated_pending_cleanup_models(payload: Any) -> list[str]:
    """Validate and extract the bounded alias cleanup list from a JSON payload."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or not isinstance(models, list)
        or len(models) > 32
    ):
        raise ValueError("pending model cleanup is invalid")
    if any(
        not isinstance(model, str)
        or len(model) > 160
        or not model.startswith(("sonder-personal-candidate:", "sonder-personal-previous:"))
        or not _MODEL_RE.fullmatch(model)
        for model in models
    ):
        raise ValueError("pending model cleanup contains unsafe aliases")
    return list(models)


def record_pending_model_cleanup(
    model: str,
    *,
    cleanup_pending_path_fn: Callable[[], Path],
) -> bool:
    """Add a temporary candidate/previous alias to the pending cleanup ledger."""
    alias = str(model)
    if (
        len(alias) > 160
        or not alias.startswith(("sonder-personal-candidate:", "sonder-personal-previous:"))
        or not _MODEL_RE.fullmatch(alias)
    ):
        return False
    path = cleanup_pending_path_fn()
    with _policy_file_lock():
        models: list[str] = []
        if path.exists():
            payload = json_object(path, "pending model cleanup")
            models = validated_pending_cleanup_models(payload)
        models = sorted(set(models) | {alias})
        if len(models) > 32:
            raise ValueError("pending model cleanup is full")
        write_json_atomic(
            path, {"schema": 1, "models": models, "updated_ts": int(time.time())}
        )
    return True


def forget_pending_model_cleanup(
    model: str,
    *,
    cleanup_pending_path_fn: Callable[[], Path],
) -> bool:
    """Release cleanup ownership only after an alias is proved absent."""
    path = cleanup_pending_path_fn()
    with _policy_file_lock():
        if not path.exists():
            return True
        payload = json_object(path, "pending model cleanup")
        models = validated_pending_cleanup_models(payload)
        remaining = sorted(
            {item for item in models if isinstance(item, str) and item != model}
        )
        if remaining:
            write_json_atomic(
                path,
                {"schema": 1, "models": remaining, "updated_ts": int(time.time())},
            )
        else:
            path.unlink()
    return True


def reconcile_pending_model_cleanup(
    *,
    ollama: str = "",
    runner: RunnerFn = subprocess.run,
    cleanup_pending_path_fn: Callable[[], Path],
    run_external_fn: Callable[..., subprocess.CompletedProcess[Any]] = run_external,
) -> tuple[bool, str]:
    """Delete and verify removal of all aliases recorded in the cleanup ledger."""
    path = cleanup_pending_path_fn()
    try:
        with _policy_file_lock():
            if not path.exists():
                return True, ""
            payload = json_object(path, "pending model cleanup")
    except ValueError as exc:
        return False, f"pending model cleanup is unreadable: {bounded_error(exc)}"
    try:
        models = validated_pending_cleanup_models(payload)
    except ValueError as exc:
        return False, str(exc)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return run_external_fn(runner, command, ollama_command=True, **kwargs)

    ollama_bin = ollama_executable(ollama)
    removed: list[str] = []
    for model in models:
        run([ollama_bin, "rm", model], capture_output=True, text=True, timeout=30)
        probe = run([ollama_bin, "show", model], capture_output=True, text=True, timeout=30)
        if model_show_status(probe, model) == "missing":
            removed.append(model)
    try:
        with _policy_file_lock():
            current = (
                json_object(path, "pending model cleanup")
                if path.exists()
                else {"schema": 1, "models": []}
            )
            current_models = validated_pending_cleanup_models(current)
            remaining = sorted({
                item
                for item in current_models
                if isinstance(item, str) and item not in set(removed)
            })
            if remaining:
                write_json_atomic(
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


def write_deployment_journal(
    payload: Mapping[str, Any],
    *,
    deployment_journal_path_fn: Callable[[], Path],
) -> None:
    """Persist a catalog-normalized deployment or rollback journal."""
    write_json_atomic(
        deployment_journal_path_fn(), _training_catalog.journal(payload)
    )


def clear_deployment_journal(
    deployment_id: str,
    *,
    deployment_journal_path_fn: Callable[[], Path],
    read_record_fn: Callable[[str], dict[str, Any] | None],
    advance_transition_fn: Callable[[Mapping[str, Any], str], dict[str, Any]],
    clear_transition_fn: Callable[[str, str], bool],
) -> bool:
    """Finish the policy transition and clear the shared-alias marker."""
    path = deployment_journal_path_fn()
    try:
        current = json_object(path, "deployment journal")
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
            transition = read_record_fn("transition")
            if transition is None:
                return False
            advance_transition_fn(transition, "clearing")
        finished = _finish_transition(
            deployment_id, str(current.get("policy_token") or "")
        )
        if not finished:
            return False
        if shared and not clear_transition_fn(
            deployment_id, str(current.get("policy_token") or "")
        ):
            return False
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ReconcileHooks:
    """Injectable host callbacks for :func:`reconcile_pending_deployment`."""

    deployment_journal_path: Callable[[], Path]
    read_shared_alias_record: Callable[[str], dict[str, Any] | None]
    write_shared_alias_owner: Callable[..., None]
    write_deployment_journal: Callable[[Mapping[str, Any]], None]
    clear_deployment_journal: Callable[[str], bool]
    record_pending_model_cleanup: Callable[[str], bool]
    forget_pending_model_cleanup: Callable[[str], bool]
    local_model_digest: Callable[[str], str]
    run_external: Callable[..., subprocess.CompletedProcess[Any]]


def reconcile_pending_deployment(
    *,
    ollama: str = "",
    runner: RunnerFn = subprocess.run,
    hooks: ReconcileHooks,
) -> tuple[bool, str]:
    """Recover a policy-bound deployment or rollback transition."""
    path = hooks.deployment_journal_path()
    if not path.exists():
        return True, ""
    try:
        journal = json_object(path, "deployment journal")
        personal_model, rollback_model = _training_catalog.aliases(journal)
    except ValueError as exc:
        return False, f"pending deployment journal is unreadable: {bounded_error(exc)}"
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
        or path_has_control_chars(journal[key])
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
        or set(prior_models_value) != set(_LOCAL_TIERS)
        or any(
            not isinstance(model, str)
            or not (
                (model == "" and tier in _OPTIONAL_LOCAL_TIERS)
                or _MODEL_RE.fullmatch(model)
            )
            for tier, model in prior_models_value.items()
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
    if os.path.normcase(str(journal_policy_path)) != os.path.normcase(
        str(current_policy_path)
    ):
        return False, "pending deployment belongs to a different runtime policy"
    if operation == "deploy" and journal.get("shared_alias_transition") is True:
        try:
            shared_transition = hooks.read_shared_alias_record("transition")
            shared_owner = hooks.read_shared_alias_record("owner")
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
        recorded_state: dict[str, Any] = json_object(
            journal["state_path"], "training state"
        )
    except ValueError:
        recorded_state = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return hooks.run_external(runner, command, ollama_command=True, **kwargs)

    ollama_bin = ollama_executable(ollama)

    def remove_or_handoff(models: Sequence[str]) -> tuple[bool, list[str]]:
        failed: list[str] = []
        for model in models:
            if not model:
                continue
            run([ollama_bin, "rm", model], capture_output=True, text=True, timeout=30)
            probe = run(
                [ollama_bin, "show", model], capture_output=True, text=True, timeout=30,
            )
            if model_show_status(probe, model) == "missing":
                with contextlib.suppress(OSError, TypeError, ValueError):
                    hooks.forget_pending_model_cleanup(model)
                continue
            try:
                if not hooks.record_pending_model_cleanup(model):
                    return False, [model]
            except (OSError, TypeError, ValueError):
                return False, [model]
            failed.append(model)
        return True, failed

    prior_models = dict(prior_models_value)
    current = _policy_load(create=True)

    if operation == "rollback":
        rollback_identity = str(journal.get("rollback_identity") or "")
        rollback_digest = str(journal.get("rollback_digest") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", rollback_identity)
            or not re.fullmatch(r"[0-9a-f]{64}", rollback_digest)
        ):
            return False, "pending rollback journal has invalid model identity"
        witness = recorded_state.get("last_policy_transition")
        committed = (
            isinstance(witness, dict)
            and witness.get("id") == deployment_id
            and witness.get("operation") == "rollback"
            and witness.get("model_digest") == rollback_digest
            and recorded_state.get("policy_revision") == current.get("revision")
        )
        if committed:
            rollback_probe = run(
                [ollama_bin, "show", rollback_model],
                capture_output=True, text=True, timeout=30,
            )
            try:
                digest_matches = (
                    hooks.local_model_digest(rollback_model) == rollback_digest
                )
            except (OSError, TypeError, ValueError):
                digest_matches = False
            if (
                current.get("error")
                or current.get("revision") != journal["last_policy_revision"]
                or current.get("local_models", {}).get("code") != rollback_model
                or current.get("local_models", {}).get("general") != rollback_model
                or model_show_status(rollback_probe, rollback_model) != "exists"
                or show_identity(rollback_probe) != rollback_identity
                or not digest_matches
            ):
                return False, "committed rollback policy could not be verified"
            if not hooks.clear_deployment_journal(deployment_id):
                return False, "committed rollback journal cleanup failed"
            return True, "committed rollback finalized"

        policy_restored = current.get("local_models") == prior_models
        if not policy_restored and not current.get("error"):
            current_revision = int(current.get("revision") or 0)
            target_matches = (
                current.get("source") == "training rollback"
                and current.get("local_models", {}).get("code") == rollback_model
                and current.get("local_models", {}).get("general") == rollback_model
                and current_revision in {
                    int(journal["last_policy_revision"]),
                    int(journal["prior_policy_revision"]) + 1,
                }
            )
            if target_matches:
                try:
                    restored = _policy_update(
                        local_models=prior_models,
                        source="interrupted rollback policy restore",
                        expected_revision=current_revision,
                        transition_token=journal["policy_token"],
                    )
                    journal["last_policy_revision"] = restored["revision"]
                    hooks.write_deployment_journal(journal)
                    policy_restored = True
                except (OSError, RuntimeError, TypeError, ValueError):
                    policy_restored = False
        if not policy_restored:
            return False, "interrupted rollback policy recovery needs attention"
        if not hooks.clear_deployment_journal(deployment_id):
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
        or (
            previous_alias
            and previous_alias != f"sonder-personal-previous:{deployment_id}"
        )
    ):
        return False, "pending deployment journal contains unsafe model identities"

    committed_id = (recorded_state.get("last_deployment_eval") or {}).get(
        "deployment_id"
    )
    if committed_id == deployment_id:
        personal_probe = run(
            [ollama_bin, "show", personal_model],
            capture_output=True, text=True, timeout=30,
        )
        try:
            committed_digest_matches = (
                hooks.local_model_digest(personal_model) == candidate_digest
            )
        except (OSError, TypeError, ValueError):
            committed_digest_matches = False
        committed_policy_matches = (
            not current.get("error")
            and recorded_state.get("policy_revision") == current.get("revision")
            and current.get("local_models", {}).get("code") == personal_model
            and current.get("local_models", {}).get("general") == personal_model
        )
        committed_alias_matches = (
            model_show_status(personal_probe, personal_model) == "exists"
            and show_identity(personal_probe) == candidate_identity
            and committed_digest_matches
        )
        if not committed_policy_matches or not committed_alias_matches:
            if not current.get("error"):
                safe_models = {
                    tier: rollback_model if model == personal_model else model
                    for tier, model in current["local_models"].items()
                }
                safe_models.update({"code": rollback_model, "general": rollback_model})
                if safe_models != current["local_models"]:
                    try:
                        safe_policy = _policy_update(
                            local_models=safe_models,
                            source="committed deployment recovery fail-safe",
                            expected_revision=current["revision"],
                            transition_token=journal["policy_token"],
                        )
                        journal["last_policy_revision"] = safe_policy["revision"]
                        hooks.write_deployment_journal(journal)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        pass
            return False, "committed deployment identity recovery needs attention"
        if journal.get("shared_alias_transition") is True:
            try:
                hooks.write_shared_alias_owner(
                    deployment_id=deployment_id,
                    identity=candidate_identity,
                    digest=candidate_digest,
                    personal_model=personal_model,
                )
            except (OSError, TypeError, ValueError):
                return (
                    False,
                    "committed deployment alias ownership could not be recorded",
                )
        old_recovery = str(journal.get("old_recovery_alias") or "")
        if old_recovery and (
            not old_recovery.startswith("sonder-personal-previous:")
            or not _MODEL_RE.fullmatch(old_recovery)
        ):
            return False, "completed deployment journal has unsafe cleanup identity"
        owned, failed = remove_or_handoff([candidate, old_recovery])
        if not owned:
            return False, "completed deployment candidate cleanup ownership failed"
        if not hooks.clear_deployment_journal(deployment_id):
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
        [ollama_bin, "show", personal_model],
        capture_output=True, text=True, timeout=30,
    )
    current_status = model_show_status(current_personal, personal_model)
    current_identity = (
        show_identity(current_personal) if current_status == "exists" else ""
    )
    if current_status == "exists":
        try:
            current_digest = hooks.local_model_digest(personal_model)
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
                [ollama_bin, "show", previous_alias],
                capture_output=True, text=True, timeout=30,
            )
            try:
                backup_digest_matches = (
                    not previous_digest
                    or hooks.local_model_digest(previous_alias) == previous_digest
                )
            except (OSError, TypeError, ValueError):
                backup_digest_matches = False
            if (
                model_show_status(backup_probe, previous_alias) == "exists"
                and show_identity(backup_probe) == previous_identity
                and backup_digest_matches
            ):
                copied = run(
                    [ollama_bin, "cp", previous_alias, personal_model],
                    capture_output=True, text=True, timeout=30,
                )
                verified = (
                    run(
                        [ollama_bin, "show", personal_model],
                        capture_output=True, text=True, timeout=30,
                    )
                    if copied.returncode == 0
                    else copied
                )
                try:
                    restored_digest_matches = (
                        not previous_digest
                        or hooks.local_model_digest(personal_model) == previous_digest
                    )
                except (OSError, TypeError, ValueError):
                    restored_digest_matches = False
                alias_restored = (
                    copied.returncode == 0
                    and model_show_status(verified, personal_model) == "exists"
                    and show_identity(verified) == previous_identity
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
            run(
                [ollama_bin, "rm", personal_model],
                capture_output=True, text=True, timeout=30,
            )
            verified = run(
                [ollama_bin, "show", personal_model],
                capture_output=True, text=True, timeout=30,
            )
            alias_restored = model_show_status(verified, personal_model) == "missing"

    allowed_revisions = {
        int(journal.get("prior_policy_revision") or 0),
        int(journal.get("last_policy_revision") or 0),
    }
    target_models = (
        prior_models
        if alias_restored
        else {
            **{
                tier: rollback_model if model == personal_model else model
                for tier, model in prior_models.items()
            },
            "code": rollback_model,
            "general": rollback_model,
        }
    )
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
                _policy_update(
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
    if not hooks.clear_deployment_journal(deployment_id):
        return False, "interrupted deployment recovered but journal cleanup failed"
    if failed:
        return False, "interrupted deployment recovered; alias cleanup remains pending"
    return True, "interrupted deployment recovered"
