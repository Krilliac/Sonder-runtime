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
from sonder_runtime.domain.security.credential_formats import REDACTION_FORMATS

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
    "SONDER_HEALTH_ALERT_NTFY_URL",
    # Compatibility execution gates are also authority-bearing and therefore
    # never cross into model-authored child processes.
    "SONDER_FILE_APPROVAL_CODE",
    "SONDER_FILE_BYPASS",
    "SONDER_CODE_GATE",
    "SONDER_ISOLATED_APPROVAL_CODE",
    "SONDER_ISOLATED_WRITE_APPROVAL_CODE",
    "SONDER_LAUNCHER_CONTROL_GATE",
    "SONDER_OPENAI_API_KEY",
    "SONDER_INFERENCE_API_KEY",
    "OPENROUTER_API_KEY",
    "SONDER_ANTHROPIC_API_KEY",
    "SONDER_ANTHROPIC_API_KEY_2",
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


# A PEM private-key block. Do not call ``_PEM.sub`` on untrusted text: with
# many BEGIN headers and no END after them it is quadratic, because each
# header scans lazily to the end of the text. Go through ``_sub_pem``.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# URL userinfo, ``scheme://user:pass@``. The scheme run is matched whole from
# its first character, one attempt per run, so the scan is linear. The older
# ``\b([a-z][a-z0-9+.-]*://)`` form retried the run from every inner word
# boundary and was quadratic on ``a.a.a...``. Go through ``_sub_url_userinfo``,
# which redacts only where that older form matched: a wider match here would
# consume a ``?password=`` the query-parameter rule after it must see.
_URL_USERINFO = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+)://([^/@\s:]+:[^/@\s]+)@")
# The same shape for any scheme run, applied after every other rule.
_URL_USERINFO_ANY = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+://)([^/@\s:]+:[^/@\s]+)@")
_SCHEME_LETTER = re.compile(r"(?i)[a-z]")
_WORD_CHAR = re.compile(r"\w")


def _older_scheme_start(text, start, run):
    r"""Whether ``\b[a-z]`` (the older anchor) holds anywhere in ``run``.

    ``run`` begins at ``text[start]``. Inside the run the only non-word
    characters are ``+``, ``.`` and ``-``, so a word boundary before a letter
    falls either at the run start (after a non-word character or the start of
    the text) or right after one of those three.
    """
    if _SCHEME_LETTER.match(run[0]) and not (start and _WORD_CHAR.match(text[start - 1])):
        return True
    return any(
        run[i - 1] in "+.-" and _SCHEME_LETTER.match(run[i]) for i in range(1, len(run))
    )


_SCHEME_RUN = re.compile(r"(?i)[a-z0-9+.-]+")
_USERINFO_NAME = re.compile(r"[^/@\s:]+")
_USERINFO_SECRET = re.compile(r"[^/@\s]+")


def _sub_url_userinfo(text):
    """Redact URL userinfo where the older anchor matched, in linear time.

    Scans scheme runs by hand instead of ``_URL_USERINFO.sub``: the run is a
    maximal ``[a-z0-9+.-]`` run (the pattern's lookbehind), ``://`` must follow
    it directly, then a maximal userinfo name, ``:``, a maximal secret and
    ``@``. None of those pieces can backtrack into a shorter one, so one pass
    over the text suffices. The older match started at some letter inside the
    same scheme run, so it ended at the same ``@`` and kept the run before it;
    keeping the whole run gives the same text. A match skipped here holds no
    other candidate: the userinfo cannot contain ``://``.
    """
    out = []
    copied = pos = 0
    while True:
        run = _SCHEME_RUN.search(text, pos)
        if run is None:
            break
        pos = run.end()
        if not text.startswith("://", pos):
            continue
        name = _USERINFO_NAME.match(text, pos + 3)
        if name is None or not text.startswith(":", name.end()):
            continue
        secret = _USERINFO_SECRET.match(text, name.end() + 1)
        if secret is None or not text.startswith("@", secret.end()):
            continue
        pos = secret.end() + 1
        if _older_scheme_start(text, run.start(), run.group()):
            out.append(text[copied:run.start()])
            out.append(run.group() + "://" + REDACTED)
            copied = pos
    out.append(text[copied:])
    return "".join(out)


_PATTERNS: tuple[re.Pattern, ...] = (
    # Keep the canonical marker handling idempotent (domain drift guard).
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\[REDACTED\](?=\s|$)|\S+(?:\s+\S+)?)"),
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b(bearer\s+)([a-z0-9._~+/=-]{8,})"),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
        r"passwd|credential|\b(?:pwd|(?:aws[-_])?secret[-_]access[-_]key))"
        r"[\"']?\s*[:=]\s*)"
        r"([\"']?[^\s\"',;}{]{4,}[\"']?)"
    ),
    _URL_USERINFO,
    re.compile(
        r"(?i)([?&](?:access[-_]?token|api[-_]?key|auth[-_]?secret|"
        r"password|credential)=)([^&#\s]+)"
    ),
    _PEM,
    # Free-standing provider keys (AKIA..., ghp_..., sk-..., JWTs): the same
    # known-credential formats the contribution privacy classifier refuses,
    # then runs of adjacent AWS keys.
    *REDACTION_FORMATS,
    # URL userinfo after a scheme run the rule above skips (``1http://``,
    # ``backup_s3://``). Last, so it cannot take text an earlier rule needs.
    _URL_USERINFO_ANY,
)

