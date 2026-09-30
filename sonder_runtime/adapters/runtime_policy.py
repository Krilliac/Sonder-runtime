"""Shared, hot-reloadable policy adapter for local models and execution lanes.

Every Sonder Runtime surface uses the same per-user file. The policy intentionally
cannot configure cloud models for local tiers, permissions, roots, credentials,
or cloud consent; ``provider_models`` only picks the model an already-bound,
already-consented hosted provider serves per tier.

The pure validation/normalization rules live in
``sonder_runtime.domain.runtime_policy.rules`` and the atomic-write/lock
primitives to ``sonder_runtime.adapters.filesystem.atomic_json``. This
The adapter owns the
policy file's location, locking discipline, and load/update workflows.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

from sonder_runtime.adapters.filesystem import atomic_json as _atomic_json
from sonder_runtime.domain.runtime_policy import rules as _rules
from sonder_runtime.platform import paths as runtime_paths

VERSION = _rules.VERSION
LOCAL_TIERS = _rules.LOCAL_TIERS
BASE_LOCAL_TIERS = _rules.BASE_LOCAL_TIERS
OPTIONAL_LOCAL_TIERS = _rules.OPTIONAL_LOCAL_TIERS
ROUTING_LANES = _rules.ROUTING_LANES
DEFAULT_MODELS = _rules.DEFAULT_MODELS
DEFAULT_EMBEDDING_MODEL = _rules.DEFAULT_EMBEDDING_MODEL
RESERVED_PERSONAL_MODEL = _rules.RESERVED_PERSONAL_MODEL
DEFAULT_ROUTING = _rules.DEFAULT_ROUTING
NPU_MODES = _rules.NPU_MODES
NPU_CAPABILITIES = _rules.NPU_CAPABILITIES
DEFAULT_NPU = _rules.DEFAULT_NPU
DEFAULT_LONG_CONTEXT_OVERFLOW = _rules.DEFAULT_LONG_CONTEXT_OVERFLOW
PROVIDER_MODEL_PROVIDERS = _rules.PROVIDER_MODEL_PROVIDERS
_MODEL_RE = _rules._MODEL_RE
_LOCK = threading.RLock()


@contextlib.contextmanager
def _policy_file_lock(timeout=10.0, path=None):
    """Serialize policy read/check/replace across independent processes."""
    policy = (policy_path() if path is None else Path(path)).resolve()
    try:
        with _atomic_json.file_lock(policy, timeout=timeout):
            yield
    except RuntimeError as exc:
        if "timed out" in str(exc):
            raise RuntimeError(
                "timed out waiting for runtime policy lock"
            ) from exc
        raise


def policy_path() -> Path:
    override = os.environ.get("SONDER_RUNTIME_POLICY", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(runtime_paths.state_path("runtime_policy.json"))


def transition_path(path=None) -> Path:
    """Return the deployment-transition marker unique to one policy file."""
    path = (policy_path() if path is None else Path(path)).resolve()
    return path.with_name(path.name + ".transition.json")


# Pure rules delegate to the SPEC-3 domain module; names are preserved for
# existing callers and tests.
_is_cloud_name = _rules.is_cloud_name
_is_reserved_personal_alias = _rules.is_reserved_personal_alias
_model = _rules.validate_model
_seed_model = _rules.seed_model
_seed_embedding_model = _rules.seed_embedding_model
_normalize_npu = _rules.normalize_npu
normalize = _rules.normalize
bound_tiers = _rules.bound_tiers
_disk_payload = _rules.disk_payload
_write_json_atomic = _atomic_json.write_json_atomic


def default_policy(env=None) -> dict:
    return _rules.default_policy(os.environ if env is None else env)


def long_context_overflow(policy=None, env=None) -> dict:
    """Overflow settings in force: the policy section plus env overrides."""
    policy = load(create=False) if policy is None else policy
    return _rules.effective_long_context_overflow(
        policy, os.environ if env is None else env,
    )


def npu_mode(capability, policy=None) -> str:
    """Effective accelerator mode for one capability; unknown means off."""
    policy = load(create=False) if policy is None else policy
    return _rules.npu_mode(capability, policy)


def _write(policy: dict, path=None) -> Path:
    path = policy_path() if path is None else Path(path)
    return _write_json_atomic(path, _disk_payload(policy))


def _load_unlocked(path, create=True) -> dict:
    """Read one policy path while the caller owns any required locks."""
    path = Path(path)
    if not path.exists():
        policy = default_policy()
        if create:
            _write(policy, path)
        return {**policy, "path": str(path), "error": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        policy = normalize(raw)
        error = ""
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        policy = default_policy(env={})
        error = "%s: %s" % (type(exc).__name__, exc)
    return {**policy, "path": str(path), "error": error}


def load(create=True) -> dict:
    path = policy_path().resolve()
    with _LOCK:
        if create and not path.exists():
            # Recheck under the process-shared lock so simultaneous first loads
            # all observe the one policy that actually won creation.
            with _policy_file_lock(path=path):
                return _load_unlocked(path, create=True)
        return _load_unlocked(path, create=False)


def reserve_transition(payload) -> tuple[dict, dict]:
    """Atomically reserve one policy's model-deployment transition."""
    if not isinstance(payload, dict):
        raise ValueError("deployment transition payload must be a JSON object")
    path = policy_path().resolve()
    marker = transition_path(path)
    with _LOCK, _policy_file_lock(path=path):
        if marker.exists():
            raise RuntimeError("runtime policy already has an active model deployment")
        current = _load_unlocked(path, create=True)
        if current.get("error"):
            raise ValueError(
                "runtime policy is invalid; deployment transition was not reserved: %s"
                % current["error"]
            )
        revision = int(current.get("revision") or 0)
        journal = {
            **payload,
            "policy_path": str(path.resolve()),
            "prior_models": dict(current["local_models"]),
            "prior_policy_revision": revision,
            "last_policy_revision": revision,
        }
        _write_json_atomic(marker, journal)
        return current, journal


