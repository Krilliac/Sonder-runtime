"""Issuer-only app host selection and ordered account/fleet authority."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import math
from pathlib import Path
import threading
import time
import uuid

from ..adapters.security.account_admission import account_admission
from ..adapters.security.account_auth import account_auth
from ..application.ports.lane_continuation import HostContinuationGrant
from ..application.ports.app_control import AppControlError
from .app_control_http import grant_snapshot, _principal


@dataclass(frozen=True, eq=False, repr=False)
class AppHostSelection:
    account: object
    control: object
    binding: object
    slot: object
    context: object
    allowed_tools: tuple
    original_context: object
    _issuer: object = field(compare=False, repr=False)

    @property
    def host_conversation_id(self):
        return self.binding.canonical_host_id

    def __reduce__(self):
        raise TypeError("app selection cannot be persisted")


@dataclass(eq=False, repr=False)
class AppAdmission:
    selection: AppHostSelection
    context: object
    account: object
    _issuer: object
    thread: int
    registration: object = None
    active: bool = True
    connection: object = None
    key_digest: str = ""

    def __reduce__(self):
        raise TypeError("app admission cannot be persisted")


@dataclass(frozen=True, eq=False, repr=False)
class AppParentRegistration:
    parent_session_id: str
    continuation_id: str
    principal_id: str
    parent_grant_revision: int
    epoch: int
    owner: str
    generation: str
    selection: AppHostSelection
    bound: object


def _finite(context):
    return (
        context.deadline_monotonic is not None
        and math.isfinite(context.deadline_monotonic)
        and not context.expired
    )


def _context_within(context, ceiling, *, check_cancel=True):
    if (
        context.principal_id != ceiling.principal_id
        or context.auth_level != ceiling.auth_level
        or context.source != ceiling.source
        or context.cancellation is not ceiling.cancellation
        or not _finite(context)
        or not _finite(ceiling)
        or context.deadline_monotonic > ceiling.deadline_monotonic
        or context.cloud_allowed
        and not ceiling.cloud_allowed
        or context.remote_ollama_allowed
        and not ceiling.remote_ollama_allowed
        or check_cancel
        and (context.cancellation.cancelled or ceiling.cancellation.cancelled)
        or not context.workspace_roots
        or any(
            not any(
                Path(root).resolve().is_relative_to(Path(allowed))
                for allowed in ceiling.workspace_roots
            )
            for root in context.workspace_roots
        )
    ):
        raise PermissionError("app context exceeds original authority")


def _selection_identity(selection):
    context = selection.context
    original = selection.original_context
    return (
        selection.account,
        selection.control,
        selection.binding,
        selection.slot,
        context,
        selection.allowed_tools,
        original,
        id(selection._issuer),
    )


class AppManagedAuthority:
    def __init__(self, binding, lanes, *, capacity=128):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("bounded app parent capacity required")
        if (
            binding.store is None
            or Path(binding.store.path) != Path(lanes.store.path)
            or getattr(lanes, "managed_authority", None) is not None
            or getattr(binding, "_managed_authority", None) is not None
        ):
            raise PermissionError("app authority composition mismatch")
        binding._private()
        self.binding, self.lanes, self.capacity = binding, lanes, capacity
        self._issuer = object()
        self._lock = threading.RLock()
        self._selections = {}
        self._parents = {}
        self._workers = {}
        self._admissions = {}
        self._selection_uses = {}
        self._admission_threads = {}
        self._retained = {}
        self._callback_threads = set()
        binding._managed_authority = self
        lanes.managed_authority = self

    def issue_selection(self, *, account_token, control_token, context):
        self._outside_work_callback()
        binding = self.binding
        binding._config()
        binding._private(context_roots=context.workspace_roots)
        conn = binding._open()
        try:
            with account_admission(conn):
                account = binding._account(conn, account_token)
                control, grant = binding._session(account, control_token)
                if (
                    context.principal_id != _principal(account)
                    or context.auth_level != "admin"
                    or context.source != "http"
                    or not _finite(context)
                    or context.cancellation.cancelled
                ):
                    raise PermissionError("authenticated HTTP app context required")

                def selected(tx):
                    slot = tx.read_selection(
                        principal_id=control.principal_id,
                        control_session_id=control.control_session_id,
                    )
                    if slot is None or slot.binding_id is None:
                        raise PermissionError("explicit app binding selection required")
                    return tx.require_selection(
                        principal_id=control.principal_id,
                        control_session_id=control.control_session_id,
                        binding_id=slot.binding_id,
                        binding_revision=slot.binding_revision,
                        selection_id=slot.selection_id,
                        epoch=slot.epoch,
                    )

                current, record, slot = binding.store.atomic(selected)
                if (
                    current != control
                    or current.account_session_ref != account.reference
                ):
                    raise PermissionError("app selection session changed")
                roots = set()
                for request_root in context.workspace_roots:
                    request_root = Path(request_root)
                    if (
                        not request_root.is_absolute()
                        or request_root != request_root.resolve()
                        or not request_root.is_dir()
                    ):
                        raise PermissionError(
                            "canonical live request workspace required"
                        )
                    for allowed in grant.roots:
                        allowed = Path(allowed)
                        if request_root.is_relative_to(allowed):
                            roots.add(request_root)
                        elif allowed.is_relative_to(request_root):
                            roots.add(allowed)
                tools = tuple(sorted(set(grant.tools) & self.lanes.allowed_tools))
                if not roots or not tools:
                    raise PermissionError(
                        "app request has no delegated workspace/tools"
                    )
                config = binding._config()
                ceiling = replace(
                    context,
                    workspace_roots=tuple(sorted(roots)),
                    cloud_allowed=context.cloud_allowed
                    and grant.allow_cloud
                    and config.features.cloud,
                    remote_ollama_allowed=context.remote_ollama_allowed
                    and grant.allow_remote
                    and config.compute.allow_remote,
                    deadline_monotonic=min(
                        context.deadline_monotonic,
                        time.monotonic()
                        + max(
                            0,
                            min(
                                account.expires_at,
                                current.expires_at,
                                record.expires_at,
                                grant.expires_at,
                            )
                            - time.time(),
                        ),
                    ),
                )
                _context_within(ceiling, context)
                result = AppHostSelection(
                    account,
                    current,
                    record,
                    slot,
                    ceiling,
                    tools,
                    context,
                    self._issuer,
                )
                binding._current(conn, account_token, account, grant)
                with self._lock:
                    for key, (prior, _) in tuple(self._selections.items()):
                        if prior.context.expired and not self._referenced_locked(prior):
                            self._selections.pop(key, None)
                    if len(self._selections) >= self.capacity:
                        raise PermissionError("app selection capacity unavailable")
                    self._selections[id(result)] = (result, _selection_identity(result))
                return result
        finally:
            conn.close()

    def _issued_locked(self, selection):
        issued = self._selections.get(id(selection))
        if (
            type(selection) is not AppHostSelection
            or issued is None
            or issued[0] is not selection
            or issued[1] != _selection_identity(selection)
            or selection._issuer is not self._issuer
        ):
            raise PermissionError("private app selection issuer required")
        return selection

    def _referenced_locked(self, selection):
        return (
            self._selection_uses.get(id(selection), 0)
            or any(value is selection for value in self._retained.values())
            or any(entry.selection is selection for entry in self._parents.values())
        )

    def _outside_work_callback(self):
        with self._lock:
            if threading.get_ident() in self._callback_threads:
                raise PermissionError("work callback cannot enter host authority")

    def retain_selection(self, selection):
        """Retain one exact work reference; this is not renewed authority."""
        self._outside_work_callback()
        self._selection(selection)
        with self._lock:
            self._issued_locked(selection)
            if len(self._retained) >= 512:
                raise PermissionError("retained app selection capacity unavailable")
            lease = object()
            self._retained[lease] = selection
            return lease

    def release_retained(self, lease):
        """Release only a live issuer-owned lease, including after revocation."""
        with self._lock:
            if type(lease) is not object or lease not in self._retained:
                raise PermissionError("exact retained app selection lease required")
            self._issued_locked(self._retained[lease])
            del self._retained[lease]

    def release_selection(self, selection):
        """Forget an unreferenced request selection without granting authority."""
        with self._lock:
            self._issued_locked(selection)
            if self._referenced_locked(selection):
                raise PermissionError("app selection still has active references")
            del self._selections[id(selection)]

    def work_atomic(self, selection, context, callback):
        """Account-before-fleet admission with an explicitly borrowed app write."""
        self._outside_work_callback()
        with self._lock:
            if self._admission_threads.get(threading.get_ident(), 0):
                raise PermissionError("work transaction cannot nest inside admission")
        if not callable(callback):
            raise TypeError("app work transaction callback required")
        with self.admit(selection, context) as admission:
            with self.lanes.store.transaction() as tx:
                self.authorize_host(
                    admission,
                    context,
                    selection.host_conversation_id,
                    connection=tx.conn,
                )
                thread = threading.get_ident()
                with self._lock:
                    self._callback_threads.add(thread)
                try:
                    result = self.binding.store.atomic(callback, connection=tx.conn)
                finally:
                    with self._lock:
                        self._callback_threads.discard(thread)
                self.authorize_host(
                    admission,
                    context,
                    selection.host_conversation_id,
                    connection=tx.conn,
                )
            return result

    def _selection(self, selection):
        self._outside_work_callback()
        with self._lock:
            self._issued_locked(selection)
        _context_within(selection.context, selection.original_context)
        self.binding._private(context_roots=selection.original_context.workspace_roots)
        return selection

    @contextmanager
    def admit(self, subject, context):
        self._outside_work_callback()
        with self._lock:
            if type(subject) is AppHostSelection:
                selection = self._issued_locked(subject)
            else:
                entry = self._parents.get(subject)
                if entry is None:
                    raise PermissionError("active app parent registration unavailable")
                selection = self._issued_locked(entry.selection)
            key = id(selection)
            self._selection_uses[key] = self._selection_uses.get(key, 0) + 1
            thread = threading.get_ident()
            self._admission_threads[thread] = self._admission_threads.get(thread, 0) + 1
        try:
            with self._admit(subject, context) as admission:
                yield admission
        finally:
            with self._lock:
                thread_uses = self._admission_threads[thread] - 1
                if thread_uses:
                    self._admission_threads[thread] = thread_uses
                else:
                    del self._admission_threads[thread]
                remaining = self._selection_uses[key] - 1
                if remaining:
                    self._selection_uses[key] = remaining
                else:
                    del self._selection_uses[key]

    @contextmanager
    def _admit(self, subject, context):
        registration = None
        if type(subject) is AppHostSelection:
            selection = self._selection(subject)
        else:
            with self._lock:
                registration = self._parents.get(subject)
            if registration is None:
                raise PermissionError("active app parent registration unavailable")
            selection = self._selection(registration.selection)
        if context.source == "worker":
            with self._lock:
                proof = self._workers.get(id(context))
            if (
                proof is None
                or proof[0] is not context
                or registration is None
                or proof[1] != registration.generation
            ):
                raise PermissionError("service-issued worker context required")
            if context.expired:
                raise PermissionError("worker grant expired")
        else:
            _context_within(context, selection.context)
        binding = self.binding
        binding._config()
        binding._private(context_roots=context.workspace_roots)
        conn = binding._open()
        admission = None
        try:
            with account_admission(conn):
                binding._source(conn)
                key_digest = hashlib.sha256(account_auth._secret().encode()).hexdigest()
                account = account_auth.read_session_reference(
                    conn, selection.account.reference
                )
                if (
                    key_digest
                    != hashlib.sha256(account_auth._secret().encode()).hexdigest()
                ):
                    raise PermissionError(
                        "account signing key changed during reference read"
                    )
                if (
                    account != selection.account
                    or account is None
                    or account.role != "admin"
                ):
                    raise PermissionError("exact app account session is no longer live")
                binding._config()
                grant = binding._grant(account, selection.control.grant.project_handle)
                if grant_snapshot(grant) != selection.control.grant:
                    raise PermissionError("original app grant changed")
                admission = AppAdmission(
                    selection,
                    context,
                    account,
                    self._issuer,
                    threading.get_ident(),
                    registration,
                )
                admission.key_digest = key_digest
                with self._lock:
                    if len(self._admissions) >= 128:
                        raise PermissionError(
                            "active app admission capacity unavailable"
                        )
                    self._admissions[id(admission)] = dict(
                        token=admission,
                        thread=admission.thread,
                        context=context,
                        selection=selection,
                        account=account,
                        key_digest=admission.key_digest,
                        connection=None,
                    )
                yield admission
        finally:
            if admission is not None:
                with self._lock:
                    self._admissions.pop(id(admission), None)
                admission.active = False
                admission.connection = None
            conn.close()

    def _check(self, admission, context, connection):
        self._outside_work_callback()
        with self._lock:
            scope = self._admissions.get(id(admission))
            if (
                scope is None
                or scope["token"] is not admission
                or scope["thread"] != threading.get_ident()
                or scope["context"] is not context
                or scope["selection"] is not admission.selection
                or scope["account"] != admission.account
                or scope["key_digest"] != admission.key_digest
                or scope["connection"] is not None
                and scope["connection"] is not connection
            ):
                raise PermissionError("private admission scope is no longer current")
            scope["connection"] = connection
        if (
            type(admission) is not AppAdmission
            or not admission.active
            or admission._issuer is not self._issuer
            or admission.thread != threading.get_ident()
            or admission.context is not context
            or not connection.in_transaction
            or admission.connection is not None
            and admission.connection is not connection
        ):
            raise PermissionError("exact live app transaction admission required")
        if (
            admission.account.expires_at <= time.time()
            or admission.key_digest
            != hashlib.sha256(account_auth._secret().encode()).hexdigest()
        ):
            raise PermissionError("account admission expired or signing key changed")
        admission.connection = connection
        selection = self._selection(admission.selection)
        try:
            session, binding, slot = self.binding.store.atomic(
                lambda tx: tx.require_selection(
                    principal_id=selection.control.principal_id,
                    control_session_id=selection.control.control_session_id,
                    binding_id=selection.binding.binding_id,
                    binding_revision=selection.binding.revision,
                    selection_id=selection.slot.selection_id,
                    epoch=selection.slot.epoch,
                ),
                connection=connection,
            )
        except AppControlError as exc:
            raise PermissionError("app selection is no longer current") from exc
        if (
            session != selection.control
            or binding != selection.binding
            or slot != selection.slot
        ):
            raise PermissionError("sealed app selection changed")
        current = self.binding._grant(admission.account, session.grant.project_handle)
        if grant_snapshot(current) != session.grant:
            raise PermissionError("app catalog changed")
        if not set(selection.allowed_tools).issubset(self.lanes.allowed_tools):
            raise PermissionError("live lane tools were reduced")
        if admission.registration is not None:
            entry = admission.registration
            with self._lock:
                if self._parents.get(entry.parent_session_id) is not entry:
                    raise PermissionError("app attachment generation was removed")
            row = connection.execute(
                "SELECT data FROM agent_lane_continuations WHERE id=? AND principal=?",
                (entry.continuation_id, entry.principal_id),
            ).fetchone()
            if row is None:
                raise PermissionError("app parent unavailable")
            import json

            record = json.loads(row[0])
            if (
                entry.bound._closed
                or entry.bound._lease.handle.closed
                or record["parent_session_id"] != entry.parent_session_id
                or record["epoch"] != entry.epoch
                or record["owner"] != entry.owner
                or record["parent_grant_revision"] != entry.parent_grant_revision
                or record.get("attachment_state") != "active"
                or record["expires_at"] <= time.time()
            ):
                raise PermissionError("app attachment epoch changed")
        return selection

    def require_bound(self, admission, bound, record, context, *, connection):
        with self._lock:
            entry = self._parents.get(record["parent_session_id"])
        if (
            entry is None
            or entry.bound is not bound
            or entry.continuation_id != record["id"]
        ):
            raise PermissionError("private app attachment registration is absent")
        if (
            type(admission) is not AppAdmission
            or admission.selection is not entry.selection
        ):
            raise PermissionError("app attachment selection mismatch")
        admission.registration = entry
        self._check(admission, context, connection)

    def authorize_host(self, admission, context, host_conversation_id, *, connection):
        selection = self._check(admission, context, connection)
        if (
            host_conversation_id != selection.host_conversation_id
            or context.source != "http"
        ):
            raise PermissionError("exact canonical app host required")
        _context_within(context, selection.context)
        return HostContinuationGrant(
            context.principal_id,
            host_conversation_id,
            selection.control.grant.grant_id,
            selection.control.grant.revision,
            min(
                selection.control.expires_at,
                selection.binding.expires_at,
                selection.account.expires_at,
            ),
            tuple(str(p) for p in selection.context.workspace_roots),
            selection.allowed_tools,
        )

    def authorize_lane(self, admission, lane, context, *, connection):
        selection = self._check(admission, context, connection)
        entry = admission.registration
        if entry is None:
            with self._lock:
                entry = self._parents.get(lane["parent_session_id"])
        if entry is not None and admission.registration is None:
            # A parent-bound continuation transaction carries the selection rather
            # than a worker registration. Still validate its exact durable epoch.
            admission.registration = entry
            self._check(admission, context, connection)
        if (
            entry is None
            or entry.selection is not selection
            or lane["parent_session_id"] != entry.parent_session_id
            or lane["principal_id"] != selection.control.principal_id
            or lane["auth_level"] != "admin"
            or not set(lane["allowed_tools"]).issubset(selection.allowed_tools)
            or not any(
                Path(lane["workspace_root"]).resolve().is_relative_to(p)
                for p in selection.context.workspace_roots
            )
            or lane["grant_expires"]
            > min(
                selection.control.expires_at,
                selection.binding.expires_at,
                selection.account.expires_at,
            )
            or lane["cloud_allowed"]
            and not selection.context.cloud_allowed
            or lane["remote_ollama_allowed"]
            and not selection.context.remote_ollama_allowed
        ):
            raise PermissionError("lane exceeds registered app authority")
        if context.source == "worker":
            with self._lock:
                proof = self._workers.get(id(context))
            if proof is None or proof[2:4] != (lane["id"], lane["attempt_id"]):
                raise PermissionError("worker attempt identity changed")
        else:
            _context_within(context, selection.context)

    def continuation_service(
        self,
        selection,
        *,
        projection_codec=None,
        command_codec=None,
        terminal_result_codec=None
    ):
        from ..application.agents.lane_continuation import LaneContinuationService

        self._selection(selection)
        return LaneContinuationService(
            self.lanes,
            managed_authority=self,
            authority_subject=selection,
            projection_codec=projection_codec,
            command_codec=command_codec,
            terminal_result_codec=terminal_result_codec,
            model_writable_roots=lambda: self.binding._config().state.workspace_roots,
        )

    def register_parent(self, bound, record, context):
        selection = bound._service.authority_subject
        with self.admit(selection, context) as admission:
            with self.lanes.store.transaction() as tx:
                self.authorize_host(
                    admission,
                    context,
                    selection.host_conversation_id,
                    connection=tx.conn,
                )
                current = bound._service._row(tx, record["id"])
                if current != record or bound._closed or bound._lease.handle.closed:
                    raise PermissionError("app parent registration changed")
                entry = AppParentRegistration(
                    record["parent_session_id"],
                    record["id"],
                    record["principal_id"],
                    record["parent_grant_revision"],
                    record["epoch"],
                    record["owner"],
                    uuid.uuid4().hex,
                    selection,
                    bound,
                )
                with self._lock:
                    if (
                        len(self._parents) >= self.capacity
                        or entry.parent_session_id in self._parents
                    ):
                        raise PermissionError("app parent registry unavailable")
                    self._parents[entry.parent_session_id] = entry
                return entry

    def release_parent(self, bound):
        self._outside_work_callback()
        with self._lock:
            matches = [p for p, v in self._parents.items() if v.bound is bound]
            for parent in matches:
                entry = self._parents.pop(parent)
                for key, proof in tuple(self._workers.items()):
                    if proof[1] == entry.generation:
                        self._workers.pop(key, None)

    def bind_worker(self, lane, parent_context, worker_context, *, issuer):
        if issuer is None or issuer is not getattr(self.lanes, "_worker_issuer", None):
            raise PermissionError("lane service worker issuer required")
        with self._lock:
            entry = self._parents.get(lane["parent_session_id"])
        if entry is None:
            raise PermissionError("app parent unavailable")
        _context_within(parent_context, entry.selection.context)
        if (
            worker_context.source != "worker"
            or worker_context.principal_id != entry.principal_id
            or worker_context.auth_level != "admin"
            or not _finite(worker_context)
            or worker_context.deadline_monotonic > parent_context.deadline_monotonic
            or worker_context.workspace_roots != (Path(lane["workspace_root"]),)
        ):
            raise PermissionError("invalid trusted worker derivation")
        with self._lock:
            if len(self._workers) >= 512:
                raise PermissionError("worker proof capacity unavailable")
            self._workers[id(worker_context)] = (
                worker_context,
                entry.generation,
                lane["id"],
                lane["attempt_id"],
            )

    def release_worker(self, context, *, issuer):
        if issuer is not self.lanes._worker_issuer:
            raise PermissionError("lane service worker issuer required")
        with self._lock:
            proof = self._workers.get(id(context))
            if proof is not None and proof[0] is context:
                self._workers.pop(id(context), None)
