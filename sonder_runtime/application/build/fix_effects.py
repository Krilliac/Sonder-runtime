"""The ``build-fix`` effect family: journaled source edits of a build fix.

Every source edit a fix makes (a candidate write, a revert to the best
state, a ``revert_after``, a ``build_fix_restore``) is one effect in the
worker effect journal, recorded around the editor call by
``journaled_effect``:

* the intent is committed **before** the edit. Its identity is the fix job,
  the candidate (the attempt, revert or restore that asked for the edit), the
  file and the (before, after) SHA-256 pair; the idempotency key is exactly
  that tuple, and the operation id is ``build-fix:<digest of the key>``, so
  the same logical edit always maps to the same intent;
* the receipt (with a content-free checkpoint) is committed **after** the
  editor returned. A write that was refused, or that the editor proves did
  not reach the file, is a ``failed`` outcome; a write whose result cannot
  be proven leaves the intent ``uncertain``.

The run is ``build-fix:<job_id>``; the scope is the project root. Nothing
here reads file contents: the provider verifier
(``adapters.build.fix_effect_verifier``) proves an unresolved intent from
the file's current SHA-256 after a crash.

A second request for an edit whose key the journal already holds (a resumed
or restarted fix asking for the same edit) is refused before the editor is
called, so an edit is never applied twice. An unresolved intent in the run
also refuses every later edit until reconciliation proves it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from ...domain.build.model import safe_rel
from ..execution.effect_journal import EffectIntent, EffectJournalError, EffectState
from ..execution.worker_bindings import (
    AuthenticatedWorkerBinding,
    effect_request_digest,
    journaled_effect,
)
from .fix_ports import BUILD_FIX_ID_RE, EditConflict, EditContext, EditReceipt, EditRefused

FAMILY = "build-fix"
RUN_PREFIX = FAMILY + ":"
MAX_REL_CHARS = 1024
MAX_ROOT_CHARS = 4096
MAX_KEY_CHARS = 2048
MAX_APPLIED_PAGES = 8
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")

# ``(run_id, scope) -> binding``: trusted composition supplies the worker
# identity and owner epoch; callers never choose them per edit.
BindingFactory = Callable[[str, str], AuthenticatedWorkerBinding]


def run_id_for(job_id: str) -> str:
    return RUN_PREFIX + job_id


@dataclass(frozen=True)
class BuildFixEdit:
    """The identity of one journaled source edit (no file content)."""

    job_id: str
    candidate: str
    rel: str
    before_sha256: str
    after_sha256: str
    project_root: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not BUILD_FIX_ID_RE.fullmatch(self.job_id):
            raise ValueError("invalid build fix job id")
        if not isinstance(self.candidate, str) or not _CANDIDATE.fullmatch(self.candidate):
            raise ValueError("invalid build fix candidate id")
        if (not isinstance(self.rel, str) or len(self.rel) > MAX_REL_CHARS
                or safe_rel(self.rel) != self.rel):
            raise ValueError("the edited file must be a normalized relative path")
        for name in ("before_sha256", "after_sha256"):
            if not isinstance(getattr(self, name), str) or not _SHA256.fullmatch(getattr(self, name)):
                raise ValueError("%s must be a lowercase SHA-256 digest" % name)
        if self.before_sha256 == self.after_sha256:
            raise ValueError("an edit must change the file")
        if (not isinstance(self.project_root, str) or not self.project_root.strip()
                or len(self.project_root) > MAX_ROOT_CHARS or "\x00" in self.project_root):
            raise ValueError("invalid project root")

    @property
    def run_id(self) -> str:
        return run_id_for(self.job_id)

    @property
    def idempotency_key(self) -> str:
        return json.dumps([FAMILY, self.job_id, self.candidate, self.rel,
                           self.before_sha256, self.after_sha256],
                          separators=(",", ":"), ensure_ascii=True)

    @property
    def operation_id(self) -> str:
        digest = hashlib.sha256(self.idempotency_key.encode("ascii")).hexdigest()
        return "%s:%s" % (FAMILY, digest[:40])

    def request(self) -> dict[str, str]:
        """The admitted request (hashed into the intent's request digest)."""
        return {
            "family": FAMILY, "job_id": self.job_id, "candidate": self.candidate,
            "rel": self.rel, "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256, "project_root": self.project_root,
        }


def edit_from_intent(intent: EffectIntent) -> BuildFixEdit | None:
    """Parse and authenticate a journaled ``build-fix`` intent, else None.

    Every derived field must match what was admitted: the run, the operation
    id, the scope and the request digest. A row that does not round-trip is
    not a build-fix edit and a verifier must not prove it.
    """
    key = getattr(intent, "idempotency_key", "")
    if not isinstance(key, str) or len(key) > MAX_KEY_CHARS:
        return None
    try:
        parts = json.loads(key)
    except ValueError:
        return None
    if (not isinstance(parts, list) or len(parts) != 6 or parts[0] != FAMILY
            or not all(isinstance(item, str) for item in parts)):
        return None
    try:
        edit = BuildFixEdit(job_id=parts[1], candidate=parts[2], rel=parts[3],
                            before_sha256=parts[4], after_sha256=parts[5],
                            project_root=str(getattr(intent, "scope", "")))
    except ValueError:
        return None
    if (edit.idempotency_key != key or intent.run_id != edit.run_id
            or intent.operation_id != edit.operation_id
            or not str(intent.worker_id).startswith(RUN_PREFIX)
            or intent.request_digest != effect_request_digest(edit.request())):
        return None
    return edit


@dataclass(frozen=True)
class _EditOutcome:
    receipt: EditReceipt | None
    error: BaseException | None


class BuildFixEffects:
    """Journal each source edit of a fix around the editor call."""

    def __init__(self, binding_factory: BindingFactory) -> None:
        if not callable(binding_factory):
            raise TypeError("binding_factory must be callable")
        self._factory = binding_factory

    def binding(self, job_id: str, project_root: str) -> AuthenticatedWorkerBinding:
        binding = self._factory(run_id_for(job_id), project_root)
        if not isinstance(binding, AuthenticatedWorkerBinding):
            raise TypeError("the build-fix binding factory must return an authenticated binding")
        if not binding.worker_id.startswith(RUN_PREFIX) or binding.run_id != run_id_for(job_id):
            raise EffectJournalError("the build-fix binding has the wrong identity")
        return binding

    def replace(self, editor: Any, *, job_id: str, candidate: str, rel: str, text: str,
                before_sha256: str, after_sha256: str, ctx: EditContext) -> EditReceipt:
        """``editor.replace`` with its intent before and its receipt after.

        Raises ``EditConflict(uncertain=False)`` when the journal refuses the
        intent (nothing was written), and ``EditConflict(uncertain=True)``
        when the edit ran but its receipt could not be recorded.
        """
        if before_sha256 == after_sha256:
            # The editor writes nothing for an unchanged text.
            return editor.replace(rel, text, expected_sha256=before_sha256, ctx=ctx)
        edit = BuildFixEdit(job_id=job_id, candidate=candidate, rel=rel,
                            before_sha256=before_sha256, after_sha256=after_sha256,
                            project_root=ctx.project_root)
        invoked = []

        def invoke() -> _EditOutcome:
            invoked.append(True)
            try:
                receipt = editor.replace(rel, text, expected_sha256=before_sha256, ctx=ctx)
            except EditRefused as exc:
                return _EditOutcome(None, exc)
            except EditConflict as exc:
                if exc.uncertain:
                    raise
                return _EditOutcome(None, exc)
            if receipt.after != after_sha256 or receipt.before != before_sha256:
                raise EditConflict("the editor's receipt does not match the journaled edit",
                                   uncertain=True, rel=rel)
            return _EditOutcome(receipt, None)

        for reconciled in (False, True):
            try:
                binding = self.binding(job_id, ctx.project_root)
                outcome = journaled_effect(
                    binding, operation_id=edit.operation_id,
                    idempotency_key=edit.idempotency_key, request=edit.request(), invoke=invoke,
                    receipt_key=lambda result: _receipt_key(edit, result),
                    reconciliation="query",
                    success=lambda result: result.receipt is not None,
                    checkpoint_state=lambda result: {
                        "family": FAMILY, "operation_id": edit.operation_id,
                        "applied": result.receipt is not None,
                    },
                )
            except EffectJournalError as exc:
                if invoked:
                    raise EditConflict("the edit of %s ran but its journal receipt failed (%s)"
                                       % (rel, type(exc).__name__), uncertain=True,
                                       rel=rel) from None
                if not reconciled and self._reconciled(job_id, ctx.project_root):
                    # An earlier edit of this run raised (for example a
                    # cancellation inside the gateway) and left its intent
                    # uncertain, which fences the run. The trusted verifier
                    # proved it from the file, so admit this edit once more.
                    # A duplicate key is refused again: nothing runs twice.
                    continue
                raise EditConflict("the effect journal refused the edit of %s: %s"
                                   % (rel, str(exc)[:160]), uncertain=False, rel=rel) from None
            break
        if outcome.error is not None:
            raise outcome.error
        intent_id = "%s:%s" % (edit.run_id, edit.operation_id)
        receipt = outcome.receipt
        return EditReceipt(rel=receipt.rel, before=receipt.before, after=receipt.after,
                           receipt_id=receipt.receipt_id, effect_intent_id=intent_id,
                           tool=receipt.tool)

    def _reconciled(self, job_id: str, project_root: str) -> bool:
        """True when every unresolved edit of the run is now proven."""
        try:
            self.recover(job_id, project_root)
        except EffectJournalError:
            return False
        return True

    def recover(self, job_id: str, project_root: str) -> None:
        """Offer this fix's unresolved edits to the verifiers before new edits.

        Raises ``EffectRecoveryRequired`` (an ``EffectJournalError``) when an
        edit stays unproven.
        """
        self.binding(job_id, project_root).recover_before_restart()

    def applied(self, job_id: str, project_root: str) -> dict[str, frozenset[str]]:
        """``rel -> after-digests`` of this fix's completed edits (bounded read).

        Only receipted or verifier-proven ``completed`` edits count; a journal
        that cannot page its records yields nothing.
        """
        journal = self.binding(job_id, project_root).journal
        page_of = getattr(journal, "effects_since", None)
        if not callable(page_of):
            return {}
        out: dict[str, set[str]] = {}
        after = 0
        for _ in range(MAX_APPLIED_PAGES):
            page = page_of(run_id_for(job_id), after, limit=100)
            for record in page.records:
                if record.state is EffectState.COMPLETED:
                    edit = edit_from_intent(record)
                    if edit is not None and edit.project_root == project_root:
                        out.setdefault(edit.rel, set()).add(edit.after_sha256)
            if not page.truncated or not page.records:
                break
            after = page.records[-1].sequence
        return {rel: frozenset(digests) for rel, digests in out.items()}


def _receipt_key(edit: BuildFixEdit, result: _EditOutcome) -> str:
    if result.receipt is None:
        return "%s:not-applied:%s" % (edit.operation_id, type(result.error).__name__)
    return "%s:sha256:%s" % (edit.operation_id, edit.after_sha256)


__all__ = [
    "BindingFactory", "BuildFixEdit", "BuildFixEffects", "FAMILY", "edit_from_intent",
    "run_id_for",
]
