"""Private bounded turn transitions under an existing live root attachment."""

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json

from ..ports.delegated_verification import digest
from ..ports.host_final import HostFinalFacts
from ..ports.host_turn_links import ManagedHostTurnLink, ManagedHostTerminalLink, FinalizedHostResult, ManagedHostFinalEvidence
from ..ports.lane_continuation import (
    ProjectionBinding,
    open_projection,
    seal_projection,
)


@dataclass(frozen=True)
class HostTurnAdmission:
    run_id: str
    ordinal: int
    previous_projection: object
    owner: object = field(repr=False, compare=False)


def _stored_projection(bound, tx, record, turn):
    sealed = tx.terminal_projection(
        record["id"], record["principal_id"], turn["projection_id"]
    )
    binding = sealed.binding
    if (
        sealed.sha256 != turn["projection_digest"]
        or binding.continuation_id != record["id"]
        or binding.principal_id != record["principal_id"]
        or binding.host_conversation_id != record["host_conversation_id"]
        or binding.parent_session_id != record["parent_session_id"]
        or binding.parent_grant_revision != record["parent_grant_revision"]
        or binding.run_id != turn["run_id"]
    ):
        raise PermissionError("host turn projection scope changed")
    return open_projection(bound._service.projection_codec, sealed, binding)


def advance_host_turn(bound, run_id, *, verifier=None):
    if not isinstance(run_id, str) or not 1 <= len(run_id.encode()) <= 128:
        raise ValueError("bounded host turn identity required")
    pending = bound.pending_verification()
    verdict = None
    if pending is not None:
        if verifier is None:
            raise PermissionError("original verification requires explicit recovery")
        verdict = bound.verification_view(
            verifier, pending.verification_id, action="validate"
        )
        if not verdict.valid or verdict.code != "CERTIFIED":
            raise PermissionError(
                "original verification is pending, unknown or no longer valid"
            )
    with bound._scope() as context, bound._service._transaction(context) as tx:
        record = bound._service._row(tx, bound.continuation_id)
        bound._require_current_tx(tx, record, context=context)
        if record.get("pending_verification") != (asdict(pending) if pending else None):
            raise PermissionError("host pending identity changed")
        prior = record.get("host_turn")
        history = record.setdefault("host_turn_history", [])
        if len(history) >= 31:
            raise ValueError("retained host turn limit reached")
        if prior and prior["run_id"] == run_id:
            if prior["state"] != "active":
                raise PermissionError("closed host turn cannot be replayed")
            previous = (
                _stored_projection(bound, tx, record, history[-1]) if history else None
            )
            return HostTurnAdmission(run_id, prior["ordinal"], previous, bound)
        if prior and prior["state"] != "closed":
            raise PermissionError(
                "previous host turn lacks a durable terminal boundary"
            )
        if prior:
            _stored_final(bound, tx, record, prior)
        if any(item["run_id"] == run_id for item in history):
            raise PermissionError("historical host turn cannot be replayed")
        parent = record["parent_session_id"]
        tx.require_verification_workspace_quiescence(
            parent, context.principal_id, record["workspace_roots"]
        )
        if tx.verification_barrier(parent, context.principal_id):
            raise PermissionError("verification still owns the workspace")
        children = tx.verification_children(parent, context.principal_id)
        if children and pending is None:
            raise PermissionError(
                "child work requires completed verification before another turn"
            )
        previous = _stored_projection(bound, tx, record, prior) if prior else None
        retained = dict(prior) if prior else None
        if pending:
            if prior is None:
                raise PermissionError("completed host turn identity unavailable")
            value = tx.verification_row(pending.verification_id, context.principal_id)
            certificate = value.get("certificate")
            linked = tx.terminal_projection(
                record["id"], context.principal_id, pending.verification_id
            )
            receipt_row = tx.conn.execute(
                "SELECT * FROM agent_lane_terminal_results WHERE continuation_id=? AND verification_id=?",
                (record["id"], pending.verification_id),
            ).fetchone()
            if (
                value["state"] != "certified"
                or value["owner"]
                or not certificate
                or linked.binding.run_id != prior["run_id"]
                or linked.sha256 != pending.projection_digest
                or tx.verification_generation(parent, context.principal_id)
                != pending.generation
                or verdict.generation != pending.generation
                or verdict.parent_session_id != parent
                or verdict.parent_grant_revision != record["parent_grant_revision"]
                or receipt_row is None
                or receipt_row["principal"] != context.principal_id
                or receipt_row["original_digest"] != pending.projection_digest
                or receipt_row["certificate_digest"] != digest(certificate)
                or hashlib.sha256(receipt_row["payload"]).hexdigest()
                != receipt_row["digest"]
            ):
                raise PermissionError(
                    "completed terminal receipt or cleanup evidence changed"
                )
            receipt = json.loads(receipt_row["receipt"])
            expected_binding = replace(
                linked.binding, revision=linked.binding.revision + 1
            )
            if (
                receipt["projection_digest"] != receipt_row["digest"]
                or receipt["original_projection_digest"] != pending.projection_digest
                or receipt["certificate_digest"] != receipt_row["certificate_digest"]
                or receipt["revision"] != expected_binding.revision
                or json.loads(receipt_row["binding"])
                != json.loads(json.dumps(asdict(expected_binding)))
            ):
                raise PermissionError("terminal receipt integrity failure")
            linked_original = open_projection(
                bound._service.projection_codec, linked, linked.binding
            )
            if any(
                getattr(previous, field) != getattr(linked_original, field)
                for field in ("output", "ledger_bytes", "terminal_class", "blockers")
            ):
                raise PermissionError(
                    "certified result does not cover the captured host turn"
                )
            # Recheck the exact current child signatures under the transition lock.
            signatures, roots, _jobs = verifier._children(tx, parent, context)
            if signatures != verdict.children or roots != verdict.roots:
                raise PermissionError("certified child set changed")
            if len(value["job_ids"]) > 256:
                raise PermissionError("cleanup evidence bound exceeded")
            certified_proofs = {
                proof["job_id"]: proof for proof in certificate["cleanup_proofs"]
            }
            try:
                for job in value["job_ids"]:
                    proof = verifier._proof(job, parent, context.principal_id)
                    if digest(proof) != digest(certified_proofs[job]):
                        raise ValueError("cleanup proof changed")
            except (ValueError, KeyError, OSError):
                raise PermissionError("current cleanup proof unavailable") from None
            retained.update(pending_identity=asdict(pending), terminal_receipt=receipt)
            record.pop("pending_verification")
        if retained is not None:
            history.append(retained)
        ordinal = 1 if prior is None else prior["ordinal"] + 1
        record["host_turn"] = dict(run_id=run_id, ordinal=ordinal, state="active")
        tx.bump_verification(
            dict(parent_session_id=parent, principal_id=context.principal_id)
        )
        bound._service._save(tx, record)
        return HostTurnAdmission(run_id, ordinal, previous, bound)


