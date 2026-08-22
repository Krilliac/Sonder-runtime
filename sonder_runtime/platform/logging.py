"""Canonical structured logging, redaction, and child-environment policy."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Iterable

from sonder_runtime.platform.child_environment_policy import (
    unsafe_child_secret_name,
)

REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED]"

SECRET_ENV_VARS = (
    "SONDER_API_KEY",
    "SONDER_AUTH_SECRET",
    "SONDER_LAUNCHER_HEALTH_TOKEN",
    "SONDER_FILE_APPROVAL_CODE",
    "SONDER_FILE_BYPASS",
    "SONDER_CODE_GATE",
    "SONDER_ISOLATED_APPROVAL_CODE",
    "SONDER_ISOLATED_WRITE_APPROVAL_CODE",
    "SONDER_LAUNCHER_CONTROL_GATE",
    "SONDER_OPENAI_API_KEY",
)

_unsafe_child_secret_name = unsafe_child_secret_name


def child_environment(base=None):
    """Copy the environment while removing control-plane secrets."""
    source = os.environ if base is None else base
    secret = set(SECRET_ENV_VARS)
    unsafe_policy = sys.modules.get("unsafe_lab")
    if unsafe_policy is not None and unsafe_policy.active():
        secret.update(key for key in source if _unsafe_child_secret_name(key))
    return {key: value for key, value in source.items() if key not in secret}


_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\S+(?:\s+\S+)?)"),
    re.compile(r"(?i)\b(bearer\s+)([a-z0-9._~+/=-]{8,})"),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
        r"passwd|credential)[\"']?\s*[:=]\s*)([\"']?[^\s\"',;}{]{4,}[\"']?)"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


class Redactor:
    """Apply value- and pattern-based redaction to arbitrary log text."""

    def __init__(
        self,
        *,
        secret_values: Iterable[str] = (),
        path_prefixes: Iterable[str] = (),
        env: dict[str, str] | None = None,
        failure_hook=None,
    ) -> None:
        source = os.environ if env is None else env
        values = {v for v in secret_values if v}
        for name in SECRET_ENV_VARS:
            value = source.get(name, "")
            if len(value) >= 4:
                values.add(value)
        self._values = sorted(values, key=len, reverse=True)
        self._path_prefixes = tuple(p for p in path_prefixes if p)
        self._failure_hook = failure_hook

    def redact(self, text: str) -> str:
        try:
            for value in self._values:
                if value in text:
                    text = text.replace(value, REDACTED)
            for pattern in _PATTERNS:
                if pattern.groups >= 2:
                    text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
                else:
                    text = pattern.sub(REDACTED, text)
            for prefix in self._path_prefixes:
                if prefix in text:
                    text = text.replace(prefix, "[WORKSPACE]")
            return text
        except Exception:
            if self._failure_hook is not None:
                try:
                    self._failure_hook()
                except Exception:
                    pass
            return REDACTION_FAILED


class JsonFormatter(logging.Formatter):
    """Structured JSON log line with UTC timestamps and redacted text."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or Redactor()

    def format(self, record: logging.Record) -> str:
        message = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        payload = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "severity": record.levelname,
            "component": getattr(record, "component", record.name),
            "event_code": getattr(record, "event_code", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "operation_id": getattr(record, "operation_id", None),
            "principal_id": getattr(record, "principal_id", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "result": getattr(record, "result", None),
            "message": self._redactor.redact(message),
        }
        return json.dumps(
            {k: v for k, v in payload.items() if v is not None},
            ensure_ascii=False,
            default=str,
        )


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    redactor: Redactor | None = None,
    stream=None,
) -> logging.Logger:
    """Install the production logging configuration on the root logger."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    handler = logging.StreamHandler(stream)
    if log_format == "json":
        handler.setFormatter(JsonFormatter(redactor))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.handlers[:] = [handler]
    return root


__all__ = [
    "REDACTED",
    "REDACTION_FAILED",
    "JsonFormatter",
    "Redactor",
    "SECRET_ENV_VARS",
    "child_environment",
    "configure_logging",
]