def finish_transition(transition_id, token) -> bool:
    """Remove only the exact transition marker owned by the caller."""
    if not isinstance(transition_id, str) or not transition_id:
        raise ValueError("transition_id must be a non-empty string")
    if not isinstance(token, str) or not token:
        raise ValueError("transition token must be a non-empty string")
    path = policy_path().resolve()
    marker = transition_path(path)
    with _LOCK, _policy_file_lock(path=path):
        if not marker.exists():
            raise RuntimeError("runtime policy has no active model deployment")
        try:
            journal = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("runtime policy deployment transition is unreadable") from exc
        if not isinstance(journal, dict):
            raise RuntimeError("runtime policy deployment transition is invalid")
        recorded_id = journal.get("transition_id") or journal.get("deployment_id")
        recorded_token = journal.get("policy_token")
        recorded_path = journal.get("policy_path")
        expected_path = str(path.resolve())
        if not isinstance(recorded_id, str) or not hmac.compare_digest(
            recorded_id, transition_id
        ):
            raise RuntimeError("runtime policy deployment transition id does not match")
        if not isinstance(recorded_token, str) or not hmac.compare_digest(
            recorded_token, token
        ):
            raise RuntimeError("runtime policy deployment transition token does not match")
        if not isinstance(recorded_path, str) or not hmac.compare_digest(
            recorded_path, expected_path
        ):
            raise RuntimeError("runtime policy deployment transition belongs to another policy")
        marker.unlink()
        return True


