"""Bounded append-only JSONL audit repository for tool gateway receipts.

Every record is redacted before it is written, carries the digest of the
record before it, and names how its call ended (``terminal``), where the
call came from (``source``, ``auth_level``) and what it touched in digest
form (``argument_digest``, ``result_digest``).  The file is bounded; when the
next record would cross a bound the current file is rotated aside (renamed
with a UTC stamp) and a fresh chain starts whose first record names the file
it continues from, so the operator remedy for a full audit is nothing --
the repository rotates itself -- and an unbounded audit can never grow
silently.  Rotation can be switched off, in which case a full audit fails
the call closed, as the audit boundary promises.

Rotated chains share an aggregate retention quota (a file count and a byte
total).  By default the oldest rotated chains are pruned to make room and
the next chain's first record names what was pruned, so the deletion is
itself on the record; with ``prune_rotated`` off, a full quota fails the
call closed instead of deleting evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.application.tools.gateway_contract import ToolGatewayRequest, ToolReceipt
from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor
from sonder_runtime.platform.private_files import prepare_private_file

RECORD_SCHEMA = "tool-audit-record-v2"


@dataclass(frozen=True)
class ToolAuditLimits:
    max_records: int = 4096
    max_bytes: int = 8 * 1024 * 1024
    rotate: bool = True
    # Aggregate bound over rotated chains: 32 x 8 MiB by default.
    max_rotated_files: int = 32
    max_rotated_bytes: int = 256 * 1024 * 1024
    prune_rotated: bool = True


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        safe = redactor.redact(value)
        if safe == REDACTION_FAILED:
            raise ToolAuditError("tool audit redaction failed")
        return safe
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, redactor) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, redactor) for item in value]
    return value


def _json_safe(value: Any) -> Any:
    """Coerce evidence to plain JSON so the digest and the file agree."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


