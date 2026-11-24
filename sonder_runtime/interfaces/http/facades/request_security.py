"""HTTP bind security, auth/role gating, idempotency, session LRU, and wire framing.

Extracted from ``sonder_runtime/interfaces/http/serve.py``.  Imports only
``sonder_runtime.application``, ``sonder_runtime.interfaces``, and non-IO
standard-library modules so SPEC-3 boundaries hold strictly while ``serve.py``
retains every monkeypatch seam via the injected ``serve_mod`` namespace.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult
from ..chat_tool_stream import (
    is_local_open_context as _is_local_open_context,
    request_mode_scope as _request_mode_scope,
)
from ..host_policy import (
    HOST_NOT_ALLOWED_REMEDY,
    HOST_TRUSTED,
    host_decision,
    normalize_allowed_host,
    parse_host_header,
    reflectable_origin,
)
from .config_wiring import (
    ConversationState,
    DuplicateJsonObjectKey,
    HTTPRequestError,
    LOCAL_LOG_PAGE,
    reject_duplicate_json_object_key,
    reject_nonfinite_json_number,
)

MAX_IDEMPOTENCY_KEY_LENGTH = 512

_REJECTED_HOST_LOG_INTERVAL_SECONDS = 60.0
_REJECTED_HOST_LOG_MAX_NAMES_PER_INTERVAL = 8
_REJECTED_HOST_LOG: OrderedDict[str, float] = OrderedDict()
_REJECTED_HOST_LOG_LOCK = threading.Lock()

SCOPED_TASK_TOOLS = frozenset((
    "task_create", "task_list", "task_update", "task_show", "task_delete",
    "task_plan", "task_progress", "task_ledger", "task_depend", "checklist_create",
    "checklist_show", "checklist_update",
))

ACCOUNT_UNSCOPED_TASK_TOOLS = frozenset((
    "master_orchestrate", "master_retry",
    "agent_retry", "self_heal_repair",
    "mission_start", "mission_status", "composition_bindings",
))

ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS = frozenset((
    "workflow_list", "workflow_save", "workflow_delete", "workflow_run",
))

ACCOUNT_UNSCOPED_LOOP_ACTIONS = frozenset((
    "checklist_create", "checklist_update", "checklist_show",
    "work", "agent", "workbench_agent", "master", "master_orchestrate",
    "master_retry", "agent_retry", "self_heal_repair",
    "mission_start", "mission_status", "goal",
))

DEVELOPER_AUTHORITY_BY_MODE: dict[str, str] = {
    "local-open": "every caller (unauthenticated)",
    "api-key": "the owner API key, or a developer/admin account",
    "account": "an authenticated account, and it must hold developer or admin",
    "both": "the owner API key AND an account holding developer or admin",
    "either": "the owner API key, or an account holding developer or admin",
}

BODY_GUARDED_INVENTORY_ROUTES = (
    "/v1/compute/nodes",
    "/v1/compute/nodes/refresh",
    "/v1/tools/inventory",
    "/v1/tools/inventory/refresh",
)


def env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default*."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean flag environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def resolve_auth_mode(
    api_key: str = "",
    require_account: bool = False,
    configured: str | None = None,
    *,
    logger: Any,
) -> str:
    """Resolve the effective HTTP auth mode from explicit env/config or defaults."""
    resolved_cfg = (
        os.environ.get("SONDER_AUTH_MODE", "")
        if configured is None
        else configured
    )
    mode = (resolved_cfg or "").strip().lower().replace("_", "-")
    if mode:
        if mode not in ("api-key", "account", "both", "either"):
            logger.critical(
                f"SONDER_AUTH_MODE has invalid value={resolved_cfg!r}, cannot start server"
            )
            raise RuntimeError("invalid SONDER_AUTH_MODE")
        logger.info(f"Auth mode resolved, mode={mode!r} (explicit)")
        logger.debug(f"_resolve_auth_mode: explicit mode={mode!r}")
        return mode
    if api_key:
        logger.debug("_resolve_auth_mode: inferred api-key mode from API key presence")
        return "api-key"
    if require_account:
        logger.debug("_resolve_auth_mode: inferred account mode from require_account")
        return "account"
    logger.debug("_resolve_auth_mode: defaulting to local-open")
    return "local-open"


def parse_cors_origins(value: str | None) -> frozenset[str]:
    """Parse comma-separated exact CORS origins, ignoring wildcards."""
    return frozenset(
        origin.strip()
        for origin in (value or "").split(",")
        if origin.strip() and origin.strip() != "*"
    )


def parse_allowed_hosts(
    values: Iterable[str], *, logger: Any,
) -> tuple[tuple[str, int | None], ...]:
    """Validated public Host names; malformed entries are dropped, not widened."""
    parsed: list[tuple[str, int | None]] = []
    for value in values:
        try:
            parsed.append(normalize_allowed_host(value))
        except ValueError:
            logger.warning("ignoring malformed allowed host entry")
    return tuple(dict.fromkeys(parsed))


def log_rejected_host(
    value: str | None,
    *,
    logger: Any,
    log_map: OrderedDict[str, float] | None = None,
    lock: threading.Lock | None = None,
) -> None:
    """Warn about a refused Host name, at most once a minute per name."""
    parsed = parse_host_header(value) if value is not None else None
    name = parsed[0] if parsed is not None else "(malformed or repeated Host header)"
    now = time.monotonic()
    entries = _REJECTED_HOST_LOG if log_map is None else log_map
    guard = _REJECTED_HOST_LOG_LOCK if lock is None else lock
    with guard:
        last = entries.get(name)
        if last is not None and now - last < _REJECTED_HOST_LOG_INTERVAL_SECONDS:
            return
        recent = sum(
            1
            for stamp in entries.values()
            if now - stamp < _REJECTED_HOST_LOG_INTERVAL_SECONDS
        )
        if recent >= _REJECTED_HOST_LOG_MAX_NAMES_PER_INTERVAL:
            return
        entries[name] = now
        entries.move_to_end(name)
        while len(entries) > 64:
            entries.popitem(last=False)
    logger.warning(
        f"request refused (421 HOST_NOT_ALLOWED): Host {name!r} is not an address, "
        "a loopback or machine name, or listed in [server].allowed_hosts, and this "
        "listener runs without credentials"
    )


def account_identity(account: Any) -> str:
    """Return one bounded identity or ``""`` for malformed auth output."""
    if not isinstance(account, dict):
        return ""
    identity = account.get("username") or account.get("id") or ""
    if not isinstance(identity, str):
        return ""
    identity = identity.strip()
    if (
        not identity
        or len(identity) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in identity)
    ):
        return ""
    return identity


def state_principal(context: Any) -> str:
    """Derive the canonical principal string for session/receipt/cache isolation."""
    ctx = context or {}
    account = ctx.get("account") or {}
    if account:
        identity = account_identity(account)
        if not identity:
            raise PermissionError("authenticated account identity is unavailable")
        return "account:%s" % identity
    if ctx.get("api_key"):
        return "api-key"
    return "local-open"


def build_principal(context: Any) -> str:
    """The build tools' principal for an authenticated request, ``""`` if malformed."""
    account = (context or {}).get("account")
    if account is None:
        return "owner"
    identity = account_identity(account)
    if not identity:
        return ""
    return "account:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def request_idempotency_key(
    context: Any,
    endpoint: Any,
    supplied_key: Any,
    *,
    principal_of: Callable[[Any], str],
) -> str:
    """Return an opaque idempotency key bound to one HTTP principal."""
    key = str(supplied_key or "").strip()
    if not key:
        return ""
    material = "http-idempotency\0%s\0%s\0%s" % (
        str(endpoint or ""),
        principal_of(context),
        key,
    )
    return "hi-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def http_action_idempotency_key(
    context: Any,
    supplied_key: Any,
    action: Any,
    *,
    principal_of: Callable[[Any], str],
) -> str:
    """Return a principal- and action-bound replay key for HTTP work controls."""
    key = str(supplied_key or "").strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        return ""
    material = "\0".join((
        "served-action-idempotency-v1",
        principal_of(context),
        key,
        str(action or ""),
    ))
    return "act-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def receipt_owner_scope(
    context: Any, *, principal_of: Callable[[Any], str],
) -> str:
    """Opaque per-principal budget key for durable action receipts."""
    material = "served-receipt-owner\0" + principal_of(context)
    return "rw-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdempotencyRefusal(str):
    """A replay-guard refusal: still readable chat text, never a success."""

    code: str
    status: int
    retryable: bool

    def __new__(
        cls,
        text: str,
        *,
        code: str,
        status: int,
        retryable: bool = False,
    ) -> IdempotencyRefusal:
        value = super().__new__(cls, text)
        value.code = code
        value.status = status
        value.retryable = retryable
        return value