def update(
    local_models=None, embedding_model=None, routing=None, npu=None, reset=False,
    source="user update", expected_revision=None, transition_token=None,
    long_context_overflow=None, provider_models=None,
) -> dict:
    """Apply one guarded policy change under the cross-process lock.

    ``provider_models`` is ``{provider: {tier: model}}`` for a hosted
    provider's per-tier model choice (an empty model clears that tier).  It
    never binds a tier to the provider or enables cloud; see
    ``domain.runtime_policy.rules``.
    """
    path = policy_path().resolve()
    with _LOCK, _policy_file_lock(path=path):
        journal_path = transition_path(path)
        transition_authorized = False
        if journal_path.exists():
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("runtime policy is blocked by an unreadable deployment transition") from exc
            journal_policy = str(journal.get("policy_path") or "") if isinstance(journal, dict) else ""
            current_policy = str(path.resolve())
            if not journal_policy or os.path.normcase(journal_policy) != os.path.normcase(current_policy):
                raise RuntimeError("runtime policy update blocked by deployment for another policy")
            expected_token = str(journal.get("policy_token") or "") if isinstance(journal, dict) else ""
            supplied = str(transition_token or "")
            if not expected_token or not hmac.compare_digest(expected_token, supplied):
                raise RuntimeError("runtime policy update blocked by active model deployment")
            transition_authorized = True
        current = _load_unlocked(path, create=True)
        if current.get("error") and not reset:
            raise ValueError(
                "runtime policy is invalid; use reset before updating: %s"
                % current["error"]
            )
        if expected_revision is not None:
            try:
                expected_revision_value = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_revision must be an integer") from exc
            if int(current.get("revision") or 0) != expected_revision_value:
                raise RuntimeError(
                    "runtime policy changed concurrently: expected revision %s, found %s"
                    % (expected_revision, current.get("revision", 0))
                )
        base = default_policy(env={}) if reset else current
        candidate = {
            **base,
            "local_models": dict(base["local_models"]),
            "routing": dict(base["routing"]),
            "npu": dict(base.get("npu") or DEFAULT_NPU),
            "long_context_overflow": dict(
                base.get("long_context_overflow") or DEFAULT_LONG_CONTEXT_OVERFLOW
            ),
            "provider_models": {
                name: dict(tiers)
                for name, tiers in (base.get("provider_models") or {}).items()
            },
        }
        if provider_models:
            if not isinstance(provider_models, dict):
                raise ValueError("provider_models update must be a JSON object")
            for name, tiers in provider_models.items():
                if not isinstance(tiers, dict):
                    raise ValueError("provider_models.%s update must be a JSON object" % name)
                merged = candidate["provider_models"].setdefault(str(name).strip().lower(), {})
                merged.update(tiers)
            candidate["provider_models"] = _rules.normalize_provider_models(
                candidate["provider_models"],
            )
        if embedding_model is not None:
            candidate["embedding_model"] = embedding_model
        if local_models:
            if not isinstance(local_models, dict):
                raise ValueError("local_models update must be a JSON object")
            unknown = set(local_models) - set(LOCAL_TIERS)
            if unknown:
                raise ValueError("unknown local tier(s): %s" % ", ".join(sorted(unknown)))
            if (
                any(
                    _is_reserved_personal_alias(value)
                    for value in local_models.values()
                )
                and not transition_authorized
            ):
                raise ValueError(
                    "sonder-personal:latest is reserved for an active validated deployment"
                )
            candidate["local_models"].update(local_models)
        if routing:
            if not isinstance(routing, dict):
                raise ValueError("routing update must be a JSON object")
            unknown = set(routing) - set(ROUTING_LANES)
            if unknown:
                raise ValueError("unknown routing lane(s): %s" % ", ".join(sorted(unknown)))
            candidate["routing"].update(routing)
        if npu:
            if not isinstance(npu, dict):
                raise ValueError("npu update must be a JSON object")
            unknown = set(npu) - ({"mode"} | set(NPU_CAPABILITIES))
            if unknown:
                raise ValueError(
                    "unknown npu key(s): %s" % ", ".join(sorted(unknown))
                )
            candidate["npu"] = _normalize_npu(
                {**candidate["npu"], **npu}, candidate["npu"],
            )
        if long_context_overflow:
            if not isinstance(long_context_overflow, dict):
                raise ValueError("long_context_overflow update must be a JSON object")
            candidate["long_context_overflow"] = _rules.normalize_long_context_overflow(
                {**candidate["long_context_overflow"], **long_context_overflow},
            )
        candidate["revision"] = int(current.get("revision") or 0) + 1
        candidate["updated_ts"] = int(time.time())
        candidate["source"] = str(source or "user update")[:120]
        normalized = normalize(candidate, defaults=default_policy(env={}))
        _write(normalized, path)
        return _load_unlocked(path, create=False)


def route_tier(lane: str, policy=None, fallback="code") -> str:
    lane = str(lane or "").strip().lower()
    policy = load(create=True) if policy is None else policy
    tier = str((policy.get("routing") or {}).get(lane) or fallback).strip().lower()
    return tier if tier in BASE_LOCAL_TIERS else fallback


