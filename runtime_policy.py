"""Shared, hot-reloadable policy for local models and execution lanes.

Every Sonder Runtime surface uses the same per-user file. The policy intentionally
cannot configure cloud models, permissions, roots, or credentials.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import sonder_paths


VERSION = 1
LOCAL_TIERS = ("fast", "code", "general")
ROUTING_LANES = ("router", "workbench", "autopilot", "fleet", "review")
DEFAULT_MODELS = {
    "fast": "qwen2.5:3b",
    "code": "sonder:latest",
    "general": "sonder:latest",
}
RESERVED_PERSONAL_MODEL = "sonder-personal:latest"
DEFAULT_ROUTING = {
    "router": "fast",
    "workbench": "code",
    "autopilot": "code",
    "fleet": "code",
    "review": "code",
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_LOCK = threading.RLock()


@contextlib.contextmanager
def _policy_file_lock(timeout=10.0, path=None):
    """Serialize policy read/check/replace across independent processes."""
    policy = (policy_path() if path is None else Path(path)).resolve()
    lock_path = policy.with_name(policy.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for runtime policy lock") from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def policy_path() -> Path:
    override = os.environ.get("SONDER_RUNTIME_POLICY", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(sonder_paths.state_path("runtime_policy.json"))


def transition_path(path=None) -> Path:
    """Return the deployment-transition marker unique to one policy file."""
    path = (policy_path() if path is None else Path(path)).resolve()
    return path.with_name(path.name + ".transition.json")


def _is_cloud_name(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return "-cloud" in lowered or lowered.endswith(":cloud")


def _is_reserved_personal_alias(value) -> bool:
    model = str(value or "").strip().casefold()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    if ":" not in model:
        model += ":latest"
    return model == RESERVED_PERSONAL_MODEL.casefold()


def _model(value, fallback: str) -> str:
    model = str(value or fallback).strip()
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("invalid local model name %r" % model)
    if _is_cloud_name(model):
        raise ValueError("runtime policy local tiers cannot reference cloud models")
    if _is_reserved_personal_alias(model):
        return RESERVED_PERSONAL_MODEL
    return model


def _seed_model(env, tier: str) -> str:
    configured = str(env.get("SONDER_%s" % tier.upper(), "") or "").strip()
    if tier == "code" and _is_cloud_name(configured):
        configured = str(env.get("SONDER_CODE_LOCAL", "") or "").strip()
    if _is_reserved_personal_alias(configured):
        configured = ""
    if configured and not _is_cloud_name(configured):
        return _model(configured, DEFAULT_MODELS[tier])
    return DEFAULT_MODELS[tier]


def default_policy(env=None) -> dict:
    env = os.environ if env is None else env
    return {
        "version": VERSION,
        "revision": 0,
        "local_models": {
            tier: _seed_model(env, tier) for tier in LOCAL_TIERS
        },
        "routing": dict(DEFAULT_ROUTING),
        "updated_ts": 0,
        "source": "environment seed",
    }


def normalize(payload, defaults=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("runtime policy must be a JSON object")
    # Environment variables seed a file only when it is first created. Once a
    # shared policy exists, normalization and recovery use stable built-ins so
    # separately launched surfaces cannot drift with their inherited env.
    base = default_policy(env={}) if defaults is None else defaults
    raw_models = payload.get("local_models") or {}
    raw_routing = payload.get("routing") or {}
    if not isinstance(raw_models, dict) or not isinstance(raw_routing, dict):
        raise ValueError("runtime policy local_models and routing must be objects")
    local_models = {
        tier: _model(raw_models.get(tier), base["local_models"][tier])
        for tier in LOCAL_TIERS
    }
    routing = {}
    for lane in ROUTING_LANES:
        tier = str(raw_routing.get(lane) or base["routing"][lane]).strip().lower()
        if tier not in LOCAL_TIERS:
            raise ValueError(
                "runtime routing lane %s must use: %s"
                % (lane, ", ".join(LOCAL_TIERS))
            )
        routing[lane] = tier
    return {
        "version": VERSION,
        "revision": max(0, int(payload.get("revision") or 0)),
        "local_models": local_models,
        "routing": routing,
        "updated_ts": max(0, int(payload.get("updated_ts") or 0)),
        "source": str(payload.get("source") or "runtime policy")[:120],
    }


def _disk_payload(policy: dict) -> dict:
    return {key: policy[key] for key in (
        "version", "revision", "local_models", "routing", "updated_ts", "source"
    )}


def _write_json_atomic(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


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
    local_models=None, routing=None, reset=False, source="user update",
    expected_revision=None, transition_token=None,
) -> dict:
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
        }
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
    return tier if tier in LOCAL_TIERS else fallback


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
        lines.append("    %s: %s" % (tier, policy["local_models"][tier]))
    lines.append("  execution lanes:")
    for lane in ROUTING_LANES:
        tier = policy["routing"][lane]
        lines.append("    %s: %s -> %s" % (
            lane, tier, policy["local_models"][tier],
        ))
    lines.append("  cloud tiers remain separate explicit opt-in configuration")
    return "\n".join(lines)