def capture_host_turn(bound, admission, draft, ledger):
    if type(admission) is not HostTurnAdmission or admission.owner is not bound:
        raise PermissionError("private host turn admission required")
    codec = bound._service.projection_codec
    with bound._scope() as context:
        with bound._service._transaction(context) as tx:
            record = bound._service._row(tx, bound.continuation_id)
            bound._require_current_tx(tx, record, context=context)
            turn = record.get("host_turn")
            if (
                not turn
                or turn["run_id"] != admission.run_id
                or turn["ordinal"] != admission.ordinal
            ):
                raise PermissionError("host turn changed")
            stamp = digest(
                dict(
                    continuation=record["id"],
                    run_id=admission.run_id,
                    ordinal=admission.ordinal,
                )
            )
            binding = ProjectionBinding(
                record["id"],
                context.principal_id,
                admission.run_id,
                record["host_conversation_id"],
                record["parent_session_id"],
                record["parent_grant_revision"],
                "host-turn-" + stamp,
                stamp,
                tuple(str(root) for root in context.workspace_roots),
                1,
            )
        original = codec.capture(
            binding=binding,
            ledger=ledger,
            output=draft.output,
            terminal_class=draft.terminal_class,
            blockers=draft.blockers,
            terminal_receipt_id=admission.run_id + "-host-terminal",
        )
        sealed = seal_projection(codec, original, binding)
        with bound._service._transaction(context) as tx:
            record = bound._service._row(tx, bound.continuation_id)
            bound._require_current_tx(tx, record, context=context)
            turn = record.get("host_turn")
            if (
                not turn
                or turn["run_id"] != admission.run_id
                or turn["ordinal"] != admission.ordinal
            ):
                raise PermissionError("host turn changed before projection commit")
            if turn.get("projection_digest") not in (None, sealed.sha256):
                raise PermissionError("host turn terminal projection is immutable")
            tx.link_terminal_projection(record["id"], context.principal_id, sealed)
            turn.update(
                projection_id=binding.verification_id, projection_digest=sealed.sha256
            )
            bound._service._save(tx, record)


