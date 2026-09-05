"""Explicit host reauthorization. Durable identity never substitutes for authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import threading
import time
import uuid

from ..ports.lane_continuation import (
    ContinuationSelection,
    HostContinuationGrant,
    PreparedReattachment,
    RecoveryItem,
    RecoveryPage,
    GrantedApprovalEvidence,
    VerificationApprovalPending,
    PendingVerificationIdentity,
    seal_projection,
    open_projection,
    TerminalProjectionReceipt,
    HostAuthorityCeiling,
    PendingApprovalEvidence,
)

_CURRENT_BOUND = ContextVar("lane_host_attachment", default=None)
_PENDING_RESUME = ContextVar("lane_pending_verification_resume", default=None)


def require_root_admission(tx, store, parent, context):
    """Called in the SAME transaction as root command/effect admission."""
    row = tx.conn.execute(
        "SELECT data FROM agent_lane_continuations WHERE parent_session=?", (parent,)
    ).fetchone()
    if row is None:
        return
    bound = _CURRENT_BOUND.get()
    if bound is None or bound._service.store.path != store.path:
        raise PermissionError("host-managed root requires current attachment")
    value = json.loads(row[0])
    if (
        value["parent_session_id"] != parent
        or context.principal_id != value["principal_id"]
    ):
        raise PermissionError("host attachment scope mismatch")
    bound._require_current_tx(tx, value)
    return value


def current_verification_binding(tx, store, prepared, context):
    record = require_root_admission(tx, store, prepared.parent_session_id, context)
    if record is None:
        return None
    identity = record.get("pending_verification")
    if (
        not identity
        or identity["verification_id"] != prepared.verification_id
        or identity["bundle_digest"] != prepared.bundle_digest
    ):
        raise PermissionError(
            "managed verification requires original terminal projection link"
        )
    projection = tx.terminal_projection(
        record["id"], context.principal_id, prepared.verification_id
    )
    if (
        projection.sha256 != identity["projection_digest"]
        or projection.binding.revision != identity["projection_revision"]
    ):
        raise PermissionError("original terminal projection link changed")
    return record


def permits_pending_context(prepared):
    permit = _PENDING_RESUME.get()
    bound = _CURRENT_BOUND.get()
    return (
        permit is not None
        and bound is not None
        and permit == (bound, prepared.verification_id, prepared.bundle_digest)
    )


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 256:
        raise ValueError("bounded host identity required")
    return value


class BoundContinuation:
    """Private live lease, never a serialized capability or reusable context."""

    def __init__(self, service, record, context, lease, issuer):
        if issuer is not service._issuer:
            raise PermissionError("host attachment issuer mismatch")
        self._service, self._live_context, self._lease = service, context, lease
        self._context = replace(
            context,
            workspace_roots=tuple(Path(root) for root in record["workspace_roots"]),
            cloud_allowed=record["cloud_allowed"],
            remote_ollama_allowed=record["remote_allowed"],
            deadline_monotonic=min(
                context.deadline_monotonic or float("inf"),
                time.monotonic() + max(0, record["expires_at"] - time.time()),
            ),
        )
        self._issuer = issuer
        self.continuation_id = record["id"]
        self._epoch, self._owner = record["epoch"], record["owner"]
        self._closed = False
        self._lock = threading.RLock()

    def _require_current_tx(self, tx, record, *, observe=False):
        if (
            self._closed
            or self._issuer is not self._service._issuer
            or self._lease.handle.closed
            or record["id"] != self.continuation_id
            or record["epoch"] != self._epoch
            or record["owner"] != self._owner
            or record.get("attachment_state") != "active"
        ):
            raise PermissionError("host attachment is fenced")
        self._service._current(tx, record, self._live_context, observe=observe)

    def require_current(self, *, context=None):
        with self._lock, self._service.store.transaction() as tx:
            self._require_current_tx(tx, self._service._row(tx, self.continuation_id))
            if context is not None:
                ceiling = self._context
                if (
                    context.principal_id != ceiling.principal_id
                    or context.auth_level != ceiling.auth_level
                    or context.source != ceiling.source
                    or context.cancellation is not ceiling.cancellation
                    or context.expired
                    or context.cancellation.cancelled
                    or context.cloud_allowed
                    and not ceiling.cloud_allowed
                    or context.remote_ollama_allowed
                    and not ceiling.remote_ollama_allowed
                    or context.deadline_monotonic is None
                    or not math.isfinite(context.deadline_monotonic)
                    or context.deadline_monotonic > ceiling.deadline_monotonic
                    or any(
                        not any(
                            Path(root).resolve().is_relative_to(allowed)
                            for allowed in ceiling.workspace_roots
                        )
                        for root in context.workspace_roots
                    )
                ):
                    raise PermissionError(
                        "parent admission context exceeds original host ceiling"
                    )

    def authority_ceiling(self):
        self.require_current()
        context = self._context
        return HostAuthorityCeiling(
            context.principal_id,
            context.auth_level,
            tuple(str(root) for root in context.workspace_roots),
            context.cloud_allowed,
            context.remote_ollama_allowed,
            context.deadline_monotonic,
        )

    @contextmanager
    def _scope(self):
        with self._lock:
            self.require_current()
            token = _CURRENT_BOUND.set(self)
            try:
                yield self._context
            finally:
                _CURRENT_BOUND.reset(token)

    def close(self):
        with self._lock:
            self._closed = True
            self._lease.close()

    def dispatch(self, prepared_host_command):
        codec = self._service.command_codec
        if codec is None:
            raise PermissionError("trusted host command codec unavailable")
        # The injected host validates its immutable command issuer and exact gate.
        action, payload = codec.decode_command(prepared_host_command)
        with self._scope() as context:
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
            return self._service._dispatch(record, action, payload, context)

    def prepare_verification(self, verifier, *, command_id):
        self._verifier(verifier)
        with self._scope() as context:
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
            return verifier.prepare(
                record["parent_session_id"],
                command_id=command_id,
                context=context,
                bound_parent_revision=record["parent_grant_revision"],
            )

    def verification_view(self, verifier, verification_id, *, action="inspect"):
        self._verifier(verifier)
        if action not in {"inspect", "validate", "reconcile"}:
            raise ValueError("unknown bound verification observation")
        with self._scope() as context:
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
            return getattr(verifier, action)(
                record["parent_session_id"],
                verification_id,
                context=context,
                bound_parent_revision=record["parent_grant_revision"],
            )

    def _verifier(self, verifier):
        if (
            verifier.lanes is not self._service.lanes
            or verifier.store is not self._service.store
        ):
            raise PermissionError("verifier must use the exact bound lane service")

    def link_pending_verification(self, verifier, prepared, terminal_projection):
        self._verifier(verifier)
        codec = self._service.projection_codec
        if codec is None:
            raise PermissionError("trusted host projection codec unavailable")
        with self._scope() as context:
            supplied = codec.binding(terminal_projection)
            expected = replace(
                supplied,
                continuation_id=self.continuation_id,
                principal_id=context.principal_id,
                parent_session_id=prepared.parent_session_id,
                parent_grant_revision=prepared.parent_grant_revision,
                verification_id=prepared.verification_id,
                bundle_digest=prepared.bundle_digest,
                project_roots=prepared.roots,
            )
            sealed = seal_projection(codec, terminal_projection, expected)
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
                if (
                    record["parent_session_id"] != prepared.parent_session_id
                    or expected.host_conversation_id != record["host_conversation_id"]
                ):
                    raise PermissionError("host terminal conversation mismatch")
                value = tx.verification_row(
                    prepared.verification_id, context.principal_id
                )
                if (
                    value["prepared"] != prepared.approval_payload()
                    or value["state"] != "admitted"
                    or value["owner"]
                    or value["job_ids"]
                ):
                    raise PermissionError(
                        "terminal projection must precede approval/effect admission"
                    )
                identity = PendingVerificationIdentity(
                    record["id"],
                    prepared.verification_id,
                    prepared.parent_session_id,
                    prepared.parent_grant_revision,
                    prepared.generation,
                    prepared.bundle_digest,
                    value["command_id"],
                    sealed.sha256,
                    sealed.binding.revision,
                )
                prior = record.get("pending_verification")
                if prior is not None and prior != asdict(identity):
                    old = tx.verification_row(
                        prior["verification_id"], context.principal_id
                    )
                    if (
                        old["state"] not in {"failed", "stale"}
                        or old["owner"]
                        or old["job_ids"]
                    ):
                        raise ValueError(
                            "active pending verification identity is immutable"
                        )
                tx.link_terminal_projection(record["id"], context.principal_id, sealed)
                record["pending_verification"] = asdict(identity)
                self._service._save(tx, record)
                return identity

    def pending_verification(self):
        with self._scope():
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
                value = record.get("pending_verification")
                return PendingVerificationIdentity(**value) if value else None

    def prepared_verification(self, identity):
        """Read the original immutable bundle; never prepare against a new context."""
        with self._lock, self._service.store.transaction() as tx:
            record = self._service._row(tx, self.continuation_id)
            self._require_current_tx(tx, record, observe=True)
            if not isinstance(identity, PendingVerificationIdentity) or record.get(
                "pending_verification"
            ) != asdict(identity):
                raise PermissionError("exact original pending identity required")
            prepared, _ = self._service._linked_verification(tx, record)
            return prepared

    def terminal_projection(self, identity):
        with self._scope() as context:
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
                if not isinstance(identity, PendingVerificationIdentity) or record.get(
                    "pending_verification"
                ) != asdict(identity):
                    raise PermissionError("original pending identity mismatch")
                sealed = tx.terminal_projection(
                    record["id"], context.principal_id, identity.verification_id
                )
                if (
                    sealed.sha256 != identity.projection_digest
                    or sealed.binding.revision != identity.projection_revision
                ):
                    raise PermissionError("original projection digest mismatch")
            return open_projection(
                self._service.projection_codec, sealed, sealed.binding
            )

    def execute_verification(self, verifier, prepared, *, approve):
        self._verifier(verifier)
        with self._scope() as context:
            return verifier.execute_prepared(prepared, context=context, approve=approve)

    def commit_terminal_projection(self, identity, original_revision, host_result):
        from ..ports.delegated_verification import digest

        codec = self._service.terminal_result_codec
        if codec is None:
            raise PermissionError("trusted terminal result codec unavailable")
        original = self.terminal_projection(identity)
        original_binding = self._service.projection_codec.binding(original)
        if (
            type(original_revision) is not int
            or original_revision != original_binding.revision
        ):
            raise ValueError("original terminal projection revision mismatch")
        expected = replace(original_binding, revision=original_revision + 1)
        sealed = seal_projection(codec, host_result, expected)
        certificate_digest = codec.certificate_digest(host_result)
        with self._scope() as context:
            with self._service.store.transaction() as tx:
                record = self._service._row(tx, self.continuation_id)
                self._require_current_tx(tx, record)
                if record.get("pending_verification") != asdict(identity):
                    raise PermissionError("terminal result link changed")
                value = tx.verification_row(
                    identity.verification_id, context.principal_id
                )
                certificate = value.get("certificate")
                if (
                    value["state"] != "certified"
                    or not certificate
                    or digest(certificate) != certificate_digest
                    or certificate["bundle"]["bundle_digest"] != identity.bundle_digest
                    or tx.verification_generation(
                        identity.parent_session_id, context.principal_id
                    )
                    != identity.generation
                ):
                    raise PermissionError(
                        "terminal result certificate changed or unavailable"
                    )
                prior = tx.conn.execute(
                    "SELECT * FROM agent_lane_terminal_results WHERE continuation_id=? AND verification_id=?",
                    (self.continuation_id, identity.verification_id),
                ).fetchone()
                if prior:
                    if (
                        prior["principal"] != context.principal_id
                        or prior["original_digest"] != identity.projection_digest
                        or prior["payload"] != sealed.payload
                        or prior["digest"] != sealed.sha256
                        or prior["certificate_digest"] != certificate_digest
                        or prior["binding"] != _json(asdict(expected))
                    ):
                        raise ValueError("terminal result is immutable")
                    receipt = TerminalProjectionReceipt(**json.loads(prior["receipt"]))
                    if (
                        receipt.projection_digest != sealed.sha256
                        or receipt.original_projection_digest
                        != identity.projection_digest
                        or receipt.certificate_digest != certificate_digest
                        or receipt.revision != expected.revision
                    ):
                        raise ValueError("terminal result receipt integrity failure")
                    return receipt
                receipt = TerminalProjectionReceipt(
                    "terminal-receipt-" + uuid.uuid4().hex,
                    sealed.sha256,
                    identity.projection_digest,
                    certificate_digest,
                    expected.revision,
                )
                tx.conn.execute(
                    "INSERT INTO agent_lane_terminal_results VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        self.continuation_id,
                        identity.verification_id,
                        context.principal_id,
                        identity.projection_digest,
                        _json(asdict(expected)),
                        sealed.payload,
                        sealed.sha256,
                        certificate_digest,
                        _json(asdict(receipt)),
                    ),
                )
                return receipt


class LaneContinuationService:
    def __init__(
        self,
        lanes,
        *,
        authorize_host=None,
        projection_codec=None,
        command_codec=None,
        terminal_result_codec=None,
        model_writable_roots=None,
    ):
        self.lanes, self.store = lanes, lanes.store
        self.authorize_host, self.projection_codec = authorize_host, projection_codec
        self.command_codec = command_codec
        self.terminal_result_codec = terminal_result_codec
        self.model_writable_roots = model_writable_roots
        self._issuer = object()

    def _dispatch(self, record, action, payload, context):
        if not isinstance(payload, dict) or len(_json(payload).encode()) > 16384:
            raise ValueError("bounded host command required")
        if {
            "context",
            "author",
            "principal_id",
            "parent_session_id",
            "parent_lane_id",
        }.intersection(payload):
            raise PermissionError("host identity cannot be supplied by command")
        args = dict(payload)
        parent = record["parent_session_id"]
        lane_id = args.pop("lane_id", None)
        if lane_id:
            with self.store.transaction() as tx:
                require_root_admission(tx, self.store, parent, context)
                lane = tx.lane(lane_id)
                if lane["parent_session_id"] != parent:
                    raise PermissionError("host command targets another parent")
        if action == "spawn":
            return self.lanes.spawn(parent_session_id=parent, context=context, **args)
        if action == "list":
            return self.lanes.list(context, parent_session_id=parent, **args)
        if action == "reports":
            return self.lanes.reports(parent, context, **args)
        if action == "ack":
            return self.lanes.ack_report(
                context=context, parent_session_id=parent, **args
            )
        if not lane_id:
            raise ValueError("lane_id required")
        if action in {"inspect", "wait"}:
            return getattr(self.lanes, action)(lane_id, context, **args)
        if action == "send_message":
            return self.lanes.send_message(
                lane_id, context=context, author="parent", **args
            )
        if action in {"interrupt", "resume", "cancel"}:
            return self.lanes.control(
                lane_id, action, context=context, author="parent", **args
            )
        raise ValueError("unknown host lane command")

    @staticmethod
    def _row(tx, identity):
        row = tx.conn.execute(
            "SELECT data FROM agent_lane_continuations WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise KeyError("host continuation unavailable")
        return json.loads(row[0])

    @staticmethod
    def _save(tx, record):
        tx.conn.execute(
            "UPDATE agent_lane_continuations SET data=? WHERE id=?",
            (_json(record), record["id"]),
        )

    @staticmethod
    def _linked_verification(tx, record):
        from ..ports.delegated_verification import PreparedVerification, digest

        identity = PendingVerificationIdentity(**record["pending_verification"])
        if (
            identity.continuation_id != record["id"]
            or identity.parent_session_id != record["parent_session_id"]
            or identity.parent_grant_revision != record["parent_grant_revision"]
        ):
            raise PermissionError("pending recovery identity scope mismatch")
        value = tx.verification_row(identity.verification_id, record["principal_id"])
        prepared = PreparedVerification.from_payload(value["prepared"])
        if (
            prepared.principal_id != record["principal_id"]
            or prepared.parent_session_id != identity.parent_session_id
            or prepared.verification_id != identity.verification_id
            or value["command_id"] != identity.command_id
            or prepared.parent_grant_revision != identity.parent_grant_revision
            or prepared.generation != identity.generation
            or prepared.bundle_digest != identity.bundle_digest
            or digest(replace(prepared, bundle_digest="").approval_payload())
            != identity.bundle_digest
        ):
            raise ValueError("original prepared verification integrity mismatch")
        sealed = tx.terminal_projection(
            record["id"], record["principal_id"], identity.verification_id
        )
        if (
            sealed.sha256 != identity.projection_digest
            or sealed.binding.host_conversation_id != record["host_conversation_id"]
            or sealed.binding.parent_session_id != identity.parent_session_id
            or sealed.binding.parent_grant_revision != identity.parent_grant_revision
            or sealed.binding.revision != identity.projection_revision
            or sealed.binding.bundle_digest != identity.bundle_digest
            or sealed.binding.project_roots != prepared.roots
        ):
            raise ValueError("original projection link integrity mismatch")
        return prepared, value

    def _recovery_verification(self, tx, record):
        if not record.get("pending_verification"):
            return "", "", None, None
        try:
            _, value = self._linked_verification(tx, record)
            identity = PendingVerificationIdentity(**record["pending_verification"])
            phase, code = value["state"], value["code"]
            if phase not in {
                "admitted",
                "approval_deciding",
                "approval_pending",
                "approved",
                "running",
                "certified",
                "failed",
                "stale",
                "incomplete",
                "approval_unknown",
            }:
                raise ValueError("unknown recovery phase")
            if code not in {
                "",
                "APPROVAL_PENDING",
                "APPROVAL_OUTCOME_UNKNOWN",
                "VERIFICATION_REFUSED",
                "CLEANUP_UNRESOLVED",
                "PENDING_BUNDLE_CHANGED",
                "RECOVERED_INCOMPLETE",
            }:
                raise ValueError("unknown recovery code")
            evidence = value.get("pending_approval")
            pending = PendingApprovalEvidence(**evidence) if evidence else None
            return phase, code, identity, pending
        except (ValueError, TypeError, KeyError, PermissionError):
            return "unavailable", "RECOVERY_METADATA_UNAVAILABLE", None, None

    def _grant(self, context, host_id):
        if (
            self.authorize_host is None
            or context.expired
            or context.cancellation.cancelled
        ):
            raise PermissionError("live host authorizer unavailable")
        if not callable(self.model_writable_roots):
            raise PermissionError("complete model-writable root provider unavailable")
        configured_roots = tuple(self.model_writable_roots())
        if len(configured_roots) > 256:
            raise PermissionError("configured model root inventory exceeds bound")
        private_directory = Path(self.store.path).parent.resolve()
        if any(
            private_directory.is_relative_to(Path(root).resolve())
            for root in (*configured_roots, *context.workspace_roots)
        ):
            raise PermissionError(
                "host continuation store overlaps model-writable scope"
            )
        grant = self.authorize_host(context, host_id)
        if (
            not isinstance(grant, HostContinuationGrant)
            or grant.principal_id != context.principal_id
            or grant.host_conversation_id != host_id
            or type(grant.revision) is not int
            or grant.revision < 1
            or not math.isfinite(grant.expires_at)
            or grant.expires_at <= time.time()
        ):
            raise PermissionError("host grant mismatch or expiry")
        for value in (grant.principal_id, grant.host_conversation_id, grant.grant_id):
            _text(value)
        roots = grant.workspace_roots
        if (
            not isinstance(roots, tuple)
            or not 1 <= len(roots) <= 16
            or roots != tuple(sorted(set(roots)))
            or len(grant.allowed_tools) > 64
        ):
            raise PermissionError("host grant bounds invalid")
        for root in roots:
            if not isinstance(root, str) or len(root.encode()) > 4096:
                raise PermissionError("host root exceeds byte bound")
            path = Path(root)
            if (
                not path.is_absolute()
                or str(path.resolve()) != root
                or not path.is_dir()
            ):
                raise PermissionError("host root is unavailable or noncanonical")
            if not any(
                path.is_relative_to(p.resolve()) for p in context.workspace_roots
            ):
                raise PermissionError("host root exceeds current context")
        if not set(grant.allowed_tools).issubset(self.lanes.allowed_tools):
            raise PermissionError("host tool grant exceeds current policy")
        return grant

    def _current(self, tx, record, context, *, observe=False):
        grant = self._grant(context, record["host_conversation_id"])
        now = time.time()
        if (
            record["principal_id"] != context.principal_id
            or record["auth_level"] != context.auth_level
            or now < record["last_wall"]
            or now >= record["expires_at"]
            or grant.grant_id != record["grant_id"]
            or grant.revision != record["grant_revision"]
            or not set(record["workspace_roots"]).issubset(grant.workspace_roots)
            or not set(record["allowed_tools"]).issubset(grant.allowed_tools)
            or record["cloud_allowed"]
            and not context.cloud_allowed
            or record["remote_allowed"]
            and not context.remote_ollama_allowed
        ):
            raise PermissionError("original host authority is no longer current")
        parent = tx.conn.execute(
            "SELECT * FROM agent_lane_parent_grants WHERE session_id=?",
            (record["parent_session_id"],),
        ).fetchone()
        if (
            parent is None
            or parent["principal"] != context.principal_id
            or parent["revoked"]
            or parent["revision"] != record["parent_grant_revision"]
            or parent["expires"] <= now
        ):
            raise PermissionError("original parent grant revoked or expired")
        if not observe:
            record["last_wall"] = now
            self._save(tx, record)
        return grant

    def register_parent(
        self,
        parent_session_id,
        parent_token,
        host_conversation_id,
        *,
        context,
        command_id,
    ):
        _text(command_id)
        grant = self._grant(context, host_conversation_id)
        remaining = context.remaining_seconds
        if remaining is None or not math.isfinite(remaining) or remaining <= 0:
            raise PermissionError("bounded original host deadline required")
        owner = "lane-owner-" + uuid.uuid4().hex
        lease = self.store.acquire_owner(owner)
        try:
            with self.store.transaction() as tx:
                if (
                    tx.conn.execute(
                        "SELECT COUNT(*) FROM agent_lane_continuations WHERE principal=?",
                        (context.principal_id,),
                    ).fetchone()[0]
                    >= 1024
                ):
                    raise ValueError("retained host continuation capacity reached")
                if tx.conn.execute(
                    "SELECT 1 FROM agent_lane_continuations WHERE parent_session=? OR (principal=? AND host_conversation=?)",
                    (parent_session_id, context.principal_id, host_conversation_id),
                ).fetchone():
                    raise PermissionError(
                        "registered root requires explicit reattachment"
                    )
                parent = self.store._verify_parent(
                    tx, parent_session_id, parent_token, context.principal_id
                )
                children = tx.verification_children(
                    parent_session_id, context.principal_id
                )
                for child in children:
                    if not set(child["allowed_tools"]).issubset(
                        grant.allowed_tools
                    ) or not any(
                        Path(child["workspace_root"]).is_relative_to(Path(root))
                        for root in grant.workspace_roots
                    ):
                        raise PermissionError(
                            "host grant does not cover existing child ceiling"
                        )
                now = time.time()
                expires = min(
                    [now + remaining, parent["expires"], grant.expires_at]
                    + [c["grant_expires"] for c in children]
                )
                if expires <= now:
                    raise PermissionError("original child grant expired")
                record = dict(
                    id="continuation-" + uuid.uuid4().hex,
                    principal_id=context.principal_id,
                    parent_session_id=parent_session_id,
                    parent_grant_revision=parent["revision"],
                    host_conversation_id=host_conversation_id,
                    grant_id=grant.grant_id,
                    grant_revision=grant.revision,
                    workspace_roots=list(grant.workspace_roots),
                    allowed_tools=list(grant.allowed_tools),
                    auth_level=context.auth_level,
                    cloud_allowed=context.cloud_allowed,
                    remote_allowed=context.remote_ollama_allowed,
                    expires_at=expires,
                    last_wall=now,
                    epoch=1,
                    owner=owner,
                    command_id=command_id,
                    attachment_state="active",
                )
                tx.conn.execute(
                    "INSERT INTO agent_lane_continuations(id,principal,parent_session,host_conversation,data) VALUES (?,?,?,?,?)",
                    (
                        record["id"],
                        context.principal_id,
                        parent_session_id,
                        host_conversation_id,
                        _json(record),
                    ),
                )
            return BoundContinuation(self, record, context, lease, self._issuer)
        except BaseException:
            lease.close()
            raise

    def select(self, continuation_id, context):
        _text(continuation_id)
        with self.store.transaction() as tx:
            record = self._row(tx, continuation_id)
            self._current(tx, record, context)
        return ContinuationSelection(
            continuation_id, context.principal_id, self._issuer
        )

    def prepare_reattachment(self, selection, context, *, command_id):
        _text(command_id)
        if (
            not isinstance(selection, ContinuationSelection)
            or selection.issuer is not self._issuer
            or selection.principal_id != context.principal_id
        ):
            raise PermissionError("private host selection required")
        with self.store.transaction() as tx:
            record = self._row(tx, selection.continuation_id)
            self._current(tx, record, context)
            if record.get("attachment_state") == "approval_pending":
                if record["attachment_prepared"]["command_id"] != command_id:
                    raise ValueError("pending attachment command identity changed")
                return self._restore_prepared(record["attachment_prepared"])
            if record.get("attachment_state") == "approval_deciding":
                raise PermissionError("attachment approval outcome unresolved")
        return self._prepared(record, command_id)

    def _restore_prepared(self, value):
        value = dict(value)
        value["workspace_roots"] = tuple(value["workspace_roots"])
        value["allowed_tools"] = tuple(value["allowed_tools"])
        return PreparedReattachment(**value, issuer=self._issuer)

    def _prepared(self, record, command_id):
        return PreparedReattachment(
            record["id"],
            record["parent_session_id"],
            record["parent_grant_revision"],
            record["principal_id"],
            record["host_conversation_id"],
            record["grant_id"],
            record["grant_revision"],
            record["epoch"],
            record["expires_at"],
            tuple(record["workspace_roots"]),
            tuple(record["allowed_tools"]),
            command_id,
            self._issuer,
        )

    def execute_reattachment(self, prepared, context, *, approve):
        if (
            not isinstance(prepared, PreparedReattachment)
            or prepared.issuer is not self._issuer
        ):
            raise PermissionError("private prepared host attachment required")
        owner = "lane-owner-" + uuid.uuid4().hex
        lease = self.store.acquire_owner(owner)
        try:
            with self.store.transaction() as tx:
                record = self._row(tx, prepared.continuation_id)
                self._current(tx, record, context)
                expected = (
                    record["attachment_prepared"]
                    if record.get("attachment_state") == "approval_pending"
                    else self._prepared(record, prepared.command_id).approval_payload()
                )
                if expected != prepared.approval_payload():
                    raise PermissionError("prepared host attachment changed")
                if record.get(
                    "attachment_state"
                ) == "approval_deciding" or not self.store.owner_definitely_stopped(
                    record["owner"]
                ):
                    raise PermissionError("host owner remains live or unknown")
                if record["epoch"] >= 128:
                    raise ValueError("host attachment attempt budget exhausted")
                if record.get("attachment_state") == "active":
                    from ..ports.delegated_verification import digest

                    history = record.setdefault("attachment_history", [])
                    if len(history) >= 32:
                        raise ValueError("retained host attachment capacity reached")
                    if record.get("attachment_approval"):
                        history.append(
                            dict(
                                epoch=record["epoch"],
                                command_id=record["attachment_prepared"]["command_id"],
                                prepared_digest=digest(record["attachment_prepared"]),
                                approval=record["attachment_approval"],
                            )
                        )
                    record.pop("attachment_pending", None)
                    record.pop("attachment_approval", None)
                # Claim before an approval callback so concurrent callers cannot spend twice.
                record.update(
                    owner=owner,
                    epoch=record["epoch"] + 1,
                    attachment_state="approval_deciding",
                    attachment_prepared=expected,
                )
                self._save(tx, record)
            try:
                approval = approve(prepared, context)
            except VerificationApprovalPending as pending:
                if pending.evidence.expires_at <= time.time():
                    raise PermissionError("pending attachment ledger record expired")
                evidence = asdict(pending.evidence)
                with self.store.transaction() as tx:
                    current = self._row(tx, prepared.continuation_id)
                    self._current(tx, current, context)
                    if current["owner"] != owner or current["epoch"] != record["epoch"]:
                        raise PermissionError("host attachment claim changed")
                    prior = current.get("attachment_pending")
                    if prior and any(
                        prior[k] != evidence[k]
                        for k in ("tool", "call_digest", "surface", "call_id")
                    ):
                        raise PermissionError(
                            "pending attachment ledger identity changed"
                        )
                    current.update(
                        attachment_state="approval_pending", attachment_pending=evidence
                    )
                    self._save(tx, current)
                raise
            if (
                not isinstance(approval, GrantedApprovalEvidence)
                or approval.expires_at <= time.time()
            ):
                raise PermissionError("typed exact host reattachment approval required")
            with self.store.transaction() as tx:
                current = self._row(tx, prepared.continuation_id)
                self._current(tx, current, context)
                if current["owner"] != owner or current["epoch"] != record["epoch"]:
                    raise PermissionError("host attachment claim changed")
                prior = current.get("attachment_pending")
                if prior and any(
                    prior[k] != getattr(approval, k)
                    for k in ("tool", "call_digest", "surface")
                ):
                    raise PermissionError(
                        "attachment approval does not cover pending identity"
                    )
                current.update(
                    attachment_state="active", attachment_approval=asdict(approval)
                )
                self._save(tx, current)
            return BoundContinuation(self, current, context, lease, self._issuer)
        except BaseException:
            lease.close()
            raise

    def recovery_page(self, context, *, cursor=0, limit=32):
        if (
            type(cursor) is not int
            or cursor < 0
            or type(limit) is not int
            or not 1 <= limit <= 128
        ):
            raise ValueError("recovery page outside bounds")
        if (
            self.authorize_host is None
            or context.expired
            or context.cancellation.cancelled
        ):
            raise PermissionError("live host authorizer unavailable")
        items = []
        with self.store.transaction() as tx:
            rows = tx.conn.execute(
                "SELECT position,data FROM agent_lane_continuations WHERE principal=? AND position>? ORDER BY position LIMIT ?",
                (context.principal_id, cursor, limit + 1),
            ).fetchall()
            for row in rows[:limit]:
                record = json.loads(row["data"])
                if record["principal_id"] != context.principal_id:
                    raise PermissionError("stored recovery scope mismatch")
                grant = self._grant(context, record["host_conversation_id"])
                # Read-only recovery visibility is fresh host authorization, not
                # authority to resume an expired original grant.
                parent = tx.conn.execute(
                    "SELECT revision,expires,revoked FROM agent_lane_parent_grants WHERE session_id=? AND principal=?",
                    (record["parent_session_id"], context.principal_id),
                ).fetchone()
                authority = "current"
                if (
                    parent is None
                    or parent["revoked"]
                    or parent["expires"] <= time.time()
                    or record["expires_at"] <= time.time()
                    or parent["revision"] != record["parent_grant_revision"]
                    or grant.grant_id != record["grant_id"]
                    or grant.revision != record["grant_revision"]
                ):
                    authority = "requires_reauthorization"
                state = (
                    "stopped"
                    if self.store.owner_definitely_stopped(record["owner"])
                    else "live_or_unknown"
                )
                phase, code, identity, pending = self._recovery_verification(tx, record)
                attachment = record.get("attachment_state", "unavailable")
                if attachment not in {
                    "active",
                    "approval_deciding",
                    "approval_pending",
                }:
                    attachment = "unavailable"
                items.append(
                    RecoveryItem(
                        record["id"],
                        record["parent_session_id"],
                        record["epoch"],
                        state,
                        record["expires_at"],
                        authority,
                        attachment,
                        phase,
                        code,
                        identity,
                        pending,
                    )
                )
        return RecoveryPage(
            tuple(items),
            rows[min(len(rows), limit) - 1]["position"] if rows else cursor,
            len(rows) > limit,
        )
