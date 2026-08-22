"""Thread-safe, bounded accumulation of long-running textual output.

Exact replay retains only per-chunk length and SHA-256 metadata. A bounded raw
preview window remains process-local while the stream is open, is passed through
a required host redactor for snapshots, and is discarded at terminal state.
Events contain identifiers, counters, and hashes, never output.
"""
from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from ..ports.event_sink import EventSink


_STREAM_ID = re.compile(r"^out_[0-9a-f]{32}$")
_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

HARD_MAX_CHUNK_BYTES = 1_048_576
HARD_MAX_TOTAL_BYTES = 16_777_216
HARD_MAX_CHUNKS = 10_000
HARD_MAX_PREVIEW_BYTES = 16_384
# The redactor sees bytes beyond the displayed preview so a credential that
# starts just before the display boundary is not cut into an unrecognizable,
# partially leaked token.  This source window remains bounded and is discarded
# when the stream becomes terminal.
PREVIEW_REDACTION_LOOKAHEAD_BYTES = 4_096


class OutputStreamError(RuntimeError):
    code = "OUTPUT_STREAM_ERROR"


class RevisionConflictError(OutputStreamError):
    code = "REVISION_CONFLICT"


class InvalidSequenceError(OutputStreamError):
    code = "INVALID_SEQUENCE"


class ConflictingReplayError(OutputStreamError):
    code = "CONFLICTING_REPLAY"


class TerminalStateError(OutputStreamError):
    code = "TERMINAL_STATE"


class OutputStreamState(str, Enum):
    OPEN = "open"
    FINALIZED = "finalized"
    FAILED = "failed"


@dataclass(frozen=True)
class OutputStreamId:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _STREAM_ID.fullmatch(self.value):
            raise ValueError("output stream ID is invalid")

    @classmethod
    def new(cls) -> "OutputStreamId":
        return cls("out_" + uuid.uuid4().hex)


