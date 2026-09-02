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
"""
from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.application.tools.gateway_contract import ToolGatewayRequest, ToolReceipt
from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor

RECORD_SCHEMA = "tool-audit-record-v2"


@dataclass(frozen=True)
class ToolAuditLimits:
    max_records: int = 4096
    max_bytes: int = 8 * 1024 * 1024
    rotate: bool = True


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
        self._lock = threading.Lock()

    def append(self, request: ToolGatewayRequest, receipt: ToolReceipt) -> None:
        with self._lock:
            rotated_from = None
            try:
                entries = self._read()
            except ToolAuditError:
                if not self.limits.rotate:
                    raise
                # A file this repository cannot read is not evidence it can
                # extend; set it aside and start a chain it can vouch for.
                rotated_from = {"path": self._rotate(), "audit_digest": "",
                                "records": None, "reason": "unreadable"}
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
                rotated_from = {"path": self._rotate(), "audit_digest": previous,
                                "records": len(entries),
                                "reason": "records" if over_records else "bytes"}
                line = self._line(request, receipt, "", rotated_from)
                current = b""
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(current + line)

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

    def _rotate(self) -> str:
        """Move the current file aside under a UTC stamp; return its new name."""
        if not self.path.exists():
            return ""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        for index in itertools.count():
            suffix = ".%d" % index if index else ""
            candidate = self.path.with_name(
                "%s.%s%s%s" % (self.path.stem, stamp, suffix, self.path.suffix))
            if not candidate.exists():
                break
        self.path.replace(candidate)
        return candidate.name

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = self.path.read_bytes()
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
            candidate for candidate in self.path.parent.glob(pattern)
            if candidate != self.path
        )) if self.path.parent.exists() else ()

    def verify(self) -> None:
        with self._lock:
            previous = ""
            for entry in self._read():
                digest = entry.get("audit_digest")
                material = dict(entry)
                material.pop("audit_digest", None)
                if entry.get("previous_audit_digest", "") != previous or digest != _digest(material):
                    raise ToolAuditError("tool audit integrity check failed")
                previous = digest


__all__ = ["DurableToolAuditRepository", "RECORD_SCHEMA", "ToolAuditLimits"]