def format_policy(policy=None) -> str:
    policy = load(create=True) if policy is None else policy
    lines = [
        "Sonder Runtime local model policy",
        "  path: %s" % policy.get("path", policy_path()),
        "  revision: %s | source: %s" % (
            policy.get("revision", 0), policy.get("source", ""),
        ),
    ]
    if policy.get("error"):
        lines.append("  ERROR: %s (safe defaults active)" % policy["error"])
    lines.append("  local models:")
    for tier in LOCAL_TIERS:
        model = policy["local_models"].get(tier) or ""
        if not model:
            lines.append(
                "    %s: (unset - capability routing falls back to a base tier)"
                % tier
            )
            continue
        lines.append("    %s: %s" % (tier, model))
    lines.append("  embeddings: %s" % policy.get("embedding_model", DEFAULT_EMBEDDING_MODEL))
    lines.append("  execution lanes:")
    for lane in ROUTING_LANES:
        tier = policy["routing"][lane]
        lines.append("    %s: %s -> %s" % (
            lane, tier, policy["local_models"][tier],
        ))
    npu = policy.get("npu") or DEFAULT_NPU
    lines.append(
        "  npu accelerator: mode=%s (routing=%s, embeddings=%s)"
        % (
            npu.get("mode", "off"),
            npu.get("routing") or "-",
            npu.get("embeddings") or "-",
        )
    )
    lines.append("  " + format_long_context_overflow(policy))
    for provider, tiers in sorted((policy.get("provider_models") or {}).items()):
        lines.append("  %s tier models (used only where that provider is bound): %s" % (
            provider, ", ".join("%s=%s" % item for item in tiers.items()) or "-",
        ))
    lines.append("  cloud tiers remain separate explicit opt-in configuration")
    return "\n".join(lines)


def format_long_context_overflow(policy=None, env=None) -> str:
    """One status line for the long-context overflow settings in force."""
    policy = load(create=True) if policy is None else policy
    settings = long_context_overflow(policy, env)
    model = settings["model"] or (
        (policy.get("local_models") or {}).get("reasoning") or ""
    )
    model_text = (
        "%s (reasoning tier)" % model if model and not settings["model"]
        else model or "(unset: bind a reasoning tier or set a model)"
    )
    line = "long-context overflow: %s (threshold %d tokens -> %s on the %s pool)" % (
        "on" if settings["enabled"] else "off",
        settings["threshold_tokens"], model_text, settings["provider"],
    )
    if settings["overrides"]:
        line += " [environment overrides: %s]" % ", ".join(settings["overrides"])
    if settings["error"]:
        line += " [ignored invalid environment: %s]" % settings["error"]
    return line


OVERFLOW_USAGE = (
    "usage: /runtime overflow [status|on|off|threshold <tokens>|model <local-model>]\n"
    "  on/off          switch a turn whose estimated prompt exceeds the threshold\n"
    "                  from its local tier to the overflow model on the Ollama pool\n"
    "  threshold <n>   estimated prompt tokens that trigger it (at least %d)\n"
    "  model <name>    the overflow model; 'model' alone uses the reasoning tier's\n"
    "Cloud models are never accepted. SONDER_LONG_CONTEXT_OVERFLOW, "
    "SONDER_LONG_CONTEXT_THRESHOLD and SONDER_LONG_CONTEXT_MODEL override the policy."
    % _rules.OVERFLOW_MIN_THRESHOLD
)


def overflow_command(text: str) -> str:
    """``/runtime overflow on|off|status|threshold <tokens>|model <name>``."""
    words = str(text or "").split()
    action = words[0].lower() if words else "status"
    if action in ("status", "show") and len(words) <= 1:
        return format_long_context_overflow()
    if action == "on" and len(words) == 1:
        change = {"enabled": True}
    elif action == "off" and len(words) == 1:
        change = {"enabled": False}
    elif action == "threshold" and len(words) == 2:
        change = {"threshold_tokens": words[1]}
    elif action == "model" and len(words) <= 2:
        change = {"model": words[1] if len(words) == 2 else ""}
    else:
        return OVERFLOW_USAGE
    try:
        policy = update(long_context_overflow=change, source="/runtime overflow")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return "long-context overflow not changed: %s" % exc
    return format_long_context_overflow(policy)
