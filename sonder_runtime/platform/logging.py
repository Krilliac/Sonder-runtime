"""Canonical structured logging, redaction, and child-environment policy."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import time
from typing import Iterable

from sonder_runtime.platform.child_environment_policy import (
    unsafe_child_secret_name,
)
# Exact reviewed exception (scripts/check_architecture.py): the pure shared
# credential formats, reused rather than copied.
from sonder_runtime.domain.security.credential_formats import CREDENTIAL_FORMATS

REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED]"

SECRET_ENV_VARS = (
    # Typed configuration secrets.  Keep this list in lockstep with
    # ``config.SECRET_ENV_KEYS``: command and tool child processes use this
    # boundary before a typed config is exported back into ``os.environ``.
    "SONDER_MEMBERSHIP_CLIENT_CERT_FILE",
    "SONDER_MEMBERSHIP_CLIENT_KEY_FILE",
    "SONDER_API_KEY",
    "SONDER_ARTIFACT_TRANSFER_KEY",
    "SONDER_MEMORY_REPLICATION_KEY",
    "SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY",
    "SONDER_ARTIFACT_MOBILITY_PEER_KEY",
    "SONDER_AUTH_SECRET",
    "SONDER_BACKUP_KEY_FILE",
    "SONDER_LAUNCHER_HEALTH_TOKEN",
    "SONDER_CONTROL_STATE_REHEARSAL_API_KEY",
    # Compatibility execution gates are also authority-bearing and therefore
    # never cross into model-authored child processes.
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
    # Child tools must never receive a secret merely because the unsafe-lab
    # policy is inactive.  The shared classifier deliberately covers both
    # typed Sonder names and common provider/authority names.
    secret.update(key for key in source if _unsafe_child_secret_name(key))
    return {key: value for key, value in source.items() if key not in secret}


_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\S+(?:\s+\S+)?)"),
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b(bearer\s+)([a-z0-9._~+/=-]{8,})"),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
        r"passwd|credential)[\"']?\s*[:=]\s*)([\"']?[^\s\"',;}{]{4,}[\"']?)"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@"),
    re.compile(
        r"(?i)([?&](?:access[-_]?token|api[-_]?key|auth[-_]?secret|"
        r"password|credential)=)([^&#\s]+)"
    ),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Free-standing provider keys (AKIA..., ghp_..., sk-..., JWTs): the same
    # known-credential formats the contribution privacy classifier refuses.
    *CREDENTIAL_FORMATS,
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


def redactor_for_config(config, *, env=None) -> Redactor:
    """Build one redactor for a validated typed configuration.

    Secret files are deliberately loaded into the typed configuration rather
    than copied into the process environment.  Logging and durable sinks need
    those values before any compatibility export happens, so collect every
    string field from the private ``Secrets`` value and every private source
    path here.  The function accepts the config structurally to keep the
    platform logging boundary independent of config loading.
    """
    secret_values: tuple[str, ...] = ()
    secrets = getattr(config, "secrets", None)
    if is_dataclass(secrets):
        secret_values = tuple(
            value
            for item in fields(secrets)
            if isinstance((value := getattr(secrets, item.name, "")), str) and value
        )
    private_paths = getattr(config, "private_source_paths", ())
    path_prefixes = (
        tuple(path for path in private_paths if isinstance(path, str) and path)
        if isinstance(private_paths, (tuple, list))
        else ()
    )
    return Redactor(
        secret_values=secret_values,
        path_prefixes=path_prefixes,
        env=env,
    )


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
            "component": self._redact_value(getattr(record, "component", record.name)),
            "event_code": self._redact_value(getattr(record, "event_code", None)),
            "correlation_id": self._redact_value(getattr(record, "correlation_id", None)),
            "operation_id": self._redact_value(getattr(record, "operation_id", None)),
            "principal_id": self._redact_value(getattr(record, "principal_id", None)),
            "duration_ms": getattr(record, "duration_ms", None),
            "result": self._redact_value(getattr(record, "result", None)),
            "message": self._redactor.redact(message),
        }
        return json.dumps(
            {k: v for k, v in payload.items() if v is not None},
            ensure_ascii=False,
            default=lambda value: self._redactor.redact(str(value)),
        )

    def _redact_value(self, value):
        if isinstance(value, str):
            return self._redactor.redact(value)
        if isinstance(value, dict):
            return {
                self._redactor.redact(str(key)): self._redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in value]
        return value


class RedactingTextFormatter(logging.Formatter):
    """Text formatter that redacts the complete rendered record."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._redactor = redactor or Redactor()

    def format(self, record: logging.Record) -> str:
        return self._redactor.redact(super().format(record))


class _SafeStreamHandler(logging.StreamHandler):
    """Avoid stderr noise when a test or shutdown owner closes its stream."""

    def handleError(self, record: logging.LogRecord) -> None:
        if self.stream is None or getattr(self.stream, "closed", False):
            return
        super().handleError(record)


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
    handler = _SafeStreamHandler(stream)
    if log_format == "json":
        handler.setFormatter(JsonFormatter(redactor))
    else:
        handler.setFormatter(RedactingTextFormatter(redactor))
    root.handlers[:] = [handler]
    return root


# --- interactive REPL logging ---------------------------------------------
#
# A terminal REPL must not print JSON log lines over its banner, prompt or
# live line. Its records go to a private rotating file instead; WARNING+ is
# also handed to a notice sink the REPL drains between turns, and piped runs
# get ERROR on stderr as text. serve and mcp keep ``configure_logging``.

REPL_LOG_NAME = "repl.log"
REPL_LOG_MAX_BYTES = 1_000_000
REPL_LOG_BACKUPS = 5
REPL_LOG_FILE_MODE = 0o600

