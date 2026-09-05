"""Local delegated verification with durable steering generation and dispatch barrier."""

from dataclasses import replace
import json
from pathlib import Path
import time
import uuid
from ..ports.delegated_verification import (
    PreparedVerification,
    VerificationVerdict,
    _PreparedCheckPermit,
    canonical,
    digest,
)


class _VerificationCancellation:
    def __init__(self, service, prepared, original):
        self.service, self.prepared, self.original = service, prepared, original

    @property
    def cancelled(self):
        if self.original.cancellation.cancelled or self.original.expired:
            return True
        try:
            self.service._require_current(self.prepared, self.original)
        except (ValueError, PermissionError, OSError, KeyError):
            return True
        return False

    def wait(self, timeout=None):
        until = time.monotonic() + (timeout or 0)
        while not self.cancelled and time.monotonic() < until:
            time.sleep(min(0.05, max(0, until - time.monotonic())))
        return self.cancelled


class DelegatedVerificationService:
    def __init__(self, lanes, verifier_gateway, process_evidence, snapshotter):
        self.lanes, self.store = lanes, lanes.store
        self.gateway, self.process_evidence, self.snapshotter = (
            verifier_gateway,
            process_evidence,
            snapshotter,
        )
        self._issuer = object()
        if callable(getattr(self.gateway, "bind_issuer", None)):
            self.gateway.bind_issuer(self._issuer)

    @staticmethod
    def _context_fingerprint(context):
        return digest(
            dict(
                principal=context.principal_id,
                auth=context.auth_level,
                source=context.source,
                correlation=context.correlation_id,
                roots=[str(p) for p in context.workspace_roots],
                deadline=context.deadline_monotonic,
                cloud=context.cloud_allowed,
                remote=context.remote_ollama_allowed,
            )
        )

    def _parent(self, tx, parent, context, revision):
        if context.expired or context.cancellation.cancelled:
            raise PermissionError("verification authority expired or cancelled")
        row = tx.conn.execute(
            "SELECT * FROM agent_lane_parent_grants WHERE session_id=?", (parent,)
        ).fetchone()
        if (
            row is None
            or row["principal"] != context.principal_id
            or row["revoked"]
            or row["revision"] != revision
            or row["expires"] <= time.time()
        ):
            raise PermissionError("verification parent capability is not current")
        tx.verification_generation(parent, context.principal_id)

    def _children(self, tx, parent, context):
        children = tx.verification_children(parent, context.principal_id)
        if not children:
            raise ValueError("delegated verification needs children")
        signatures, jobs = [], []
        for lane in children:
            self.lanes._authorize(lane, context, execute=True)
            if (
                lane["status"]
                not in {"completed", "cancelled", "failed", "interrupted"}
                or lane["owner"]
                or lane["pending_effect"]
                or lane.get("pending_response")
            ):
                raise ValueError("child is not quiescent")
            queued = tx.conn.execute(
                "SELECT 1 FROM agent_lane_messages WHERE lane_id=? AND report=0 AND delivery_state=? LIMIT 1",
                (lane["id"], "queued"),
            ).fetchone()
            if queued:
                raise ValueError("child has queued instruction")
            sequence = tx.conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM agent_lane_messages WHERE lane_id=? AND report=0",
                (lane["id"],),
            ).fetchone()[0]
            signatures.append((lane["id"], lane["revision"], sequence))
            rows = tx.conn.execute(
                "SELECT json_extract(payload, '$.name'), json_extract(payload, '$.call_id') FROM agent_lane_events WHERE lane_id=? AND event_type='tool.requested' ORDER BY sequence LIMIT 4097",
                (lane["id"],),
            ).fetchall()
            if len(rows) > 4096:
                raise ValueError("child process evidence bound exceeded")
            for row in rows:
                if row[0] == "run_tests":
                    jobs.append(("lane-test-" + row[1], lane["session_id"]))
        roots = tuple(sorted({lane["workspace_root"] for lane in children}))
        if len(roots) > 16:
            raise ValueError("verification root bound exceeded")
        return tuple(signatures), roots, jobs

    def _proof(self, job, parent, principal):
        proof = self.process_evidence(job)
        if (
            not proof
            or proof.get("job_id") != job
            or proof.get("parent_session_id") != parent
            or proof.get("principal_id") != principal
            or not proof.get("digest")
            or any(
                proof.get(k) is not True
                for k in ("process_exited", "containment_empty", "resources_released")
            )
        ):
            raise ValueError("process cleanup proof unavailable")
        return proof

    def prepare(self, parent_session_id, *, command_id, context, bound_parent_revision):
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            raise ValueError("verification command_id must be bounded")
        if self.gateway is None or not callable(self.process_evidence):
            raise PermissionError("delegated verifier unavailable")
        verification_id = "verification-" + uuid.uuid4().hex
        with self.store.transaction() as tx:
            self._parent(tx, parent_session_id, context, bound_parent_revision)
            prior = tx.conn.execute(
                "SELECT data FROM agent_verifications WHERE principal=? AND command_id=?",
                (context.principal_id, command_id),
            ).fetchone()
            if prior:
                value = json.loads(prior[0])
                prepared = PreparedVerification.from_payload(value["prepared"])
                if (
                    prepared.parent_session_id != parent_session_id
                    or prepared.parent_grant_revision != bound_parent_revision
                    or prepared.context_fingerprint
                    != self._context_fingerprint(context)
                ):
                    raise ValueError("verification command replay conflict")
                return prepared
            signatures, roots, jobs = self._children(tx, parent_session_id, context)
            for job, parent in jobs:
                self._proof(job, parent, context.principal_id)
            generation = tx.verification_generation(
                parent_session_id, context.principal_id
            )
            checks = self.gateway.prepare_checks(roots)
            if tuple(sorted(c.workspace_root for c in checks)) != roots:
                raise ValueError(
                    "independent catalog checks must cover all affected roots exactly"
                )
            value = dict(
                verification_id=verification_id,
                parent_session_id=parent_session_id,
                principal_id=context.principal_id,
                parent_grant_revision=bound_parent_revision,
                generation=generation,
                children=signatures,
                roots=roots,
                checks=checks,
                context_fingerprint=self._context_fingerprint(context),
                bundle_digest="",
            )
            prepared = PreparedVerification(**value)
            prepared = replace(
                prepared, bundle_digest=digest(prepared.approval_payload())
            )
            record = dict(
                verification_id=verification_id,
                parent_session_id=parent_session_id,
                principal_id=context.principal_id,
                command_id=command_id,
                prepared=prepared.approval_payload(),
                generation=generation,
                state="admitted",
                code="",
                job_ids=[],
                certificate=None,
                owner="",
            )
            tx.acquire_verification_barrier(
                parent_session_id, context.principal_id, verification_id
            )
            tx.conn.execute(
                "INSERT INTO agent_verifications VALUES (?,?,?,?,?)",
                (
                    verification_id,
                    parent_session_id,
                    context.principal_id,
                    command_id,
                    canonical(record),
                ),
            )
        return prepared

    def _require_current(self, prepared, context, *, exact_context=True):
        if (
            exact_context
            and self._context_fingerprint(context) != prepared.context_fingerprint
        ):
            raise PermissionError("prepared verification context changed")
        self.gateway.require_current(prepared.checks)
        with self.store.transaction() as tx:
            self._parent(
                tx, prepared.parent_session_id, context, prepared.parent_grant_revision
            )
            if (
                tx.verification_generation(
                    prepared.parent_session_id, context.principal_id
                )
                != prepared.generation
            ):
                raise ValueError("verification steering generation changed")
            signatures, roots, jobs = self._children(
                tx, prepared.parent_session_id, context
            )
            if signatures != prepared.children or roots != prepared.roots:
                raise ValueError("verification child snapshot changed")
        for job, parent in jobs:
            self._proof(job, parent, context.principal_id)

    def _record(self, prepared, context):
        with self.store.transaction() as tx:
            self._parent(
                tx, prepared.parent_session_id, context, prepared.parent_grant_revision
            )
            value = tx.verification_row(prepared.verification_id, context.principal_id)
        if value["prepared"] != prepared.approval_payload():
            raise PermissionError("prepared verification binding changed")
        return value

    def execute_prepared(self, prepared, *, context, approve):
        value = self._record(prepared, context)
        if value["state"] in {"certified", "failed", "stale", "incomplete"}:
            return self._public(value)
        owner = "lane-owner-" + uuid.uuid4().hex
        owner_lease = self.store.acquire_owner(owner)
        with self.store.transaction() as tx:
            value = tx.verification_row(prepared.verification_id, context.principal_id)
            if value["owner"]:
                owner_lease.close()
                return self._public(value)
            value["owner"] = owner
            tx.save_verification(value)
        try:
            self._require_current(prepared, context)
            approval_id = approve(prepared, context)
            if not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 256:
                raise PermissionError("independent exact approval is required")
            self._require_current(prepared, context)
            before = self.snapshotter.capture(tuple(Path(p) for p in prepared.roots))
            with self.store.transaction() as tx:
                value = tx.verification_row(
                    prepared.verification_id, context.principal_id
                )
                value.update(
                    approval_id=approval_id,
                    before_manifest=before.digest,
                    state="running",
                )
                tx.save_verification(value)
            control = _VerificationCancellation(self, prepared, context)
            execution_context = replace(
                context,
                cancellation=control,
                deadline_monotonic=min(
                    context.deadline_monotonic or float("inf"), time.monotonic() + 600
                ),
            )
            proofs = []
            for index, check in enumerate(prepared.checks):
                self._require_current(prepared, context)
                call_id = prepared.verification_id + "-" + str(index)
                job_id = "lane-test-" + call_id
                with self.store.transaction() as tx:
                    value = tx.verification_row(
                        prepared.verification_id, context.principal_id
                    )
                    if (
                        tx.verification_generation(
                            prepared.parent_session_id, context.principal_id
                        )
                        != prepared.generation
                    ):
                        raise ValueError("verification steering generation changed")
                    value["job_ids"].append(job_id)
                    tx.save_verification(value)
                self.gateway.execute_check(
                    check,
                    call_id,
                    prepared.parent_session_id,
                    execution_context,
                    permit=_PreparedCheckPermit(
                        prepared, approval_id, check, call_id, self._issuer
                    ),
                )
                proof = self._proof(
                    job_id, prepared.parent_session_id, context.principal_id
                )
                proofs.append(proof)
                if proof["status"] != "succeeded" or proof["exit_code"] != 0:
                    raise ValueError("independent check failed")
            after = self.snapshotter.capture(tuple(Path(p) for p in prepared.roots))
            if before != after:
                raise ValueError("source manifest changed during independent checks")
            self._require_current(prepared, context)
            with self.store.transaction() as tx:
                self._parent(
                    tx,
                    prepared.parent_session_id,
                    context,
                    prepared.parent_grant_revision,
                )
                if (
                    tx.verification_generation(
                        prepared.parent_session_id, context.principal_id
                    )
                    != prepared.generation
                ):
                    raise ValueError("verification steering generation changed")
                value = tx.verification_row(
                    prepared.verification_id, context.principal_id
                )
                value.update(
                    state="certified",
                    owner="",
                    code="",
                    certificate=dict(
                        id=prepared.verification_id,
                        bundle=prepared.approval_payload(),
                        approval_id=approval_id,
                        before_manifest_digest=before.digest,
                        after_manifest_digest=after.digest,
                        manifest_policy=json.loads(before.policy_json),
                        cleanup_proofs=proofs,
                        created_at=time.time(),
                    ),
                )
                tx.save_verification(value)
                tx.release_verification_barrier(
                    prepared.parent_session_id,
                    context.principal_id,
                    prepared.verification_id,
                )
        except Exception as exc:
            with self.store.transaction() as tx:
                value = tx.verification_row(
                    prepared.verification_id, context.principal_id
                )
                if value["state"] == "certified":
                    return self._public(value)
                clean = True
                for job_id in value["job_ids"]:
                    try:
                        self._proof(
                            job_id, prepared.parent_session_id, context.principal_id
                        )
                    except (ValueError, KeyError, OSError):
                        clean = False
                value.update(
                    state="failed" if clean else "incomplete",
                    owner="",
                    code="VERIFICATION_REFUSED" if clean else "CLEANUP_UNRESOLVED",
                )
                tx.save_verification(value)
                if clean:
                    tx.release_verification_barrier(
                        prepared.parent_session_id,
                        context.principal_id,
                        prepared.verification_id,
                    )
        finally:
            owner_lease.close()
        if value["state"] != "incomplete":
            self.lanes.resume_after_verification(prepared.parent_session_id)
        return self._public(value)

    @staticmethod
    def _public(value):
        return {
            k: value[k]
            for k in (
                "verification_id",
                "parent_session_id",
                "state",
                "generation",
                "code",
                "certificate",
                "job_ids",
            )
        }

    def inspect(
        self, parent_session_id, verification_id, *, context, bound_parent_revision
    ):
        with self.store.transaction() as tx:
            self._parent(tx, parent_session_id, context, bound_parent_revision)
            value = tx.verification_row(verification_id, context.principal_id)
            if value["parent_session_id"] != parent_session_id:
                raise PermissionError("wrong verification parent")
        return self._public(value)

    def validate(
        self, parent_session_id, verification_id, *, context, bound_parent_revision
    ):
        try:
            value = self.inspect(
                parent_session_id,
                verification_id,
                context=context,
                bound_parent_revision=bound_parent_revision,
            )
            if value["state"] != "certified":
                return VerificationVerdict(False, value["code"] or "NOT_CERTIFIED")
            prepared = PreparedVerification.from_payload(value["certificate"]["bundle"])
            self._require_current(prepared, context, exact_context=False)
            manifest = self.snapshotter.capture(tuple(Path(p) for p in prepared.roots))
            if manifest.digest != value["certificate"]["after_manifest_digest"]:
                raise ValueError("source manifest changed")
            self._require_current(prepared, context, exact_context=False)
            return VerificationVerdict(
                True,
                "CERTIFIED",
                verification_id,
                prepared.generation,
                prepared.parent_session_id,
                prepared.parent_grant_revision,
                prepared.roots,
                prepared.children,
            )
        except (ValueError, PermissionError, KeyError, OSError):
            return VerificationVerdict(False, "STALE_OR_UNAVAILABLE", verification_id)

    def reconcile(
        self, parent_session_id, verification_id, *, context, bound_parent_revision
    ):
        """Resolve only cleanup; never rerun unknown effects or mint crash success."""
        self.inspect(
            parent_session_id,
            verification_id,
            context=context,
            bound_parent_revision=bound_parent_revision,
        )
        with self.store.transaction() as tx:
            value = tx.verification_row(verification_id, context.principal_id)
            if value["state"] in {"certified", "failed", "stale"}:
                return self._public(value)
            # Kernel owner-lock evidence, never lease expiry, proves a controller stopped.
            if value["owner"]:
                if not self.store.owner_definitely_stopped(value["owner"]):
                    return self._public(value)
            for job_id in value["job_ids"]:
                self._proof(job_id, parent_session_id, context.principal_id)
            value.update(state="failed", code="RECOVERED_INCOMPLETE", owner="")
            tx.save_verification(value)
            tx.release_verification_barrier(
                parent_session_id, context.principal_id, verification_id
            )
        self.lanes.resume_after_verification(parent_session_id)
        return self._public(value)