# Upper bound on pattern passes over one text. A substitution can expose a
# match for the next pass (``AKIA...Cookie: a=b`` needs two), so the passes
# repeat until one changes nothing or the finite bound below is reached. No
# finite bound covers every input, though: each ``x'`` unit after
# ``token='abcd'`` costs one more pass. Each pass replaces matched text with
# the marker, so reaching the bound preserves redactions from earlier
# passes. The bound does not guarantee that all newly exposed matches have
# been redacted: the result may be non-idempotent, and a second call can
# redact further.
_MAX_PASSES = 8

# Byte-identical to domain/security/redaction.py (see that copy's note): a
# second stage, run last (after the path-prefix labels), over the settled text
# that only ever adds redaction.
_CREDENTIAL_VALUE = re.compile(
    r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
    r"passwd|credential|\b(?:pwd|(?:aws[-_])?secret[-_]access[-_]key))"
    r"[\"']?\s*[:=]\s*)"
    r'''("(?:[^\s"\\]|\\(?!")){4,}"|'(?:[^\s'\\]|\\(?!')){4,}'|\\"(?:[^\s"\\]|\\(?!")){4,}\\"|\\'(?:[^\s'\\]|\\(?!')){4,}\\'|["']?[^\s"',;}{](?:["']*[^\s"',;}{]){3,}["']*)'''
)


def _sub_credential_values(text):
    """The second stage over settled text, repeated until stable (bounded)."""
    for _ in range(_MAX_PASSES):
        after = _CREDENTIAL_VALUE.sub(lambda m: m.group(1) + REDACTED, text)
        if after == text:
            break
        text = after
    return text

_PEM_BEGIN_MARK = "-----BEGIN "
_PEM_END_MARK = "-----END "
_PEM_LABEL = re.compile(r"[A-Z ]*")
_PEM_KEY = "PRIVATE KEY"


def _pem_marker_end(text, start):
    """End of ``[A-Z ]*PRIVATE KEY-----`` at ``start``, or -1.

    The label run stops at the first ``-``, so it is maximal and must itself
    end in ``PRIVATE KEY`` right before the five dashes.
    """
    label = _PEM_LABEL.match(text, start).end()
    if (
        label - start >= len(_PEM_KEY)
        and text.startswith(_PEM_KEY, label - len(_PEM_KEY))
        and text.startswith("-----", label)
    ):
        return label + 5
    return -1


def _sub_pem(text):
    """Same result as ``_PEM.sub(REDACTED, text)``, in linear time.

    Scans by hand: each BEGIN header takes the first END marker at or after
    its end (the lazy ``.*?``), and the scan resumes after that marker. A
    header with no END marker after it ends the scan, because every later
    header ends further on and would find none either.
    """
    out = []
    copied = pos = 0
    while True:
        begin = text.find(_PEM_BEGIN_MARK, pos)
        if begin < 0:
            break
        header_end = _pem_marker_end(text, begin + len(_PEM_BEGIN_MARK))
        if header_end < 0:
            pos = begin + 1
            continue
        end = -1
        probe = header_end
        while end < 0:
            marker = text.find(_PEM_END_MARK, probe)
            if marker < 0:
                break
            end = _pem_marker_end(text, marker + len(_PEM_END_MARK))
            probe = marker + 1
        if end < 0:
            break
        out.append(text[copied:begin])
        out.append(REDACTED)
        copied = pos = end
    out.append(text[copied:])
    return "".join(out)


def _apply_patterns(text):
    for pattern in _PATTERNS:
        if pattern is _PEM:
            text = _sub_pem(text)
        elif pattern is _URL_USERINFO:
            text = _sub_url_userinfo(text)
        elif pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


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
            # A substitution can create a new word boundary, so repeat the
            # pattern passes until stable (see _MAX_PASSES; mirrors the
            # domain layer).
            for _ in range(_MAX_PASSES):
                before = text
                text = _apply_patterns(text)
                if text == before:
                    break
            for prefix in self._path_prefixes:
                if prefix in text:
                    text = text.replace(prefix, "[WORKSPACE]")
            text = _sub_credential_values(text)
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
        # O_NOFOLLOW: a planted symlink must not redirect the log (and the
        # chmod below) to another file; opening one fails and the caller
        # falls back to logging without a file.
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND
            | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            REPL_LOG_FILE_MODE,
        )
        try:
            # A file created before this handler existed may be wider.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, REPL_LOG_FILE_MODE)
            else:
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
    - If the log file cannot be opened, the same console handler is used
      without a file (never JSON on an interactive terminal), a WARNING
      naming the reason is logged through it, and the reason is returned in
      the plan.
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
    file_handler = None
    path = None
    fallback_reason = None
    try:
        from sonder_runtime.platform.private_files import ensure_private_dir

        log_dir = ensure_private_dir(Path(home) / "logs")
        path = os.fspath(log_dir / REPL_LOG_NAME)
        file_handler = PrivateRotatingFileHandler(
            path, maxBytes=REPL_LOG_MAX_BYTES, backupCount=REPL_LOG_BACKUPS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError, TypeError) as exc:
        file_handler = None
        path = None
        fallback_reason = "%s: %s" % (type(exc).__name__, exc)
    if file_handler is not None:
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
    handlers = [h for h in (file_handler, console) if h is not None]
    root.setLevel(min(h.level for h in handlers))
    for old in list(root.handlers):
        if old not in handlers:
            try:
                old.close()
            except Exception:
                pass
    root.handlers[:] = handlers
    if fallback_reason is not None:
        logging.getLogger("sonder.repl.logging").warning(
            "REPL log file unavailable (%s); logs are not being saved",
            fallback_reason,
        )
        return ReplLoggingPlan(console_kind, None, None,
                               fallback_reason=fallback_reason)
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