def idempotency_refusal_payload(refusal: IdempotencyRefusal) -> dict[str, Any]:
    """Serialize an :class:`IdempotencyRefusal` to the HTTP error envelope."""
    return {
        "error": {
            "message": str(refusal),
            "type": (
                "rate_limit_error"
                if refusal.status == 429
                else ("server_error" if refusal.status >= 500 else "invalid_request")
            ),
            "code": refusal.code,
            "retryable": refusal.retryable,
        }
    }


def send_idempotency_refusal(handler: Any, result: Any) -> bool:
    """Answer a replay-guard refusal with its status; ``False`` otherwise."""
    if not isinstance(result, IdempotencyRefusal):
        return False
    handler._send_json_payload(
        idempotency_refusal_payload(result),
        status=result.status,
        headers={"Retry-After": "1"} if result.retryable else None,
    )
    return True


def http_action_binding(
    context: Any,
    supplied_key: Any,
    action: Any,
    *,
    principal_of: Callable[[Any], str],
) -> tuple[str, str]:
    """Opaque ``(binding_key, action_digest)`` tying one client key to one request."""
    key = str(supplied_key or "").strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        return "", ""
    binding = "\0".join(("served-action-binding-v1", principal_of(context), key))
    return (
        "bind-" + hashlib.sha256(binding.encode("utf-8")).hexdigest(),
        hashlib.sha256(
            ("served-action-request-v1\0" + str(action or "")).encode("utf-8")
        ).hexdigest(),
    )


def idempotent_http_action(
    context: Any,
    supplied_key: Any,
    action: Any,
    factory: Callable[[], Any],
    *,
    serve_mod: Any,
    receipts_store: Any,
    lifecycle_get: Callable[[], Any],
    storage_errors: tuple[type[BaseException], ...],
) -> Any:
    """Run an opt-in action once, preserving uncertainty across restarts."""
    cache_key = serve_mod._http_action_idempotency_key(context, supplied_key, action)
    if not cache_key:
        return factory()
    binding_key, action_digest = serve_mod._http_action_binding(
        context, supplied_key, action,
    )
    logger = serve_mod._serve_logger
    claim_errors: tuple[type[BaseException], ...] = (*storage_errors, ValueError)

    def durable_factory() -> Any:
        try:
            state = receipts_store.claim(
                cache_key,
                owner_scope=serve_mod._receipt_owner_scope(context),
                binding_key=binding_key,
                action_digest=action_digest,
            )
        except claim_errors:
            logger.error(
                f"idempotency receipt store unavailable for cache_key={cache_key!r}, refusing action",
                exc_info=True,
            )
            return IdempotencyRefusal(
                "idempotency receipt unavailable: the action was not started. "
                "Retry after restoring local runtime storage.",
                code="IDEMPOTENCY_RECEIPT_UNAVAILABLE",
                status=503,
                retryable=True,
            )
        if state == receipts_store.REJECTED:
            logger.warning(
                f"idempotency receipt capacity exhausted for cache_key={cache_key!r}"
            )
            return IdempotencyRefusal(
                "idempotency receipt capacity exhausted: the action was not "
                "started. Reuse the Idempotency-Key of the retried action, or "
                "retry after older completed receipts expire.",
                code="IDEMPOTENCY_CAPACITY_EXHAUSTED",
                status=429,
                retryable=True,
            )
        if state == receipts_store.CONFLICT:
            logger.warning(
                f"idempotency key reused for a different request, cache_key={cache_key!r}"
            )
            return IdempotencyRefusal(
                "idempotency key reused: this Idempotency-Key already names a "
                "different request, so this one was not started. Use a new "
                "Idempotency-Key for a new action.",
                code="IDEMPOTENCY_KEY_REUSED",
                status=422,
            )
        if state == "completed":
            return IdempotencyRefusal(
                "idempotent action refused: it already completed before the "
                "current server process. It was not run again; query its "
                "status or submit a new action with a new Idempotency-Key.",
                code="IDEMPOTENT_ACTION_COMPLETED",
                status=409,
            )
        if state in {"started", "uncertain"}:
            logger.warning(
                f"idempotent action refused with uncertain prior outcome, "
                f"cache_key={cache_key!r}, state={state!r}"
            )
            return IdempotencyRefusal(
                "idempotent action refused: it has an uncertain prior outcome "
                "after an interrupted server process. It was not run again; "
                "inspect the affected project/status before submitting a new action.",
                code="IDEMPOTENT_ACTION_UNCERTAIN",
                status=409,
            )
        try:
            result = factory()
        except BaseException:
            logger.error(
                f"idempotent action raised after admission, marking uncertain, cache_key={cache_key!r}",
                exc_info=True,
            )
            with contextlib.suppress(*storage_errors):
                receipts_store.finish(cache_key, uncertain=True)
            raise
        try:
            receipts_store.finish(
                cache_key,
                uncertain=isinstance(result, ChatWorkResult) and result.status == "running",
            )
        except storage_errors:
            logger.error(
                f"idempotency receipt finish failed for cache_key={cache_key!r}, receipt left as started",
                exc_info=True,
            )
        return result

    return lifecycle_get().idempotent(
        cache_key,
        durable_factory,
        cache_ttl_seconds=receipts_store.completed_ttl_seconds(),
        cache_result=lambda result: not (
            isinstance(result, IdempotencyRefusal)
            and result.code in ("IDEMPOTENCY_CAPACITY_EXHAUSTED", "IDEMPOTENCY_KEY_REUSED")
        ),
    )


