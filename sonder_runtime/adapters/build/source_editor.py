"""``SourceEditor`` over the typed tool gateway (the only write path of a fix).

Reads go through ``read_file``; writes through ``text_patch`` (a strict
unified diff, applied transactionally by the guarded primitive, which
refuses a target that changed between its preflight and publication), else
``write_file`` for files ``text_patch`` cannot carry (over its size cap or
with mixed line endings). The editor never opens a project file itself.

Every request carries the fix's grant token as ``approval_token``
(``build_fix_grant:<token>``) so the permission evaluator can recognize an
in-scope write of an approved fix; the arguments stay exactly the tool's
schema. The gateway admits the call, records the effect-journal intent for
the write (when a journal is bound), invokes the guarded primitive and
publishes the receipt; this adapter only reads the outcome back.

Conflict semantics:

* the file no longer hashes to ``expected_sha256`` before the write:
  ``EditConflict(uncertain=False)``; nothing was written;
* the primitive refused (context mismatch, rolled-back transaction):
  ``EditConflict(uncertain=False)``;
* the write went through but the result cannot be proven (hash mismatch
  after the write, incomplete rollback, an exception from the invoker):
  ``EditConflict(uncertain=True)``;
* policy refusal: ``EditRefused``; nothing was written.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from typing import Any, Callable, Mapping

from ...application.build.fix_ports import EditConflict, EditContext, EditReceipt, EditRefused
from ...application.build.grants import grant_carrier
from ...application.execution import effect_journal
from ...application.tools.gateway_contract import (
    ApprovalMode,
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from ...domain.build.model import safe_rel
from ...domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, SonderError
from ...domain.security.redaction import REDACTED, REDACTION_FAILED

MAX_SOURCE_BYTES = 2 * 1024 * 1024
TEXT_PATCH_MAX_BYTES = 1_000_000
# Effects the typed descriptors declare (native catalog): reads declare none.
DEFAULT_EFFECTS = {
    "read_file": frozenset(),
    "text_patch": frozenset({"write_files"}),
    "write_file": frozenset({"write_files"}),
}
_ROLLED_BACK = "rolled_back"
_GUARD_REFUSALS = frozenset({"PermissionError", "ValueError", "FileExistsError",
                             "FileNotFoundError", "TypeError", "KeyError"})


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _eol_kinds(text: str) -> set[str]:
    kinds = set()
    crlf = text.count("\r\n")
    if crlf:
        kinds.add("crlf")
    if text.count("\n") > crlf:
        kinds.add("lf")
    if text.replace("\r\n", "").count("\r"):
        kinds.add("cr")
    return kinds


def unified_diff(rel: str, before: str, after: str) -> str:
    """A strict unified diff ``text_patch`` accepts, with no-newline markers."""
    out: list[str] = []
    for line in difflib.unified_diff(before.splitlines(keepends=True),
                                     after.splitlines(keepends=True),
                                     fromfile="a/" + rel, tofile="b/" + rel, n=3):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append("\\ No newline at end of file\n")
    return "".join(out)


class GatewaySourceEditor:
    """Read and replace project sources through the typed tool gateway."""

    def __init__(self, execute: Callable[[ToolGatewayRequest], Any], *,
                 effects: Mapping[str, frozenset[str]] | None = None,
                 max_source_bytes: int = MAX_SOURCE_BYTES,
                 id_factory: Callable[[], str] | None = None,
                 execution_world: str = "local") -> None:
        if not callable(execute):
            raise TypeError("execute must be callable (the typed gateway's execute)")
        self._execute = execute
        self._effects = dict(DEFAULT_EFFECTS)
        self._effects.update(effects or {})
        self._max = int(max_source_bytes)
        self._ids = id_factory or (lambda: "build-fix-edit-" + uuid.uuid4().hex)
        self._world = execution_world

    @classmethod
    def over(cls, facade: Any, **kwargs: Any) -> "GatewaySourceEditor":
        """An editor over a ``ToolApplicationFacade`` (effects from its registry)."""
        effects = {}
        registry = getattr(getattr(facade, "graph", None), "registry", None)
        for name in DEFAULT_EFFECTS:
            descriptor = registry.get(name) if registry is not None else None
            if descriptor is not None:
                effects[name] = frozenset(
                    effect.name.lower() if hasattr(effect, "name") else str(effect)
                    for effect in descriptor.effects)
        return cls(facade.execute, effects=effects, **kwargs)

    # -- helpers -----------------------------------------------------------------

    def _path(self, rel: str, ctx: EditContext) -> tuple[str, str]:
        clean = safe_rel(str(rel or "").replace("\\", "/"))
        if clean is None or not ctx.project_root:
            raise EditRefused("the file must be relative to the project root", rel=str(rel))
        root = ctx.project_root.rstrip("/\\")
        separator = "\\" if ("\\" in root and "/" not in root) else "/"
        return clean, root + separator + clean.replace("/", separator)

    def _request(self, tool: str, arguments: dict, ctx: EditContext) -> ToolGatewayRequest:
        operation = ctx.operation
        effects = frozenset(self._effects.get(tool, frozenset()))
        roots = tuple(str(root) for root in operation.workspace_roots) or (ctx.project_root,)
        return ToolGatewayRequest(
            request_id=self._ids(),
            tool_name=tool,
            arguments=arguments,
            scope=ToolScope(operation.principal_id, roots, effects, source=operation.source,
                            auth_level=operation.auth_level),
            permission=ToolPermission(effects, ApprovalMode.NOT_REQUIRED),
            deadline_monotonic=operation.deadline_monotonic,
            cancellation=operation.cancellation,
            approval_token=grant_carrier(ctx.grant_token) if ctx.grant_token else None,
            session_id=operation.session_id,
            execution_world=self._world,
        )

    def _call(self, tool: str, arguments: dict, ctx: EditContext, rel: str, *,
              writes: bool) -> tuple[Any, str]:
        request = self._request(tool, arguments, ctx)
        binding = effect_journal.current()
        try:
            receipt = self._execute(request)
        except (Cancelled, DeadlineExceeded):
            raise
        except Forbidden as exc:
            raise EditRefused("%s refused: %s" % (tool, exc), rel=rel) from None
        except SonderError as exc:
            if writes:
                raise EditConflict("%s failed: %s" % (tool, exc), uncertain=False, rel=rel) from None
            raise EditRefused("%s failed: %s" % (tool, exc), rel=rel, policy=False) from None
        except Exception as exc:  # noqa: BLE001 - the invoker raised after admission
            if writes:
                raise EditConflict("%s raised %s after admission" % (tool, type(exc).__name__),
                                   uncertain=True, rel=rel) from None
            raise EditRefused("%s raised %s" % (tool, type(exc).__name__), rel=rel,
                              policy=False) from None
        intent = ""
        if writes and binding is not None and self._effects.get(tool):
            intent = "%s:%s" % (binding.run_id, request.request_id)
        return receipt, intent

    # -- port --------------------------------------------------------------------

    def read(self, rel: str, ctx: EditContext) -> tuple[str, str]:
        clean, path = self._path(rel, ctx)
        receipt, _ = self._call("read_file", {"path": path, "max_bytes": self._max + 1}, ctx,
                                clean, writes=False)
        if not getattr(receipt, "success", False):
            raise EditRefused("read_file failed: %s" % str(getattr(receipt, "error", ""))[:200],
                              rel=clean, policy=False)
        output = getattr(receipt, "output", "")
        if not isinstance(output, str):
            raise EditRefused("read_file returned no text", rel=clean, policy=False)
        evidence = getattr(receipt, "evidence", {}) or {}
        if evidence.get("truncated") or len(output.encode("utf-8", "surrogatepass")) > self._max:
            raise EditRefused("the file exceeds the 2 MiB edit cap", rel=clean, policy=False)
        if REDACTED in output or REDACTION_FAILED in output or "�" in output:
            # Redacted or undecodable text is not the file: editing it would
            # write the redaction back. Fail closed.
            raise EditRefused("the file's text does not round-trip (redacted or not UTF-8)",
                              rel=clean, policy=False)
        return output, sha256_text(output)

    def replace(self, rel: str, new_text: str, *, expected_sha256: str,
                ctx: EditContext) -> EditReceipt:
        clean, path = self._path(rel, ctx)
        if not isinstance(new_text, str) or len(new_text.encode("utf-8", "surrogatepass")) > self._max:
            raise EditRefused("the new text exceeds the 2 MiB edit cap", rel=clean, policy=False)
        current, current_sha = self.read(clean, ctx)
        if current_sha != expected_sha256:
            raise EditConflict("the file changed since the fix read it", uncertain=False, rel=clean)
        after_sha = sha256_text(new_text)
        if current == new_text:
            return EditReceipt(rel=clean, before=current_sha, after=after_sha, receipt_id="",
                               effect_intent_id="", tool="none")
        use_patch = (
            len(new_text.encode("utf-8")) <= TEXT_PATCH_MAX_BYTES
            and len(current.encode("utf-8")) <= TEXT_PATCH_MAX_BYTES
            and len(_eol_kinds(current) | _eol_kinds(new_text)) <= 1
        )
        if use_patch:
            return self._patch(clean, current, current_sha, new_text, after_sha, ctx)
        return self._overwrite(clean, path, current_sha, new_text, after_sha, ctx)

    def _patch(self, rel: str, before: str, before_sha: str, after: str, after_sha: str,
               ctx: EditContext) -> EditReceipt:
        patch = unified_diff(rel, before, after)
        receipt, intent = self._call("text_patch", {"root": ctx.project_root, "patch": patch,
                                                    "apply": True}, ctx, rel, writes=True)
        report = _json(getattr(receipt, "output", ""))
        if not getattr(receipt, "success", False):
            transaction = str(report.get("transaction", ""))
            uncertain = bool(transaction) and transaction != _ROLLED_BACK
            raise EditConflict("text_patch failed: %s" % str(report.get("error") or
                                                               getattr(receipt, "error", ""))[:200],
                               uncertain=uncertain, rel=rel)
        files = report.get("files") if isinstance(report.get("files"), list) else []
        row = next((item for item in files if isinstance(item, dict) and item.get("path") == rel), None)
        if row is None or row.get("before_sha256") != before_sha or row.get("after_sha256") != after_sha:
            raise EditConflict("text_patch result does not match the intended change",
                               uncertain=True, rel=rel)
        return EditReceipt(rel=rel, before=before_sha, after=after_sha,
                           receipt_id=str(getattr(receipt, "request_id", "")),
                           effect_intent_id=intent, tool="text_patch")

    def _overwrite(self, rel: str, path: str, before_sha: str, after: str, after_sha: str,
                   ctx: EditContext) -> EditReceipt:
        receipt, intent = self._call("write_file", {"path": path, "content": after,
                                                    "mode": "overwrite"}, ctx, rel, writes=True)
        if not getattr(receipt, "success", False):
            # A guard refusal raised before anything was opened for writing.
            guarded = str(getattr(receipt, "error_code", "")) in _GUARD_REFUSALS
            raise EditConflict("write_file failed: %s" % str(getattr(receipt, "error", ""))[:200],
                               uncertain=not guarded, rel=rel)
        _, now_sha = self.read(rel, ctx)
        if now_sha != after_sha:
            raise EditConflict("write_file result does not match the intended text",
                               uncertain=True, rel=rel)
        return EditReceipt(rel=rel, before=before_sha, after=after_sha,
                           receipt_id=str(getattr(receipt, "request_id", "")),
                           effect_intent_id=intent, tool="write_file")


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) and value else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["DEFAULT_EFFECTS", "GatewaySourceEditor", "sha256_text", "unified_diff"]
