"""Output digest use cases: guarded file, owned job output, or plain text.

Every text is redacted *before* it is parsed, so no digest field -- failure
lines, tail, diagnostic messages, group templates, labels -- can carry an
unredacted value. A redactor failure yields its own sentinel text, which is
digested as-is and never replaced by the original.

Job digests for a model or typed caller are limited to test-run kinds the
same principal owns; any other job answers ``NotFound`` exactly as a missing
job does. The local REPL operator (``operator=True``) may digest any job.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
)
from ...domain.diagnostics.digest import OutputDigest, digest_text, render_digest
from ..context import OperationContext
from .ports import JobOutputReader, TextWindow, TextWindowSource


MODEL_DIGEST_JOB_KINDS = frozenset({"tool.test_run", "agent_lane.test"})

JOB_OUTPUT_MAX_BYTES = 2_000_000
JOB_OUTPUT_HEAD_BYTES = 65_536
FILE_MAX_SCAN_BYTES = 4_000_000
FILE_TAIL_LINES = 50_000
FILE_TIMEOUT_SECONDS = 5.0
TEXT_MAX_CHARS = 8_000_000
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_MAX_PATH_CHARS = 1_024


class DigestSourceRejected(Forbidden, PermissionError):
    """The requested file is outside the guarded digest surface."""

    code = "DIGEST_SOURCE_REJECTED"


def _clamp(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


class OutputDigestService:
    """Digest bounded windows of guarded files, owned jobs, or given text."""

    def __init__(
        self,
        files: TextWindowSource,
        jobs: JobOutputReader | None,
        *,
        redact: Callable[[str], str],
    ) -> None:
        if not callable(redact):
            raise TypeError("redact must be callable")
        self._files = files
        self._jobs = jobs
        self._redact = redact

    def _clean(self, text: str) -> str:
        return self._redact(str(text or ""))

    def digest_file(
        self,
        path: str,
        context: OperationContext,
        *,
        tail_lines: int = 20,
        max_failure_lines: int = 40,
        max_scan_bytes: int = FILE_MAX_SCAN_BYTES,
    ) -> OutputDigest:
        text = str(path or "").strip()
        if not text or len(text) > _MAX_PATH_CHARS or "\x00" in text:
            raise InvalidInput("digest path must be a non-empty path under 1024 chars")
        self._require_live(context)
        try:
            window = self._files.read_file_window(
                text,
                extra_roots="",
                max_scan_bytes=_clamp(max_scan_bytes, FILE_MAX_SCAN_BYTES, 1, FILE_MAX_SCAN_BYTES),
                tail_lines=FILE_TAIL_LINES,
                timeout_seconds=FILE_TIMEOUT_SECONDS,
            )
        except DigestSourceRejected:
            raise
        except (PermissionError, OSError, ValueError) as exc:
            raise DigestSourceRejected(
                "digest source rejected (%s)" % type(exc).__name__
            ) from exc
        return self._digest_window(
            window, "file", tail_lines=tail_lines, max_failure_lines=max_failure_lines,
        )

    def digest_job(
        self,
        job_id: str,
        context: OperationContext,
        *,
        tail_lines: int = 20,
        max_failure_lines: int = 40,
        operator: bool = False,
    ) -> OutputDigest:
        if self._jobs is None:
            raise DependencyUnavailable("job output is not available in this runtime")
        identifier = str(job_id or "").strip()
        if not _JOB_ID_RE.match(identifier):
            raise NotFound("job not found")
        self._require_live(context)
        metadata = self._jobs.job_metadata(identifier)
        if metadata is None:
            raise NotFound("job not found")
        if operator is not True:
            if (
                metadata.get("kind") not in MODEL_DIGEST_JOB_KINDS
                or metadata.get("principal_id") != context.principal_id
            ):
                # Indistinguishable from a missing job on purpose.
                raise NotFound("job not found")
        try:
            window = self._jobs.read_output(
                identifier, max_bytes=JOB_OUTPUT_MAX_BYTES, head_bytes=JOB_OUTPUT_HEAD_BYTES,
            )
        except KeyError as exc:
            raise NotFound("job not found") from exc
        return self._digest_window(
            window, "job", tail_lines=tail_lines, max_failure_lines=max_failure_lines,
        )

    def digest_text(self, text: str, *, label: str = "", tail_lines: int = 20) -> OutputDigest:
        raw = str(text or "")
        truncated = False
        if len(raw) > TEXT_MAX_CHARS:
            half = TEXT_MAX_CHARS // 2
            raw = raw[:half] + "\n" + raw[-half:]
            truncated = True
        cleaned = self._clean(raw)
        return digest_text(
            cleaned,
            source_kind="text",
            source_label=self._clean(label),
            tail_lines=tail_lines,
            scan_truncated=truncated,
            bytes_scanned=len(raw.encode("utf-8", errors="replace")),
        )

    def _digest_window(
        self, window: TextWindow, kind: str, *, tail_lines: int, max_failure_lines: int,
    ) -> OutputDigest:
        return digest_text(
            self._clean(window.text),
            source_kind=kind,
            source_label=self._clean(window.label),
            tail_lines=tail_lines,
            max_failure_lines=max_failure_lines,
            scan_truncated=bool(window.truncated),
            bytes_scanned=max(0, int(window.bytes_read)),
        )

    @staticmethod
    def _require_live(context: OperationContext) -> None:
        if context.cancellation.cancelled:
            raise Cancelled("digest request was cancelled")
        if context.expired:
            raise DeadlineExceeded("digest request deadline expired")


def render_output_digest(digest: OutputDigest, *, max_chars: int = 4000) -> str:
    """Text form of a digest for interfaces that may not import the domain."""
    return render_digest(digest, max_chars=max_chars)


__all__ = [
    "DigestSourceRejected", "MODEL_DIGEST_JOB_KINDS", "OutputDigestService",
    "render_output_digest",
]