def task_account_scope(
    context: Any, *, principal_of: Callable[[Any], str],
) -> str | None:
    """Return an opaque durable task boundary for an authenticated account."""
    account = (context or {}).get("account") or {}
    if not account:
        return None
    material = "task-account-scope\0" + principal_of(context)
    return "ta-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def account_global_memory_refusal(
    tool: Any,
    context: Any,
    *,
    global_memory_tools: frozenset[str],
) -> str:
    """Refuse legacy global-memory tools for a hosted account."""
    if not (context or {}).get("account"):
        return ""
    if str(tool or "").lstrip("/") not in global_memory_tools:
        return ""
    return (
        "hosted account memory is isolated to chat sessions and projects; "
        "this global memory tool is local-operator only"
    )


def account_task_boundary_refusal(
    tool_name: Any,
    kwargs: Any,
    context: Any,
    *,
    task_scope_of: Callable[[Any], str | None],
) -> str:
    """Reject account requests whose internal task writes cannot be scoped."""
    if task_scope_of(context) is None:
        return ""
    name = str(tool_name or "").strip().lower()
    if name in ACCOUNT_UNSCOPED_TASK_TOOLS:
        return (
            "refused /%s: account-scoped task state is not available for "
            "this autonomous tool; use the scoped task/checklist commands."
        ) % name
    if name in ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS:
        return (
            "refused /%s: account-scoped saved workflows are not available; "
            "use an operator-managed local workflow session."
        ) % name
    if name != "loop":
        return ""
    try:
        parsed = json.loads((kwargs or {}).get("actions_json", ""))
    except (TypeError, ValueError):
        return ""
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or action.get("action") or "").strip().lower()
        if action_type in ACCOUNT_UNSCOPED_LOOP_ACTIONS:
            return (
                "refused /loop: account-scoped task state is not available "
                "for loop action '%s'; use scoped task/checklist commands."
            ) % action_type
        if action_type == "sonder":
            raw_prompt = str(action.get("prompt") or "").strip()
            if (
                not raw_prompt.startswith("/")
                or str(action.get("tier") or "").strip()
                or str(action.get("model_override") or "").strip()
            ):
                return (
                    "refused /loop: hosted account chat must use /v1/chat/completions "
                    "rather than the local operator sonder loop action."
                )
    return ""


def fanout_request_owner(
    context: Any, *, principal_of: Callable[[Any], str],
) -> str:
    """Stable, non-secret receipt owner key for durable fanout state."""
    material = "fanout-owner\0" + principal_of(context)
    return "fo-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def reasoning_request_owner(
    context: Any,
    *,
    authenticates_callers: Callable[[], bool],
    principal_of: Callable[[Any], str],
) -> str:
    """Opaque principal bound to private reasoning captured for this request."""
    if not authenticates_callers():
        return ""
    material = "reasoning-owner\0" + principal_of(context)
    return "ro-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def admission_request_owner(
    context: Any,
    *,
    authenticates_callers: Callable[[], bool],
    principal_of: Callable[[Any], str],
) -> str:
    """Opaque principal for per-owner admission fairness accounting."""
    if not authenticates_callers() or context.get("mode") == "api-key":
        return ""
    material = "admission-owner\0" + principal_of(context)
    return "ao-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def request_cache_scope(
    context: Any, *, principal_of: Callable[[Any], str],
) -> str:
    """Opaque request-owner scope for the deterministic request cache."""
    material = "request-cache-owner\0" + principal_of(context)
    return "qc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def feed_request_owner(
    context: Any,
    *,
    authenticates_callers: Callable[[], bool],
    principal_of: Callable[[Any], str],
) -> str:
    """Opaque principal that scopes the live execution feed to one caller."""
    if not authenticates_callers():
        return ""
    material = "live-feed-owner\0" + principal_of(context)
    return "lf-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def fanout_request_role(context: Any) -> str:
    """Role label persisted alongside a fanout receipt."""
    account = context.get("account") or {}
    role = str(account.get("role") or "").strip()
    if role:
        return role
    return "api-key" if context.get("api_key") else "local-open"


def prune_http_session_states(
    states: OrderedDict[tuple[str, str], ConversationState],
    max_size: int,
) -> None:
    """Evict least-recent idle states until ``len(states) <= max_size``."""
    for key, candidate in list(states.items()):
        if len(states) <= max_size:
            break
        if candidate.session_pins == 0 and candidate.lock.acquire(blocking=False):
            try:
                states.pop(key, None)
            finally:
                candidate.lock.release()


def evict_owner_lru_session_state(
    states: OrderedDict[tuple[str, str], ConversationState],
    principal: str,
) -> bool:
    """Drop *principal*'s least-recent idle state; False if all are in-flight."""
    for key, candidate in list(states.items()):
        if key[0] != principal:
            continue
        if candidate.session_pins == 0 and candidate.lock.acquire(blocking=False):
            try:
                states.pop(key, None)
            finally:
                candidate.lock.release()
            return True
    return False