def _final_binding(record, turn, original, facts):
    stamp = digest(dict(original=turn['projection_digest'], ordinal=turn['ordinal'],
                        binding=asdict(original.binding_value), facts=facts))
    return replace(original.binding_value, verification_id='host-final-' + stamp,
                   bundle_digest=stamp)


def _stored_final(bound, tx, record, turn):
    receipt = turn.get('final_receipt')
    if not isinstance(receipt, dict):
        raise PermissionError('exact host final receipt unavailable')
    original = _stored_projection(bound, tx, record, turn)
    try:
        facts = dict(receipt['facts'])
        facts['tools'], facts['blockers'] = tuple(facts['tools']), tuple(facts['blockers'])
        typed = HostFinalFacts(**facts)
        binding = _final_binding(record, turn, original, receipt['facts'])
        final = tx.terminal_projection(record['id'], record['principal_id'], binding.verification_id)
        expected = dict(facts=receipt['facts'], original_digest=turn['projection_digest'],
                        projection_id=binding.verification_id, projection_digest=final.sha256)
        if receipt != dict(expected, digest=digest(expected)) or final.binding != binding:
            raise ValueError('final receipt changed')
        result = open_projection(bound._service.projection_codec, final, binding)
        if (result.ledger_bytes != original.ledger_bytes
                or result.terminal_class != typed.terminal_class
                or result.blockers != typed.blockers
                or typed.project_scope not in binding.project_roots):
            raise ValueError('final observation scope changed')
        return result
    except (KeyError, TypeError, ValueError):
        raise PermissionError('host final receipt integrity failure') from None


def capture_host_final(bound, admission, output, facts, ledger):
    if type(admission) is not HostTurnAdmission or admission.owner is not bound:
        raise PermissionError('private host final admission required')
    if type(facts) is not HostFinalFacts:
        raise TypeError('exact host final facts required')
    facts.__post_init__()
    encoded_facts = json.loads(json.dumps(asdict(facts)))
    codec = bound._service.projection_codec
    with bound._scope() as context:
        with bound._service._transaction(context) as tx:
            record = bound._service._row(tx, bound.continuation_id)
            bound._require_current_tx(tx, record, context=context)
            turn = record.get('host_turn')
            if (not turn or turn['run_id'] != admission.run_id
                    or turn['ordinal'] != admission.ordinal or turn['state'] != 'active'):
                raise PermissionError('exact active host final turn required')
            original = _stored_projection(bound, tx, record, turn)
            if ledger.seal() != original.ledger_bytes:
                raise PermissionError('original host ledger changed')
            binding = _final_binding(record, turn, original, encoded_facts)
            original_digest = turn['projection_digest']
        final = codec.capture(binding=binding, ledger=ledger, output=output,
            terminal_class=facts.terminal_class, blockers=facts.blockers,
            terminal_receipt_id=admission.run_id + '-host-final')
        if final.terminal_class != facts.terminal_class:
            raise PermissionError('host final class disagrees with failure marker')
        sealed = seal_projection(codec, final, binding)
        value = dict(facts=encoded_facts, original_digest=original_digest,
                     projection_id=binding.verification_id, projection_digest=sealed.sha256)
        receipt = dict(value, digest=digest(value))
        with bound._service._transaction(context) as tx:
            record = bound._service._row(tx, bound.continuation_id)
            bound._require_current_tx(tx, record, context=context)
            turn = record.get('host_turn')
            if (not turn or turn['run_id'] != admission.run_id or turn['ordinal'] != admission.ordinal
                    or turn['state'] != 'active' or turn.get('projection_digest') != original_digest):
                raise PermissionError('host final turn changed before commit')
            if turn.get('final_receipt') not in (None, receipt):
                raise PermissionError('host final receipt is immutable')
            tx.link_terminal_projection(record['id'], context.principal_id, sealed)
            turn['final_receipt'] = receipt
            bound._service._save(tx, record)