class DurableToolAuditRepository:
    """Store only redacted, bounded receipt metadata with a hash chain."""

    def __init__(self, path: str | Path, *, redactor: Redactor | None = None,
                 limits: ToolAuditLimits | None = None) -> None:
        self.path = Path(path)
        self._redactor = redactor or Redactor()
        self.limits = limits or ToolAuditLimits()
        if self.limits.max_records < 1 or self.limits.max_bytes < 256:
            raise ValueError("tool audit limits must be positive and usable")
        if (self.limits.max_rotated_files < 1
                or self.limits.max_rotated_bytes < self.limits.max_bytes):
            raise ValueError(
                "tool audit retention must keep at least one full rotated file")
        self._rotated_name = re.compile(
            r"%s\.(\d{8}T\d{6}Z)(?:\.(\d+))?%s\Z"
            % (re.escape(self.path.stem), re.escape(self.path.suffix)))
        self._lock = threading.Lock()

    def _rotation_order(self, name: str) -> tuple[str, int]:
        """(stamp, index) of a rotated chain: its position in rotation order.

        Name text is not that order: within one second the chains are
        ``<stem>.<stamp><suffix>``, ``.1``, ``.2``, ... and ``.10`` sorts
        before ``.2`` while the unsuffixed (oldest) sorts last. File mtimes
        are no substitute either; a coarse file clock ties them.
        """
        match = self._rotated_name.match(name)
        return (match.group(1), int(match.group(2) or 0)) if match else ("", -1)

    def append(self, request: ToolGatewayRequest, receipt: ToolReceipt) -> None:
        with self._lock:
            rotated_from = None
            pending_prune: list[Path] = []
            try:
                entries = self._read()
            except ToolAuditError:
                if not self.limits.rotate:
                    raise
                # A file this repository cannot read is not evidence it can
                # extend; set it aside and start a chain it can vouch for.
                pending_prune = self._make_room()
                rotated_from = {"path": self._rotate(), "audit_digest": "",
                                "records": None, "reason": "unreadable"}
                if pending_prune:
                    rotated_from["pruned"] = [path.name for path in pending_prune]
                entries = []
            previous = entries[-1]["audit_digest"] if entries else ""
            line = self._line(request, receipt, previous, rotated_from)
            current = self.path.read_bytes() if self.path.exists() else b""
            over_records = len(entries) >= self.limits.max_records
            over_bytes = len(current) + len(line) > self.limits.max_bytes
            if over_records or over_bytes:
                if not self.limits.rotate:
                    raise ToolAuditError(
                        "tool audit record bound exceeded" if over_records
                        else "tool audit byte bound exceeded")
                pending_prune = self._make_room()
                rotated_from = {"path": self._rotate(), "audit_digest": previous,
                                "records": len(entries),
                                "reason": "records" if over_records else "bytes"}
                if pending_prune:
                    rotated_from["pruned"] = [path.name for path in pending_prune]
                line = self._line(request, receipt, "", rotated_from)
                current = b""
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only audit file: created 0600, an older 0644 tightened.
            prepare_private_file(self.path)
            if pending_prune:
                # The deletion record must reach stable storage before any
                # older chain it names can be removed.
                with self.path.open("wb") as stream:
                    stream.write(current + line)
                    stream.flush()
                    os.fsync(stream.fileno())
                for candidate in pending_prune:
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise ToolAuditError(
                            "tool audit retention could not prune %s" % candidate.name
                        ) from exc
            else:
                # A journal terminal outcome may refer to this receipt as
                # soon as append returns. Flush it before that can happen.
                with self.path.open("wb") as stream:
                    stream.write(current + line)
                    stream.flush()
                    os.fsync(stream.fileno())

    def _line(self, request: ToolGatewayRequest, receipt: ToolReceipt,
              previous: str, rotated_from: dict[str, Any] | None) -> bytes:
        scope = request.scope
        raw = {
            "schema": RECORD_SCHEMA,
            "request_id": receipt.request_id,
            "tool_name": receipt.tool_name,
            "session_id": request.session_id,
            "project_id": request.project_id,
            "principal_id": scope.principal_id,
            "workspace_roots": list(scope.workspace_roots),
            "source": getattr(scope, "source", ""),
            "auth_level": getattr(scope, "auth_level", ""),
            "success": receipt.success,
            "terminal": receipt.terminal,
            "output": receipt.output,
            "evidence": _json_safe(dict(receipt.evidence)),
            "error_code": receipt.error_code,
            "error": receipt.error,
            "duration_ms": receipt.duration_ms,
            "redaction_applied": receipt.redaction_applied,
            "approval_required": receipt.approval_required,
            "execution_world": receipt.execution_world,
            "argument_digest": receipt.argument_digest,
            "result_digest": receipt.result_digest,
            "effects": list(receipt.effects),
            "policy_match": receipt.policy_match,
            "model": receipt.model,
            "previous_audit_digest": previous,
        }
        if rotated_from is not None:
            raw["rotated_from"] = rotated_from
        if request.schema_selection is not None:
            raw["tool_schema_selection"] = request.schema_selection.marker()
        try:
            safe = _redact_value(raw, self._redactor)
            json.dumps(safe, sort_keys=True, ensure_ascii=False)
            if not isinstance(safe, dict) or safe.get("request_id") != receipt.request_id:
                raise ToolAuditError("tool audit redaction produced invalid record")
        except ToolAuditError:
            raise
        except (TypeError, ValueError, OSError) as exc:
            raise ToolAuditError("tool audit record is not safely serializable") from exc
        safe["audit_digest"] = _digest(safe)
        return (json.dumps(safe, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8")

    def _make_room(self) -> list[Path]:
        """Keep the rotated chains within quota once the current file joins them.

        Returns the files to prune (oldest first), without deleting them.
        With pruning disabled a full quota raises before rotation.
        """
        incoming = self.path.stat().st_size if self.path.exists() else 0
        if not incoming:
            return []
        # rotated_files() is already oldest first, by rotation order.
        rotated = []
        for candidate in self.rotated_files():
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            rotated.append((candidate, size))
        count = len(rotated) + 1
        total = sum(item[1] for item in rotated) + incoming
        pruned: list[Path] = []
        while rotated and (count > self.limits.max_rotated_files
                           or total > self.limits.max_rotated_bytes):
            if not self.limits.prune_rotated:
                raise ToolAuditError(
                    "tool audit retention quota exhausted (%d rotated files, "
                    "%d bytes); pruning is disabled" % (len(rotated), total - incoming))
            candidate, size = rotated.pop(0)
            pruned.append(candidate)
            count -= 1
            total -= size
        return pruned

    def _rotate(self) -> str:
        """Move the current file aside under a UTC stamp; return its new name."""
        if not self.path.exists():
            return ""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        # Strictly increasing within a second: a pruned index is never reused,
        # so name order stays rotation order (which retention prunes by).
        taken = [self._rotation_order(path.name)[1] for path in self.rotated_files()
                 if self._rotation_order(path.name)[0] == stamp]
        index = max(taken) + 1 if taken else 0
        suffix = ".%d" % index if index else ""
        candidate = self.path.with_name(
            "%s.%s%s%s" % (self.path.stem, stamp, suffix, self.path.suffix))
        self.path.replace(candidate)
        return candidate.name

    def _read(self, path: Path | None = None) -> list[dict[str, Any]]:
        path = self.path if path is None else path
        if not path.exists():
            return []
        try:
            with path.open("rb") as stream:
                data = stream.read(self.limits.max_bytes + 1)
            if len(data) > self.limits.max_bytes:
                raise ToolAuditError("tool audit byte bound exceeded")
            entries = [json.loads(line) for line in data.splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolAuditError("tool audit is unreadable") from exc
        if len(entries) > self.limits.max_records or any(not isinstance(item, dict) for item in entries):
            raise ToolAuditError("tool audit record bound or shape invalid")
        return entries

    def read(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            return tuple(self._read()[-min(limit, self.limits.max_records):])

    def rotated_files(self) -> tuple[Path, ...]:
        """Earlier chains this file was rotated away from, oldest first."""
        pattern = "%s.*%s" % (self.path.stem, self.path.suffix)
        return tuple(sorted(
            (candidate for candidate in self.path.parent.glob(pattern)
             if candidate != self.path and self._rotated_name.match(candidate.name)),
            key=lambda candidate: self._rotation_order(candidate.name),
        )) if self.path.parent.exists() else ()

    def verify(self) -> None:
        with self._lock:
            self._verify_entries(self._read())

    @staticmethod
    def _verify_entries(entries: list[dict[str, Any]]) -> None:
        previous = ""
        for entry in entries:
            digest = entry.get("audit_digest")
            material = dict(entry)
            material.pop("audit_digest", None)
            if entry.get("previous_audit_digest", "") != previous or digest != _digest(material):
                raise ToolAuditError("tool audit integrity check failed")
            previous = digest

    def read_receipt(self, receipt_key: str) -> dict[str, Any] | None:
        """Find one exact receipt in the bounded retained, verified chains.

        A pruned receipt is unavailable, never permission to repeat its
        effect. Repeated request ids are ambiguous even if their bytes agree.
        """
        if not isinstance(receipt_key, str) or not receipt_key:
            raise ValueError("receipt key must be non-empty text")
        with self._lock:
            rotated = self.rotated_files()
            if len(rotated) > self.limits.max_rotated_files:
                raise ToolAuditError("tool audit retention record bound exceeded")
            total = sum(path.stat().st_size for path in rotated)
            if total > self.limits.max_rotated_bytes:
                raise ToolAuditError("tool audit retention byte bound exceeded")
            match = None
            for path in (*rotated, self.path):
                entries = self._read(path)
                self._verify_entries(entries)
                for entry in entries:
                    if entry.get("request_id") == receipt_key:
                        if match is not None:
                            raise ToolAuditError("tool audit receipt identity is ambiguous")
                        match = entry
            return match


__all__ = ["DurableToolAuditRepository", "RECORD_SCHEMA", "ToolAuditLimits"]