@dataclass(frozen=True)
class OutputLimits:
    max_chunk_bytes: int = 65_536
    max_total_bytes: int = 1_048_576
    max_chunks: int = 1_024
    preview_bytes: int = 4_096

    def __post_init__(self) -> None:
        _bounded_positive("max_chunk_bytes", self.max_chunk_bytes, HARD_MAX_CHUNK_BYTES)
        _bounded_positive("max_total_bytes", self.max_total_bytes, HARD_MAX_TOTAL_BYTES)
        _bounded_positive("max_chunks", self.max_chunks, HARD_MAX_CHUNKS)
        _bounded_positive("preview_bytes", self.preview_bytes, HARD_MAX_PREVIEW_BYTES)
        if self.max_chunk_bytes > self.max_total_bytes:
            raise ValueError("max_chunk_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True)
class OutputSnapshot:
    stream_id: OutputStreamId
    state: OutputStreamState
    revision: int
    next_sequence: int
    chunk_count: int
    total_bytes: int
    sha256: str
    preview: str
    preview_truncated: bool
    failure_code: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-safe stream status envelope."""
        return {
            "stream_id": self.stream_id.value,
            "state": self.state.value,
            "revision": self.revision,
            "next_sequence": self.next_sequence,
            "chunk_count": self.chunk_count,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
            "preview": self.preview,
            "preview_truncated": self.preview_truncated,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True)
class AppendResult:
    snapshot: OutputSnapshot
    replayed: bool


PreviewRedactor = Callable[[str], str]


@dataclass(frozen=True)
class _SnapshotSeed:
    stream_id: OutputStreamId
    state: OutputStreamState
    revision: int
    next_sequence: int
    chunk_count: int
    total_bytes: int
    sha256: str
    preview_source: bytes | None
    terminal_preview: str | None
    terminal_preview_truncated: bool
    failure_code: str | None


class BoundedOutputAccumulator:
    """One stream with monotonic sequence numbers and revision-based CAS.

    Exact replay of an accepted sequence is idempotent even when the caller's
    expected revision is stale.  A different payload at that sequence is always
    rejected.  Every new mutation requires the current revision.
    """

    def __init__(
        self,
        stream_id: OutputStreamId,
        *,
        redactor: PreviewRedactor,
        events: EventSink,
        limits: OutputLimits | None = None,
    ) -> None:
        if not isinstance(stream_id, OutputStreamId):
            raise TypeError("stream_id must be an OutputStreamId")
        if not callable(redactor):
            raise TypeError("a preview redactor is required")
        if not callable(getattr(events, "emit", None)):
            raise TypeError("an event sink is required")
        self._stream_id = stream_id
        self._redactor = redactor
        self._events = events
        self._limits = limits or OutputLimits()
        self._lock = threading.Lock()
        self._state = OutputStreamState.OPEN
        self._revision = 0
        # Exact replay needs equality evidence, not a second copy of all output.
        # Retain only byte length + SHA-256 per sequence.
        self._chunks: dict[int, tuple[int, bytes]] = {}
        self._total_bytes = 0
        self._digest = hashlib.sha256()
        self._preview_source = bytearray()
        self._terminal_preview: tuple[str, bool] | None = None
        self._failure_code: str | None = None
        self._redaction_state = threading.local()

    def snapshot(self) -> OutputSnapshot:
        with self._lock:
            seed = self._snapshot_seed_locked()
        return self._render_snapshot(seed)

    def append(self, sequence: int, chunk: str, *, expected_revision: int) -> AppendResult:
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise InvalidSequenceError("chunk sequence must be a non-negative integer")
        if not isinstance(chunk, str):
            raise TypeError("output chunks must be text")
        # Every Unicode code point requires at least one UTF-8 byte.  Reject a
        # definitely oversized value before allocating/scanning its encoded
        # representation; accepted replay payloads can never satisfy this case.
        character_limit_exceeded = len(chunk) > self._limits.max_chunk_bytes
        encoded: bytes | None = None
        encoded_digest: bytes | None = None
        if not character_limit_exceeded:
            try:
                encoded = chunk.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("output chunk is not valid Unicode text") from exc
            encoded_digest = hashlib.sha256(encoded).digest()
        terminal_event = None
        replayed = False
        with self._lock:
            existing = self._chunks.get(sequence)
            if existing is not None:
                if character_limit_exceeded:
                    raise ConflictingReplayError(
                        "chunk sequence was replayed with different bytes"
                    )
                assert encoded is not None and encoded_digest is not None
                if existing != (len(encoded), encoded_digest):
                    raise ConflictingReplayError("chunk sequence was replayed with different bytes")
                seed = self._snapshot_seed_locked()
                replayed = True
            elif self._state is not OutputStreamState.OPEN:
                raise TerminalStateError("output stream is terminal")
            else:
                self._require_revision(expected_revision)
                if sequence != len(self._chunks):
                    raise InvalidSequenceError("chunk sequence is not the next monotonic value")

                if character_limit_exceeded:
                    failure_code = "CHUNK_BYTES_LIMIT"
                else:
                    assert encoded is not None
                    failure_code = self._limit_failure(encoded)
                if failure_code is not None:
                    self._state = OutputStreamState.FAILED
                    self._failure_code = failure_code
                    self._revision += 1
                    seed = self._snapshot_seed_locked()
                    terminal_event = "OUTPUT_STREAM_FAILED"
                else:
                    assert encoded is not None and encoded_digest is not None
                    self._chunks[sequence] = (len(encoded), encoded_digest)
                    self._total_bytes += len(encoded)
                    self._digest.update(encoded)
                    source_limit = min(
                        self._limits.max_total_bytes,
                        self._limits.preview_bytes + PREVIEW_REDACTION_LOOKAHEAD_BYTES,
                    )
                    remaining = source_limit - len(self._preview_source)
                    if remaining > 0:
                        self._preview_source.extend(encoded[:remaining])
                    self._revision += 1
                    seed = self._snapshot_seed_locked()

        snapshot = self._render_snapshot(seed)
        if terminal_event is not None:
            snapshot = self._seal_terminal_preview(snapshot)
            self._emit_terminal(terminal_event, snapshot)
            raise TerminalStateError("output stream limit exceeded")
        return AppendResult(snapshot, replayed=replayed)

    def finalize(self, *, expected_revision: int) -> OutputSnapshot:
        emit = False
        with self._lock:
            if self._state is OutputStreamState.FINALIZED:
                seed = self._snapshot_seed_locked()
            elif self._state is OutputStreamState.FAILED:
                raise TerminalStateError("failed output stream cannot be finalized")
            else:
                self._require_revision(expected_revision)
                self._state = OutputStreamState.FINALIZED
                self._revision += 1
                seed = self._snapshot_seed_locked()
                emit = True
        snapshot = self._render_snapshot(seed)
        snapshot = self._seal_terminal_preview(snapshot)
        if emit:
            self._emit_terminal("OUTPUT_STREAM_FINALIZED", snapshot)
        return snapshot

    def fail(self, failure_code: str, *, expected_revision: int) -> OutputSnapshot:
        if not isinstance(failure_code, str) or not _FAILURE_CODE.fullmatch(failure_code):
            raise ValueError("failure_code must be a bounded stable identifier")
        emit = False
        with self._lock:
            if self._state is OutputStreamState.FAILED:
                if self._failure_code != failure_code:
                    raise TerminalStateError("output stream already failed differently")
                seed = self._snapshot_seed_locked()
            elif self._state is OutputStreamState.FINALIZED:
                raise TerminalStateError("finalized output stream cannot fail")
            else:
                self._require_revision(expected_revision)
                self._state = OutputStreamState.FAILED
                self._failure_code = failure_code
                self._revision += 1
                seed = self._snapshot_seed_locked()
                emit = True
        snapshot = self._render_snapshot(seed)
        snapshot = self._seal_terminal_preview(snapshot)
        if emit:
            self._emit_terminal("OUTPUT_STREAM_FAILED", snapshot)
        return snapshot

    def _require_revision(self, expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision != self._revision
        ):
            raise RevisionConflictError("output stream revision does not match")

    def _limit_failure(self, encoded: bytes) -> str | None:
        if len(encoded) > self._limits.max_chunk_bytes:
            return "CHUNK_BYTES_LIMIT"
        if len(self._chunks) >= self._limits.max_chunks:
            return "CHUNK_COUNT_LIMIT"
        if self._total_bytes + len(encoded) > self._limits.max_total_bytes:
            return "TOTAL_BYTES_LIMIT"
        return None

    def _snapshot_seed_locked(self) -> _SnapshotSeed:
        terminal = self._terminal_preview
        return _SnapshotSeed(
            stream_id=self._stream_id,
            state=self._state,
            revision=self._revision,
            next_sequence=len(self._chunks),
            chunk_count=len(self._chunks),
            total_bytes=self._total_bytes,
            sha256=self._digest.hexdigest(),
            preview_source=None if terminal is not None else bytes(self._preview_source),
            terminal_preview=terminal[0] if terminal is not None else None,
            terminal_preview_truncated=terminal[1] if terminal is not None else False,
            failure_code=self._failure_code,
        )

    def _render_snapshot(self, seed: _SnapshotSeed) -> OutputSnapshot:
        if seed.terminal_preview is not None:
            redacted = seed.terminal_preview
            preview_truncated = seed.terminal_preview_truncated
        else:
            preview = (seed.preview_source or b"").decode("utf-8", errors="ignore")
            if getattr(self._redaction_state, "active", False):
                # A host redactor may call snapshot().  Fail the nested preview
                # closed without recursively invoking the same callback.
                redacted = "<preview redaction in progress>"
            else:
                self._redaction_state.active = True
                try:
                    redacted = self._redactor(preview)
                except Exception:
                    redacted = "<preview redaction failed>"
                finally:
                    self._redaction_state.active = False
            if not isinstance(redacted, str):
                redacted = "<preview redaction failed>"
            redacted, redactor_truncated = _truncate_utf8(
                redacted, self._limits.preview_bytes,
            )
            preview_truncated = (
                seed.total_bytes > self._limits.preview_bytes or redactor_truncated
            )
        return OutputSnapshot(
            stream_id=seed.stream_id,
            state=seed.state,
            revision=seed.revision,
            next_sequence=seed.next_sequence,
            chunk_count=seed.chunk_count,
            total_bytes=seed.total_bytes,
            sha256=seed.sha256,
            preview=redacted,
            preview_truncated=preview_truncated,
            failure_code=seed.failure_code,
        )

    def _seal_terminal_preview(self, snapshot: OutputSnapshot) -> OutputSnapshot:
        """Retain only redacted terminal display state, never raw preview bytes."""
        with self._lock:
            if self._state is not OutputStreamState.OPEN and self._terminal_preview is None:
                self._terminal_preview = (snapshot.preview, snapshot.preview_truncated)
                self._preview_source.clear()
            terminal = self._terminal_preview
        if terminal is None:
            return snapshot
        return replace(
            snapshot,
            preview=terminal[0],
            preview_truncated=terminal[1],
        )

    def _emit_terminal(self, event_code: str, snapshot: OutputSnapshot) -> None:
        try:
            self._events.emit(
                event_code,
                summary="bounded output stream reached a terminal state",
                detail={
                    "stream_id": snapshot.stream_id.value,
                    "state": snapshot.state.value,
                    "revision": snapshot.revision,
                    "chunk_count": snapshot.chunk_count,
                    "total_bytes": snapshot.total_bytes,
                    "sha256": snapshot.sha256,
                    "preview_truncated": snapshot.preview_truncated,
                    "failure_code": snapshot.failure_code,
                },
            )
        except Exception:
            # ADR-008: observability copies never determine business success.
            return


def _bounded_positive(name: str, value: int, hard_max: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > hard_max
    ):
        raise ValueError(f"{name} must be between 1 and {hard_max}")


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