def acquire_http_conversation_state(
    serve_mod: Any,
    context: Any,
    session: Any,
    token: str = "",
    *,
    pin: bool = False,
) -> tuple[ConversationState, bool]:
    """Atomically select a bounded state and optionally pin it for a request."""
    session_key = (session or "").strip()
    if not session_key:
        return (
            ConversationState(token=token or "", account=context.get("account")),
            False,
        )
    principal = serve_mod._state_principal(context)
    key = (principal, session_key)
    states: OrderedDict[tuple[str, str], ConversationState] = serve_mod._HTTP_SESSION_STATES
    with serve_mod._HTTP_SESSION_STATES_LOCK:
        state = states.get(key)
        if state is None:
            owner_limit = int(serve_mod.HTTP_SESSION_STATE_OWNER_LIMIT)
            global_limit = int(serve_mod.HTTP_SESSION_STATE_LIMIT)
            if context.get("account"):
                owned = sum(1 for existing in states if existing[0] == principal)
                while owned >= owner_limit:
                    if not evict_owner_lru_session_state(states, principal):
                        serve_mod._serve_logger.warning(
                            f"session state owner limit reached: all {owned} sessions "
                            "for principal are in-flight, falling back to request-local state"
                        )
                        return (
                            ConversationState(
                                token=token or "", account=context.get("account")
                            ),
                            False,
                        )
                    owned -= 1
            prune_http_session_states(states, global_limit - 1)
            if len(states) >= global_limit:
                serve_mod._serve_logger.warning(
                    f"global session state limit reached: {len(states)}/{global_limit} "
                    "sessions all in-flight, falling back to request-local state"
                )
                return (
                    ConversationState(
                        token=token or "", account=context.get("account")
                    ),
                    False,
                )
            state = ConversationState()
            states[key] = state
        states.move_to_end(key)
        if token:
            state.token = token
        if context.get("account"):
            state.account = context["account"]
        if pin:
            state.session_pins += 1
        return state, bool(pin)


def release_http_conversation_state(
    lock: Any, state: ConversationState, pinned: bool,
) -> None:
    """Release a request's eviction pin after its state-dependent work ends."""
    if not pinned:
        return
    with lock:
        state.session_pins = max(0, int(state.session_pins or 0) - 1)


def bearer_token(auth_header: str | None) -> str:
    """Strip an optional ``Bearer `` prefix from an authorization header."""
    header = auth_header or ""
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return header.strip()


def check_auth(auth_header: str | None, api_key: str) -> bool:
    """Constant-time API-key check; True when *api_key* is empty."""
    if not api_key:
        return True
    token = bearer_token(auth_header)
    return hmac.compare_digest(token.encode("utf-8"), api_key.encode("utf-8"))


def request_account_token(
    context: Any, auth_header: str = "", account_header: str = "",
) -> str:
    """Extract the request's account session token when an account is bound."""
    if not context.get("account"):
        return ""
    source = account_header or ("" if context.get("api_key") else auth_header)
    return bearer_token(source)