CONSOLE_NOTICES = "notices"
CONSOLE_STDERR_ERRORS = "stderr-errors"
CONSOLE_STDERR_JSON = "stderr-json"


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file whose every generation is created owner-only (0600)."""

    def _open(self):
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            REPL_LOG_FILE_MODE,
        )
        try:
            # A file created before this handler existed may be wider.
            os.chmod(self.baseFilename, REPL_LOG_FILE_MODE)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)


class NoticeQueueHandler(logging.Handler):
    """Hand redacted WARNING+ records to an injected ``sink``.

    ``sink(level, levelname, component, message, created)`` is normally
    ``ReplNoticeQueue.push`` from ``application.ports.repl_notices``; it is
    injected so this platform module never imports the application layer.
    """

    def __init__(self, sink, *, redactor: Redactor | None = None,
                 level: int = logging.WARNING) -> None:
        if not callable(sink):
            raise TypeError("notice sink must be callable")
        super().__init__(level)
        self._sink = sink
        self._redactor = redactor or Redactor()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            component = getattr(record, "component", None) or record.name
            self._sink(
                record.levelno,
                record.levelname,
                self._redactor.redact(str(component)),
                self._redactor.redact(record.getMessage()),
                record.created,
            )
        except Exception:
            # Never let a notice failure reach the terminal as a traceback.
            pass


@dataclass(frozen=True)
class ReplLoggingPlan:
    """What ``configure_repl_logging`` installed, for the caller and tests."""

    console: str
    file_path: str | None
    file_level: str | None
    fallback_reason: str | None = None


def _level_name(value, default: str) -> str:
    name = str(value or "").strip().upper()
    return name if name in ("DEBUG", "INFO", "WARNING", "ERROR") else default


def repl_log_stderr_requested(env=None) -> bool:
    """``SONDER_REPL_LOG_STDERR=1`` restores JSON logs on stderr."""
    source = os.environ if env is None else env
    return str(source.get("SONDER_REPL_LOG_STDERR", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def configure_repl_logging(
    *,
    home,
    interactive: bool,
    notice_sink=None,
    redactor: Redactor | None = None,
    env=None,
    stream=None,
    log_format: str = "json",
) -> ReplLoggingPlan:
    """Install the root handlers for ``python -m sonder_runtime repl``.

    - ``SONDER_REPL_LOG_STDERR=1``: the old behaviour, logs on stderr in
      ``log_format`` at ``SONDER_REPL_LOG_LEVEL`` (default WARNING).
    - Otherwise every record at ``SONDER_REPL_LOG_LEVEL`` (default INFO) goes
      to ``<home>/logs/repl.log`` as JSON, rotating 5 x 1 MB, mode 0600, and
      * ``interactive`` with a ``notice_sink``: WARNING+ goes to the sink;
      * else: ERROR+ goes to stderr as redacted text.
    - If the log file cannot be opened, the stderr behaviour is used and the
      reason is returned in the plan.
    """
    source = os.environ if env is None else env
    redactor = redactor or Redactor(env=source)
    root = logging.getLogger()
    if repl_log_stderr_requested(source):
        level = _level_name(source.get("SONDER_REPL_LOG_LEVEL"), "WARNING")
        configure_logging(level=level, log_format=log_format,
                          redactor=redactor, stream=stream)
        return ReplLoggingPlan(CONSOLE_STDERR_JSON, None, None)
    file_level = _level_name(source.get("SONDER_REPL_LOG_LEVEL"), "INFO")
    try:
        from sonder_runtime.platform.private_files import ensure_private_dir

        log_dir = ensure_private_dir(Path(home) / "logs")
        path = os.fspath(log_dir / REPL_LOG_NAME)
        file_handler = PrivateRotatingFileHandler(
            path, maxBytes=REPL_LOG_MAX_BYTES, backupCount=REPL_LOG_BACKUPS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError, TypeError) as exc:
        configure_logging(level="WARNING", log_format=log_format,
                          redactor=redactor, stream=stream)
        return ReplLoggingPlan(
            CONSOLE_STDERR_JSON, None, None,
            fallback_reason="%s: %s" % (type(exc).__name__, exc),
        )
    file_handler.setLevel(getattr(logging, file_level))
    file_handler.setFormatter(JsonFormatter(redactor))
    if interactive and notice_sink is not None:
        console = NoticeQueueHandler(notice_sink, redactor=redactor)
        console_kind = CONSOLE_NOTICES
    else:
        console = _SafeStreamHandler(stream)
        console.setLevel(logging.ERROR)
        console.setFormatter(RedactingTextFormatter(redactor))
        console_kind = CONSOLE_STDERR_ERRORS
    root.setLevel(min(file_handler.level, console.level))
    for old in list(root.handlers):
        if old not in (file_handler, console):
            try:
                old.close()
            except Exception:
                pass
    root.handlers[:] = [file_handler, console]
    return ReplLoggingPlan(console_kind, path, file_level)


__all__ = [
    "CONSOLE_NOTICES",
    "CONSOLE_STDERR_ERRORS",
    "CONSOLE_STDERR_JSON",
    "REDACTED",
    "REDACTION_FAILED",
    "REPL_LOG_BACKUPS",
    "REPL_LOG_FILE_MODE",
    "REPL_LOG_MAX_BYTES",
    "REPL_LOG_NAME",
    "JsonFormatter",
    "NoticeQueueHandler",
    "PrivateRotatingFileHandler",
    "RedactingTextFormatter",
    "Redactor",
    "ReplLoggingPlan",
    "SECRET_ENV_VARS",
    "child_environment",
    "configure_logging",
    "configure_repl_logging",
    "redactor_for_config",
    "repl_log_stderr_requested",
]
