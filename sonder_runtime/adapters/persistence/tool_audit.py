"""Bounded append-only JSONL audit repository for tool gateway receipts."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.application.tools.gateway_contract import ToolGatewayRequest, ToolReceipt
from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor


@dataclass(frozen=True)
class ToolAuditLimits:
    max_records: int = 4096
    max_bytes: int = 8 * 1024 * 1024


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
            entries = self._read()
            if len(entries) >= self.limits.max_records:
                raise ToolAuditError("tool audit record bound exceeded")
            previous = entries[-1]["audit_digest"] if entries else ""
            raw = {
                "request_id": receipt.request_id,
                "tool_name": receipt.tool_name,
                "session_id": request.session_id,
                "project_id": request.project_id,
                "principal_id": request.scope.principal_id,
                "workspace_roots": list(request.scope.workspace_roots),
                "success": receipt.success,
                "output": receipt.output,
                "error_code": receipt.error_code,
                "error": receipt.error,
                "duration_ms": receipt.duration_ms,
                "redaction_applied": receipt.redaction_applied,
                "approval_required": receipt.approval_required,
                "previous_audit_digest": previous,
            }
            try:
                safe = _redact_value(raw, self._redactor)
                json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str)
                if not isinstance(safe, dict) or safe.get("request_id") != receipt.request_id:
                    raise ToolAuditError("tool audit redaction produced invalid record")
            except ToolAuditError:
                raise
            except (TypeError, ValueError, OSError) as exc:
                raise ToolAuditError("tool audit record is not safely serializable") from exc
            safe["audit_digest"] = _digest(safe)
            line = (json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            current = self.path.read_bytes() if self.path.exists() else b""
            if len(current) + len(line) > self.limits.max_bytes:
                raise ToolAuditError("tool audit byte bound exceeded")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(current + line)

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


__all__ = ["DurableToolAuditRepository", "ToolAuditLimits"]
