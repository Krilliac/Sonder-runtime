"""Private local-operator REPL selection; canonical history IDs are not authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import re
from threading import RLock
import time

from ..application.context import LOCAL_OWNER
from ..application.ports.lane_continuation import HostContinuationGrant


@dataclass(frozen=True)
class ReplHostPolicy:
    grant_id: str
    revision: int
    expires_at: float
    workspace_roots: tuple[str, ...]
    allowed_tools: tuple[str, ...]

    def __post_init__(self):
        if (
            not isinstance(self.grant_id, str)
            or not 1 <= len(self.grant_id.encode()) <= 256
        ):
            raise ValueError("invalid host grant identity")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("invalid host grant revision")
        if type(self.expires_at) not in (int, float) or not math.isfinite(
            self.expires_at
        ):
            raise ValueError("invalid host grant expiry")
        for entries, bound in ((self.workspace_roots, 16), (self.allowed_tools, 64)):
            if (
                not isinstance(entries, tuple)
                or not 1 <= len(entries) <= bound
                or any(
                    not isinstance(value, str) or not 1 <= len(value.encode()) <= 4096
                    for value in entries
                )
                or entries != tuple(sorted(set(entries)))
            ):
                raise ValueError("host policy must be bounded, ordered and unique")
        for entry in self.workspace_roots:
            path = Path(entry)
            if (
                not path.is_absolute()
                or str(path.resolve()) != entry
                or not path.is_dir()
            ):
                raise ValueError(
                    "host policy requires canonical existing project roots"
                )


@dataclass(frozen=True)
class HostConversationSelection:
    session_id: str
    host_conversation_id: str
    policy: ReplHostPolicy
    epoch: int
    _issuer: object = field(repr=False, compare=False)


class ReplHostSelectionAdapter:
    """Callbacks are trusted host composition, never tool arguments.

    The policy provider owns stable grant identity/revision across restart and
    validates the complete private-state inventory before granting authority.
    Normal memory-session updated_ts is deliberately not a grant revision.
    """

    def __init__(self, *, get_session, find_session, touch_session, policy):
        if not all(
            callable(callback)
            for callback in (get_session, find_session, touch_session, policy)
        ):
            raise PermissionError("trusted session and policy callbacks required")
        self._get_session = get_session
        self._find_session = find_session
        self._touch_session = touch_session
        self._policy = policy
        self._issuer = object()
        self._lock = RLock()
        self._epoch = 0
        self._current = None
        self._scope = ContextVar("private_repl_host_selection", default=None)

    @staticmethod
    def _context(context):
        if context.deadline_monotonic is not None and (
            type(context.deadline_monotonic) not in (int, float)
            or not math.isfinite(context.deadline_monotonic)
        ):
            raise PermissionError("finite host deadline required")
        if (
            context.source != "repl"
            or context.auth_level != "local"
            or context.principal_id != LOCAL_OWNER
            or context.expired
            or context.cancellation.cancelled
        ):
            raise PermissionError("current local REPL operator required")

    def _row(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(
            "[0-9a-f]{16}", session_id
        ):
            raise PermissionError("canonical persisted memory session required")
        row = self._get_session(session_id)
        if not isinstance(row, dict) or row.get("session_id") != session_id:
            raise PermissionError("canonical persisted memory session unavailable")

    def _live_policy(self, session_id, context):
        self._context(context)
        self._row(session_id)
        policy = self._policy(context, session_id)
        if not isinstance(policy, ReplHostPolicy):
            raise PermissionError("typed host policy required")
        # Revalidate directory existence and canonical identity on every admission.
        policy.__post_init__()
        if policy.expires_at <= time.time():
            raise PermissionError("host policy expired")
        roots = tuple(Path(root).resolve() for root in context.workspace_roots)
        if not roots or len(roots) > 256:
            raise PermissionError("bounded current workspace selection required")
        if any(
            not any(
                Path(entry) == root or root in Path(entry).parents for root in roots
            )
            for entry in policy.workspace_roots
        ):
            raise PermissionError("host policy outside current workspace selection")
        return policy

    def clear(self):
        with self._lock:
            self._epoch += 1
            self._current = None

    def select_exact(self, session_id, context):
        with self._lock:
            policy = self._live_policy(session_id, context)
            self._epoch += 1
            selection = HostConversationSelection(
                session_id,
                "repl-session:" + session_id,
                policy,
                self._epoch,
                self._issuer,
            )
            self._current = selection
            return selection

    def create(self, session_id, context):
        with self._lock:
            self._context(context)
            if not isinstance(session_id, str) or not re.fullmatch(
                "[0-9a-f]{16}", session_id
            ):
                raise PermissionError("canonical host-created session ID required")
            self._touch_session(session_id)
            return self.select_exact(session_id, context)

    def select_resolved(self, query, context):
        with self._lock:
            self._context(context)
            if not isinstance(query, str) or not 1 <= len(query.encode()) <= 256:
                raise ValueError("bounded operator session selector required")
            return self.select_exact(self._find_session(query), context)

    def _validate(self, selection, context):
        if (
            not isinstance(selection, HostConversationSelection)
            or selection._issuer is not self._issuer
            or self._current is not selection
            or selection.epoch != self._epoch
        ):
            raise PermissionError("current private host selection required")
        policy = self._live_policy(selection.session_id, context)
        original = selection.policy
        if policy.grant_id != original.grant_id or policy.revision != original.revision:
            raise PermissionError("host grant revision changed")
        if original.expires_at <= time.time():
            raise PermissionError("original host grant expired")
        if any(
            not any(
                Path(entry) == Path(root) or Path(root) in Path(entry).parents
                for root in original.workspace_roots
            )
            for entry in policy.workspace_roots
        ):
            raise PermissionError("host workspace authority expanded")
        if not set(policy.allowed_tools).issubset(original.allowed_tools):
            raise PermissionError("host tool authority expanded")
        return replace(policy, expires_at=min(policy.expires_at, original.expires_at))

    @contextmanager
    def scope(self, selection, context):
        with self._lock:
            self._validate(selection, context)
            token = self._scope.set((selection, context))
        try:
            yield selection
        finally:
            self._scope.reset(token)

    def authorize(self, context, host_conversation_id):
        with self._lock:
            scoped = self._scope.get()
            if scoped is None:
                raise PermissionError("private host context scope required")
            selection, original = scoped
            self._context(original)
            self._context(context)
            if (
                context.cancellation is not original.cancellation
                or context.source != original.source
                or context.principal_id != original.principal_id
                or context.auth_level != original.auth_level
                or (context.cloud_allowed and not original.cloud_allowed)
                or (
                    context.remote_ollama_allowed and not original.remote_ollama_allowed
                )
                or (
                    original.deadline_monotonic is not None
                    and (
                        context.deadline_monotonic is None
                        or not math.isfinite(context.deadline_monotonic)
                        or context.deadline_monotonic > original.deadline_monotonic
                    )
                )
            ):
                raise PermissionError("scoped host authority expanded or replaced")
            original_roots = tuple(
                Path(root).resolve() for root in original.workspace_roots
            )
            if len(context.workspace_roots) > 256 or any(
                not any(
                    Path(root).resolve() == allowed
                    or allowed in Path(root).resolve().parents
                    for allowed in original_roots
                )
                for root in context.workspace_roots
            ):
                raise PermissionError("scoped host workspace authority expanded")
            policy = self._validate(selection, context)
            if host_conversation_id != selection.host_conversation_id:
                raise PermissionError("selected host conversation mismatch")
            return HostContinuationGrant(
                LOCAL_OWNER,
                selection.host_conversation_id,
                policy.grant_id,
                policy.revision,
                policy.expires_at,
                policy.workspace_roots,
                policy.allowed_tools,
            )