def _turn_link(record, turn):
    return ManagedHostTurnLink(record['id'], record['parent_session_id'],
        record['host_conversation_id'], record['principal_id'], turn['run_id'], turn['ordinal'])


def host_turn_link(bound, admission):
    if type(admission) is not HostTurnAdmission or admission.owner is not bound:
        raise PermissionError("private host turn admission required")
    with bound._scope() as context, bound._service._transaction(context) as tx:
        record = bound._service._row(tx, bound.continuation_id)
        bound._require_current_tx(tx, record, context=context)
        turn = record.get('host_turn')
        if (not turn or turn['run_id'] != admission.run_id
                or turn['ordinal'] != admission.ordinal or turn['state'] != 'active'):
            raise PermissionError("exact active host turn required")
        return _turn_link(record, turn)


def _terminal_link(bound, tx, record, turn):
    final = _stored_final(bound, tx, record, turn)
    receipt = turn['final_receipt']
    return ManagedHostTerminalLink(_turn_link(record, turn), turn['projection_id'],
        turn['projection_digest'], receipt['projection_id'], receipt['projection_digest'],
        receipt['digest'], hashlib.sha256(final.output.encode('utf-8')).hexdigest())


def read_host_terminal_link(bound, run_id, ordinal):
    return read_host_terminal_result(bound, run_id, ordinal).receipt


def read_host_terminal_result(bound, run_id, ordinal):
    """Reconcile one exact closed turn under current attached host authority."""
    if type(run_id) is not str or not 1 <= len(run_id.encode('utf-8')) <= 128:
        raise ValueError('bounded host run identity required')
    if type(ordinal) is not int or not 1 <= ordinal <= 32:
        raise ValueError('bounded host turn ordinal required')
    with bound._scope() as context, bound._service._transaction(context) as tx:
        record = bound._service._row(tx, bound.continuation_id)
        bound._require_current_tx(tx, record, context=context)
        turns = list(record.get('host_turn_history', []))
        current = record.get('host_turn')
        if current:
            turns.append(current)
        matches = [turn for turn in turns if turn['run_id'] == run_id and turn['ordinal'] == ordinal]
        if len(matches) != 1 or matches[0]['state'] != 'closed':
            raise PermissionError('exact closed host turn unavailable')
        final = _stored_final(bound, tx, record, matches[0])
        return FinalizedHostResult(final.output, _terminal_link(bound, tx, record, matches[0]))


def close_host_turn(bound, admission):
    if type(admission) is not HostTurnAdmission or admission.owner is not bound:
        raise PermissionError("private host turn admission required")
    with bound._scope() as context, bound._service._transaction(context) as tx:
        record = bound._service._row(tx, bound.continuation_id)
        bound._require_current_tx(tx, record, context=context)
        turn = record.get("host_turn")
        if (
            not turn
            or turn["run_id"] != admission.run_id
            or turn["ordinal"] != admission.ordinal
        ):
            raise PermissionError("host turn closure changed")
        # Missing/corrupt capture remains non-replayable after a failed callback.
        if not turn.get("projection_digest"):
            raise PermissionError("host terminal evidence unavailable")
        _stored_projection(bound, tx, record, turn)
        link = _terminal_link(bound, tx, record, turn)
        turn["state"] = "closed"
        bound._service._save(tx, record)
        return link


def read_current_host_final_evidence(bound, expected_turn):
    """Observe only the exact current closed turn under live attachment authority."""
    if type(expected_turn) is not ManagedHostTurnLink:
        raise PermissionError("typed expected host turn required")
    expected_turn.__post_init__()
    with bound._scope() as context, bound._service._transaction(context) as tx:
        record = bound._service._row(tx, bound.continuation_id)
        bound._require_current_tx(tx, record, context=context)
        turn = record.get("host_turn")
        if not turn or turn["state"] != "closed" or _turn_link(record, turn) != expected_turn:
            raise PermissionError("exact current closed host turn required")
        final = _stored_final(bound, tx, record, turn)
        facts = dict(turn["final_receipt"]["facts"])
        facts["tools"], facts["blockers"] = tuple(facts["tools"]), tuple(facts["blockers"])
        return ManagedHostFinalEvidence(
            FinalizedHostResult(final.output, _terminal_link(bound, tx, record, turn)),
            HostFinalFacts(**facts),
        )