def http_scope_value(value: Any, label: str) -> str:
    """Validate an optional session/project string scope."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPRequestError(400, "%s must be a string" % label)
    stripped = value.strip()
    if len(stripped) > 256:
        raise HTTPRequestError(400, "%s is too long" % label)
    return stripped


def hosted_storage_id(
    context: Any,
    value: Any,
    kind: str,
    *,
    principal_of: Callable[[Any], str],
) -> str:
    """Namespace durable HTTP state by principal without exposing client IDs."""
    cleaned = http_scope_value(value, kind)
    if not cleaned or context.get("mode") == "local-open":
        return cleaned
    material = "%s\0%s\0%s" % (kind, principal_of(context), cleaned)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return "http-%s-%s" % (kind, digest)


def app_control_deployment_authorized(
    authorization: str | None,
    config: Any,
    *,
    previous_key_valid: Callable[[str], bool],
) -> bool:
    """Check deployment API-key authority against a live typed config snapshot."""
    required = config.server.auth_mode in ("api-key", "both") or bool(config.secrets.api_key)
    if not required:
        return True
    return bool(config.secrets.api_key) and (
        check_auth(authorization, config.secrets.api_key)
        or previous_key_valid(bearer_token(authorization))
    )


def effective_auth_mode(serve_mod: Any) -> str:
    """Compute effective auth mode from live ``serve`` module globals."""
    require_account = bool(serve_mod.REQUIRE_ACCOUNT)
    api_key = serve_mod.API_KEY
    auth_mode = str(serve_mod.AUTH_MODE)
    if require_account and not api_key and auth_mode in ("local-open", "api-key"):
        return "account"
    if auth_mode == "local-open":
        if api_key:
            return "api-key"
        if require_account:
            return "account"
    return auth_mode


def warn_if_local_open(serve_mod: Any) -> bool:
    """Warn about an unauthenticated listener from the *effective* mode only."""
    if effective_auth_mode(serve_mod) != "local-open":
        return False
    serve_mod._serve_logger.warning(
        "auth mode is local-open: no API key or account requirement configured; "
        "the loopback listener accepts unauthenticated local clients"
    )
    return True


def auth_context(
    serve_mod: Any,
    auth_header: str = "",
    account_header: str = "",
    *,
    previous_key_valid: Callable[[str], bool],
) -> dict[str, Any]:
    """Authenticate an HTTP request and return its normalized auth context."""
    mode = effective_auth_mode(serve_mod)
    api_key = serve_mod.API_KEY
    api_key_ok = bool(api_key) and (
        bool(serve_mod.check_auth(auth_header, api_key))
        or previous_key_valid(bearer_token(auth_header))
    )
    account_source = account_header or ("" if api_key_ok else auth_header)
    account = serve_mod._auth_account(account_source) if account_source else None
    if account is not None and not account_identity(account):
        account = None
    authorized = {
        "api-key": api_key_ok,
        "account": account is not None,
        "both": api_key_ok and account is not None,
        "either": api_key_ok or account is not None,
        "local-open": True,
    }[mode]
    serve_mod._serve_logger.debug(
        f"_auth_context: mode={mode!r}, authorized={authorized}, has_account={account is not None}"
    )
    return {
        "mode": mode,
        "authorized": authorized,
        "api_key": api_key_ok,
        "account": account,
    }


def developer_authorized(context: Mapping[str, Any], *, admin_auth: Any) -> bool:
    """Whether *context* holds developer or administrator authority."""
    if context.get("mode") == "local-open":
        return True
    if not context["authorized"]:
        return False
    account = context.get("account")
    role_ok = bool(admin_auth.has_developer_authority(account))
    if context["mode"] == "both":
        return role_ok
    return bool(context.get("api_key")) or role_ok


def admin_authorized(context: Mapping[str, Any], *, admin_auth: Any) -> bool:
    """Administrator authorization for privileged operations (SPEC-2)."""
    if context.get("mode") == "local-open":
        return True
    if not context.get("authorized"):
        return False
    account = context.get("account")
    if account is not None:
        ok, _ = admin_auth.require(account, "admin")
        return bool(ok)
    if context.get("mode") == "both":
        return False
    return bool(context.get("api_key"))


def restricted_tool_role(
    context: Any,
    *,
    developer_authorized_fn: Callable[[Any], bool],
    role_user: str,
) -> str | None:
    """``user`` when the role tool gate binds this caller, else ``None``."""
    if context is None or context.get("mode") == "local-open":
        return None
    try:
        unrestricted = developer_authorized_fn(context)
    except (KeyError, TypeError, AttributeError):
        unrestricted = False
    return None if unrestricted else role_user


def audit_http_approval(
    action: str,
    approval: Mapping[str, Any],
    started: float,
    *,
    runtime: Any,
    logger: Any,
) -> None:
    """Record an HTTP approve/deny/revoke on the existing direct-tool audit path."""
    record = getattr(runtime, "_record_direct_tool", None)
    if not callable(record):
        return
    try:
        if action == "revoke":
            record(
                "permission_approve",
                {"revoke": approval.get("nonce", "")},
                ok=True,
                started=started,
                summary="revoked %s call %s via http"
                % (approval.get("tool", ""), approval.get("call_id", "")),
            )
        elif action == "deny":
            record(
                "permission_approve",
                {
                    "deny": approval.get("call_id", ""),
                    "tool": approval.get("tool", ""),
                    "actor": approval.get("actor", ""),
                },
                ok=True,
                started=started,
                summary="denied %s call %s via http by %s"
                % (
                    approval.get("tool", ""),
                    approval.get("call_id", ""),
                    approval.get("actor", ""),
                ),
            )
        else:
            record(
                "permission_approve",
                {
                    "tool": approval.get("tool", ""),
                    "call_id": approval.get("call_id", ""),
                    "ttl_seconds": approval.get("ttl_seconds", 0),
                    "surface": "http",
                },
                ok=True,
                started=started,
                summary=approval.get("nonce", ""),
            )
    except Exception:
        logger.warning("approval audit record failed", exc_info=True)


def account_created_message(account: Any) -> str:
    """The human sentence a 201 registration carries next to the account."""
    acct = account if isinstance(account, dict) else {}
    username = str(acct.get("username") or "").strip() or "(unnamed)"
    role = str(acct.get("role") or "").strip() or "user"
    return "Account %s created (role %s)." % (username, role)


def system_operation_authority_error(
    operation: Any,
    context: Any,
    *,
    operation_roles: Mapping[str, str],
    admin_authorized_fn: Callable[[Any], bool],
    developer_authorized_fn: Callable[[Any], bool],
) -> str:
    """Return a role-boundary refusal, or ``""`` when this caller may proceed."""
    required = operation_roles.get(str(operation or ""))
    if required == "admin" and not admin_authorized_fn(context):
        return "administrator authorization is required"
    if required == "developer" and not developer_authorized_fn(context):
        return "developer or administrator authorization is required"
    return ""


def is_loopback_host(host: Any) -> bool:
    """True only for exact string loopback host names or IP literals."""
    if type(host) is not str:
        return False
    value = host.strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def keyless_loopback_auth_mode(
    auth_mode: str, api_key: str, host: Any, *, logger: Any,
) -> str:
    """``api-key`` with no key on a loopback bind runs as ``local-open``."""
    if auth_mode == "api-key" and not api_key and is_loopback_host(host):
        logger.warning(
            "api-key auth mode downgraded to local-open: no API key configured on loopback bind"
        )
        return "local-open"
    return auth_mode


def require_exact_bind_values(
    host: Any,
    api_key: Any,
    auth_mode: Any,
    auth_secret: Any,
    *,
    require_account: Any = None,
) -> None:
    """Reject non-builtin typed values before a bind-time security comparison."""
    if type(host) is not str:
        raise RuntimeError("bind host must be an exact builtin string")
    if type(api_key) is not str:
        raise RuntimeError("bind API key must be an exact builtin string")
    if type(auth_mode) is not str:
        raise RuntimeError("bind auth mode must be an exact builtin string")
    if type(auth_secret) is not str:
        raise RuntimeError("bind auth secret must be an exact builtin string")
    if require_account is not None and type(require_account) is not bool:
        raise RuntimeError("bind account requirement must be a boolean")


def a2a_discovery_base_url(serve_mod: Any) -> str:
    """Resolve the safe local A2A URL used by agent-card discovery."""
    configured = os.environ.get("SONDER_A2A_BASE_URL", "").strip()
    if configured:
        return configured
    if serve_mod._is_loopback_host(serve_mod.HOST):
        return "http://%s:%d" % (serve_mod.HOST, int(serve_mod.CONFIGURED_PORT))
    serve_mod._serve_logger.warning(
        f"A2A discovery base URL cannot be resolved: non-loopback host={serve_mod.HOST!r} "
        "and SONDER_A2A_BASE_URL is not set"
    )
    return ""


def selected_listener_port(
    default_port: int, config: Any = None, argv: list[str] | None = None,
) -> int:
    """Resolve the port the direct entrypoint will actually bind."""
    if config is not None:
        return int(config.server.port)
    port = default_port
    arguments = sys.argv if argv is None else argv
    if len(arguments) > 1:
        try:
            port = int(arguments[1])
        except (TypeError, ValueError):
            pass
    else:
        port = int(os.environ.get("SONDER_PORT", default_port))
    return port


def validate_bind_security(
    host: Any,
    *,
    api_key: Any = None,
    auth_mode: Any = None,
    auth_secret: Any = None,
    tls_terminated_by_proxy: Any = None,
    allow_non_loopback_proxy_backend: Any = None,
    min_api_key_length: int,
    min_account_secret_length: int,
    unsafe_lab_require_startup: Callable[..., Any],
    serve_mod: Any,
) -> None:
    """Enforce non-loopback TLS proxy and strong authentication invariants."""
    resolved_api_key = serve_mod.API_KEY if api_key is None else api_key
    resolved_secret = serve_mod.AUTH_SECRET if auth_secret is None else auth_secret
    raw_mode = serve_mod.AUTH_MODE if auth_mode is None else auth_mode
    require_exact_bind_values(
        host,
        resolved_api_key,
        raw_mode,
        resolved_secret,
        require_account=serve_mod.REQUIRE_ACCOUNT if auth_mode is None else None,
    )
    mode = effective_auth_mode(serve_mod) if auth_mode is None else raw_mode
    if mode not in ("api-key", "account", "both", "either", "local-open"):
        raise RuntimeError("invalid bind auth mode")
    if tls_terminated_by_proxy is None:
        tls_terminated_by_proxy = env_flag("SONDER_TLS_TERMINATED_BY_PROXY")
    elif type(tls_terminated_by_proxy) is not bool:
        raise RuntimeError("bind TLS proxy declaration must be a boolean")
    if allow_non_loopback_proxy_backend is None:
        allow_non_loopback_proxy_backend = env_flag(
            "SONDER_ALLOW_NON_LOOPBACK_PROXY_BACKEND"
        )
    elif type(allow_non_loopback_proxy_backend) is not bool:
        raise RuntimeError(
            "bind non-loopback proxy backend declaration must be a boolean"
        )
    logger = serve_mod._serve_logger
    logger.debug(
        f"_validate_bind_security: host={host!r}, auth_mode={mode!r}, "
        f"tls_proxy={tls_terminated_by_proxy}"
    )
    logger.info(f"Validating bind security, host={host!r}")
    unsafe_lab_require_startup(host=host)
    if mode == "api-key" and not resolved_api_key:
        logger.critical(
            f"bind security validation failed: api-key auth mode requires SONDER_API_KEY, host={host!r}"
        )
        raise RuntimeError("api-key auth mode requires SONDER_API_KEY")
    if mode == "both" and (not resolved_api_key or not resolved_secret):
        logger.critical(
            f"bind security validation failed: 'both' auth mode requires API key and account auth secret, "
            f"host={host!r}, has_api_key={bool(resolved_api_key)}, has_auth_secret={bool(resolved_secret)}"
        )
        raise RuntimeError("both auth mode requires API key and account auth secret")
    if serve_mod._is_loopback_host(host):
        return
    logger.warning(
        f"non-loopback bind requested, host={host!r}: verifying TLS proxy and strong auth requirements"
    )
    if tls_terminated_by_proxy is not True:
        logger.critical(
            f"bind security violation: non-loopback host={host!r} without TLS proxy declaration, refusing to start"
        )
        raise RuntimeError(
            "non-loopback bind requires SONDER_TLS_TERMINATED_BY_PROXY=1 "
            "for a TLS-terminating reverse proxy"
        )
    strong_api = len(resolved_api_key) >= min_api_key_length
    strong_account = len(resolved_secret) >= min_account_secret_length
    secure = {
        "api-key": strong_api,
        "account": strong_account,
        "both": strong_api and strong_account,
        "either": strong_api and strong_account,
        "local-open": False,
    }.get(mode, False)
    if not secure:
        logger.critical(
            f"bind security violation: non-loopback host={host!r} with auth_mode={mode!r} "
            "lacks strong credentials, refusing to start"
        )
        raise RuntimeError(
            "non-loopback bind requires explicitly configured strong authentication"
        )
    if allow_non_loopback_proxy_backend is not True:
        logger.critical(
            f"bind security violation: non-loopback host={host!r} with tls_terminated_by_proxy=True "
            "without SONDER_ALLOW_NON_LOOPBACK_PROXY_BACKEND=1, refusing to start"
        )
        raise RuntimeError(
            "tls_terminated_by_proxy requires a loopback host (127.0.0.1 / ::1) so plaintext HTTP "
            "is not exposed off-host; set SONDER_ALLOW_NON_LOOPBACK_PROXY_BACKEND=1 only when an "
            "isolated private network terminates TLS"
        )


def deployment_gating_summary(serve_mod: Any) -> str:
    """Report which HTTP permission tier this deployment is actually running in."""
    mode = effective_auth_mode(serve_mod)
    gated = serve_mod.DANGEROUS_HTTP_SLASH_COMMANDS | serve_mod._CONDITIONALLY_GATED_SLASH
    lines = [
        "Deployment gating (HTTP surface)",
        "  effective auth mode : %s" % mode,
        "  developer authority : %s" % DEVELOPER_AUTHORITY_BY_MODE.get(mode, "unknown"),
        "  gated slash names   : %d (aliases included)" % len(gated),
        "  model reasoning     : %s"
        % (
            "exposed to %s" % serve_mod._reasoning_audience()
            if serve_mod.server.reasoning_exposure_enabled()
            else "not exposed (SONDER_EXPOSE_REASONING is off)"
        ),
        "  bind                : %s:%s%s"
        % (
            serve_mod.HOST,
            serve_mod.DEFAULT_PORT if serve_mod.BOUND_PORT is None else serve_mod.BOUND_PORT,
            "" if serve_mod._is_loopback_host(serve_mod.HOST) else "  (non-loopback)",
        ),
    ]
    if mode == "local-open":
        lines.append(
            "  note                : anyone who can reach this port holds developer\n"
            "                        authority. That is the intended local default, and a\n"
            "                        non-loopback bind is refused outright in this mode.\n"
            "                        To restrict: set SONDER_API_KEY, or SONDER_REQUIRE_ACCOUNT=1\n"
            "                        and grant developer/admin per account."
        )
    else:
        lines.append(
            "  note                : callers without developer authority are refused the\n"
            "                        gated names above; everything else stays available."
        )
    return "\n".join(lines)


# --- Handler wire I/O, framing, CORS, Host, and response delivery helpers ---


def linger_before_close(handler: Any, serve_mod: Any) -> None:
    """Bounded lingering close for a connection ending on an unread body."""
    if (
        getattr(handler, "_artifact_transfer_request", False)
        or getattr(handler, "_memory_replication_request", False)
        or getattr(handler, "_app_control_request", False)
        or serve_mod._request_route(getattr(handler, "path", "")) in BODY_GUARDED_INVENTORY_ROUTES
    ):
        return
    if not handler.close_connection:
        return
    if getattr(handler, "_request_body_consumed", True):
        return
    connection = getattr(handler, "connection", None)
    wfile = getattr(handler, "wfile", None)
    if connection is None or wfile is None:
        return
    linger_seconds = float(serve_mod.LINGERING_CLOSE_SECONDS)
    deadline = time.monotonic() + linger_seconds
    remaining = int(serve_mod.LINGERING_CLOSE_BYTES)
    try:
        wfile.flush()
        connection.settimeout(linger_seconds)
        while remaining > 0 and time.monotonic() < deadline:
            chunk = connection.recv(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
    except (OSError, ValueError):
        return


def send_cors_headers(handler: Any, serve_mod: Any) -> None:
    """Write CORS headers matching either global CORS_ORIGINS or Observatory routes."""
    if serve_mod._request_route(getattr(handler, "path", "")) == "/v1/local/server-log":
        return
    headers = getattr(handler, "headers", None)
    if headers is None:
        return
    origins = headers.get_all("Origin") if hasattr(headers, "get_all") else None
    if origins is not None and len(origins) > 1:
        return
    origin = reflectable_origin(headers.get("Origin"))
    if origin is not None and origin in serve_mod.CORS_ORIGINS:
        if not (
            getattr(handler, "_artifact_transfer_request", False)
            or getattr(handler, "_app_control_request", False)
        ):
            serve_mod._serve_logger.debug(f"_cors: allowing origin={origin!r}")
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Sonder-Account-Token, "
            "X-Sonder-Bootstrap-Secret, X-Sonder-App-Control, X-Sonder-Spanda, Idempotency-Key, "
            "Accept, Cache-Control, Last-Event-ID",
        )
        handler.send_header(
            "Access-Control-Expose-Headers",
            "X-Sonder-Elapsed-Ms, X-Sonder-Correlation-Id, X-Sonder-Spanda-Rsc, "
            "X-Sonder-Spanda-Clusters, X-Sonder-Spanda-Decision, X-Sonder-Spanda-Uncertain",
        )
    elif handler._observatory_origin_request(origin):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        handler.send_header(
            "Access-Control-Allow-Headers",
            "Accept, Authorization, Cache-Control, Last-Event-ID",
        )
        handler.send_header("Access-Control-Max-Age", "600")
        handler.send_header("Access-Control-Expose-Headers", "X-Sonder-Correlation-Id")


def reject_disallowed_host(handler: Any, serve_mod: Any) -> bool:
    """Refuse a request whose Host could be a DNS-rebinding name (421)."""
    handler._host_trusted = False
    headers = getattr(handler, "headers", None)
    if headers is None:
        return False
    values = headers.get_all("Host") if hasattr(headers, "get_all") else None
    if values is not None and len(values) > 1:
        decision = None
    else:
        decision = host_decision(
            headers.get("Host"),
            allowed_hosts=serve_mod.ALLOWED_HOSTS,
            local_names=serve_mod._machine_host_names(),
            credentials_required=effective_auth_mode(serve_mod) != "local-open",
        )
    if decision is not None:
        handler._host_trusted = decision == HOST_TRUSTED
        return False
    serve_mod._log_rejected_host(headers.get("Host"))
    handler.close_connection = True
    handler._send_json_payload(
        {
            "error": {
                "message": "host is not allowed for this listener",
                "type": "invalid_request",
                "code": "HOST_NOT_ALLOWED",
                "remedy": HOST_NOT_ALLOWED_REMEDY,
            }
        },
        status=421,
        headers={"Connection": "close", "Cache-Control": "no-store"},
    )
    return True


def bind_request_auth_context(
    handler: Any,
    serve_mod: Any,
    *,
    role_tool_policy: Any,
    permission_policy: Any,
) -> dict[str, Any]:
    """Authenticate one request and bind principal, role, and permission-mode scopes."""
    context: dict[str, Any] = serve_mod._auth_context(
        handler.headers.get("Authorization", ""),
        handler.headers.get("X-Sonder-Account-Token", ""),
    )
    serve_mod._bind_request_build_principal(context)
    role = serve_mod._restricted_tool_role(context)
    local_open = _is_local_open_context(context)
    turn_stack = getattr(handler, "_turn_stack", None)
    if turn_stack is not None and not getattr(handler, "_mode_scope_bound", False):
        turn_stack.enter_context(role_tool_policy.caller_role_scope(role))
        permission_policy.bind_unauthenticated_caller(local_open)
        turn_stack.callback(permission_policy.bind_unauthenticated_caller, False)
        session_hdr = str(handler.headers.get("X-Sonder-Session", "") or "").strip()
        principal = serve_mod._state_principal(context)
        m_principal = serve_mod._mode_principal(context)
        turn_stack.enter_context(
            _request_mode_scope(
                permission_policy, context, principal, session_hdr,
            )
        )
        if m_principal != principal:
            turn_stack.enter_context(
                permission_policy.mode_scope(
                    m_principal, session_hdr, local_open=local_open,
                )
            )
        handler._mode_scope_bound = True
    else:
        role_tool_policy.bind_caller_role(role)
        permission_policy.bind_unauthenticated_caller(local_open)
    return context


def unread_request_body_bytes(handler: Any) -> int | None:
    """Declared body bytes *handler* has not taken off the socket, or ``None`` if unframed."""
    if getattr(handler, "_request_body_consumed", False):
        return 0
    if handler.headers.get_all("Transfer-Encoding"):
        return None
    lengths = handler.headers.get_all("Content-Length") or ()
    if not lengths:
        return 0
    if len(lengths) > 1:
        return None
    raw = lengths[0].strip()
    if not raw.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def settle_unread_request_body(handler: Any, serve_mod: Any) -> bool:
    """Return whether this response must also end the connection."""
    pending = handler._unread_request_body_bytes()
    if (
        getattr(handler, "_artifact_transfer_request", False)
        or getattr(handler, "_app_control_request", False)
        or serve_mod._request_route(getattr(handler, "path", "")) in BODY_GUARDED_INVENTORY_ROUTES
    ) and pending != 0:
        return True
    if pending == 0:
        handler._request_body_consumed = True
        return False
    if pending is None or pending > int(serve_mod.MAX_DISCARDED_BODY_BYTES):
        return True
    connection = getattr(handler, "connection", None)
    prior = None
    restore = False
    discard_timeout = float(serve_mod.DISCARD_BODY_TIMEOUT_SECONDS)
    try:
        if connection is not None:
            prior = connection.gettimeout()
            if prior is None or prior > discard_timeout:
                connection.settimeout(discard_timeout)
                restore = True
        discarded = handler.rfile.read(pending)
    except (OSError, ValueError):
        return True
    finally:
        if restore and connection is not None:
            try:
                connection.settimeout(prior)
            except OSError:
                pass
    if len(discarded) != pending:
        return True
    handler._request_body_consumed = True
    return False


def send_json_payload(
    handler: Any,
    serve_mod: Any,
    payload: Any,
    *,
    status: int = 200,
    headers: Mapping[str, Any] | None = None,
    elapsed_ms: int | float | None = None,
) -> bool:
    """Serialize *payload* to JSON and write a framed HTTP response."""
    resolved_headers: dict[str, Any] | None = dict(headers) if headers else None
    if serve_mod._request_route(getattr(handler, "path", "")) == "/v1/sonder/logout":
        resolved_headers = {
            **(resolved_headers or {}),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        }
    spanda_headers = getattr(handler, "_spanda_headers", None)
    if spanda_headers:
        resolved_headers = {**spanda_headers, **(resolved_headers or {})}
    serve_mod._serve_logger.debug(f"_send_json_payload: status={status}")
    body = json.dumps(payload).encode("utf-8")
    settle_unread_body = getattr(handler, "_close_for_unread_body", None)
    must_close = bool(settle_unread_body()) if callable(settle_unread_body) else False
    try:
        handler.send_response(status)
        handler._cors()
        if must_close:
            handler.send_header("Connection", "close")
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if getattr(handler, "_correlation_id", ""):
            handler.send_header("X-Sonder-Correlation-Id", handler._correlation_id)
        started = getattr(handler, "_request_started", None)
        if elapsed_ms is not None:
            handler.send_header("X-Sonder-Elapsed-Ms", str(max(0, int(elapsed_ms))))
        elif started is not None:
            handler.send_header(
                "X-Sonder-Elapsed-Ms",
                str(max(0, int((time.monotonic() - started) * 1000))),
            )
        for name, value in (resolved_headers or {}).items():
            handler.send_header(str(name), str(value))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return False


def send_binary_payload(
    handler: Any,
    body: bytes,
    *,
    content_type: str,
    digest: str,
    status: int = 200,
    headers: Mapping[str, Any] | None = None,
) -> bool:
    """Write a framed binary HTTP response with SHA-256 header and no-store cache."""
    if not isinstance(body, bytes):
        raise TypeError("binary response body must be bytes")
    must_close = bool(handler._close_for_unread_body())
    try:
        handler.send_response(status)
        handler._cors()
        if must_close:
            handler.send_header("Connection", "close")
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("X-Sonder-Artifact-Sha256", digest)
        handler.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            clean_name = str(name).replace("\r", "").replace("\n", "")
            clean_value = str(value).replace("\r", "").replace("\n", "")
            if clean_name:
                handler.send_header(clean_name, clean_value)
        handler.end_headers()
        handler.wfile.write(body)
        return True
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return False


def send_local_log_page(handler: Any) -> None:
    """Serve the loopback-only browser landing page without auth state."""
    body = LOCAL_LOG_PAGE.encode("utf-8")
    must_close = bool(handler._close_for_unread_body())
    handler.send_response(200)
    if must_close:
        handler.send_header("Connection", "close")
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def validate_request_framing(handler: Any) -> None:
    """Reject ambiguous body framing before any POST route dispatches."""
    transfer_encodings = handler.headers.get_all("Transfer-Encoding") or ()
    if transfer_encodings:
        raise HTTPRequestError(400, "transfer encoding is not supported")
    content_lengths = handler.headers.get_all("Content-Length") or ()
    if len(content_lengths) > 1:
        raise HTTPRequestError(400, "multiple Content-Length headers are not supported")


def validate_idempotency_key(handler: Any) -> None:
    """Reject an Idempotency-Key the replay guard could not honor."""
    keys = handler.headers.get_all("Idempotency-Key") or ()
    if len(keys) > 1:
        raise HTTPRequestError(400, "multiple Idempotency-Key headers are not supported")
    if keys and len(keys[0].strip()) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPRequestError(
            400,
            "Idempotency-Key must be at most %d characters" % MAX_IDEMPOTENCY_KEY_LENGTH,
        )


def read_json(handler: Any, serve_mod: Any, *, max_bytes: int | None = None) -> dict[str, Any]:
    """Validate framing, read bounded UTF-8 JSON, reject deep/duplicate/non-finite objects."""
    handler._validate_request_framing()
    content_lengths = handler.headers.get_all("Content-Length") or ()
    raw_length = content_lengths[0] if content_lengths else None
    if raw_length is None:
        raise HTTPRequestError(411, "Content-Length is required")
    if not raw_length.strip().isdigit():
        raise HTTPRequestError(400, "Content-Length must be a nonnegative integer")
    length = int(raw_length)
    limit = int(serve_mod.MAX_REQUEST_BYTES)
    effective_limit = limit if max_bytes is None else min(limit, max_bytes)
    if length > effective_limit:
        raise HTTPRequestError(413, "request body is too large")
    media_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPRequestError(415, "Content-Type must be application/json")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise HTTPRequestError(400, "request body is incomplete")
    handler._request_body_consumed = True
    try:
        text = raw.decode("utf-8")
        depth = 0
        in_str = False
        escaped = False
        for ch in text:
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
                if depth > 128:
                    raise RecursionError("JSON nesting depth exceeded")
            elif ch in "}]":
                depth = max(0, depth - 1)
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicate_json_object_key,
            parse_constant=reject_nonfinite_json_number,
        )
    except DuplicateJsonObjectKey:
        raise HTTPRequestError(
            400, "request JSON must not contain duplicate object keys"
        ) from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise HTTPRequestError(400, "request body must contain valid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPRequestError(400, "request JSON must be an object")
    return payload


__all__ = [
    "ACCOUNT_UNSCOPED_LOOP_ACTIONS",
    "ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS",
    "ACCOUNT_UNSCOPED_TASK_TOOLS",
    "BODY_GUARDED_INVENTORY_ROUTES",
    "DEVELOPER_AUTHORITY_BY_MODE",
    "IdempotencyRefusal",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "SCOPED_TASK_TOOLS",
    "a2a_discovery_base_url",
    "account_created_message",
    "account_global_memory_refusal",
    "account_identity",
    "account_task_boundary_refusal",
    "acquire_http_conversation_state",
    "admin_authorized",
    "admission_request_owner",
    "app_control_deployment_authorized",
    "audit_http_approval",
    "auth_context",
    "bearer_token",
    "bind_request_auth_context",
    "build_principal",
    "check_auth",
    "deployment_gating_summary",
    "developer_authorized",
    "effective_auth_mode",
    "env_flag",
    "env_int",
    "evict_owner_lru_session_state",
    "fanout_request_owner",
    "fanout_request_role",
    "feed_request_owner",
    "hosted_storage_id",
    "http_action_binding",
    "http_action_idempotency_key",
    "http_scope_value",
    "idempotency_refusal_payload",
    "idempotent_http_action",
    "is_loopback_host",
    "keyless_loopback_auth_mode",
    "linger_before_close",
    "log_rejected_host",
    "parse_allowed_hosts",
    "parse_cors_origins",
    "prune_http_session_states",
    "read_json",
    "reasoning_request_owner",
    "receipt_owner_scope",
    "reject_disallowed_host",
    "release_http_conversation_state",
    "request_account_token",
    "request_cache_scope",
    "request_idempotency_key",
    "require_exact_bind_values",
    "resolve_auth_mode",
    "restricted_tool_role",
    "selected_listener_port",
    "send_binary_payload",
    "send_cors_headers",
    "send_idempotency_refusal",
    "send_json_payload",
    "send_local_log_page",
    "settle_unread_request_body",
    "state_principal",
    "system_operation_authority_error",
    "task_account_scope",
    "unread_request_body_bytes",
    "validate_bind_security",
    "validate_idempotency_key",
    "validate_request_framing",
    "warn_if_local_open",
]
