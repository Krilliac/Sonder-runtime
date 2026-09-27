"""Bounded, explicit interchange between editors and agent runtimes.

The protocol deliberately treats imported instruction files as data.  It does
not execute front matter, follow links, or permit paths to escape the caller's
chosen root.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

MAX_PATH_LENGTH = 512
MAX_CONTENT_LENGTH = 256 * 1024
MAX_DOCUMENTS = 128
PROTOCOL_VERSION = "1"
_ALLOWED_NAMES = {"AGENTS.md", "SKILL.md"}
_ALLOWED_SUFFIXES = {".json", ".md", ".rule", ".rules", ".yaml", ".yml"}


class EditorInteropError(ValueError):
    """Raised when an editor interchange payload is malformed or unsafe."""


def _bounded_text(value: Any, *, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise EditorInteropError(f"{label} must be a non-empty string <= {limit} bytes")
    if len(value.encode("utf-8")) > limit:
        raise EditorInteropError(f"{label} exceeds {limit}-byte limit")
    return value


def _safe_relative_path(value: str) -> str:
    value = _bounded_text(value, label="path", limit=MAX_PATH_LENGTH).replace("\\", "/")
    path = Path(value)
    if path.is_absolute() or value.startswith("/") or ":" in value.split("/")[0]:
        raise EditorInteropError("path must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise EditorInteropError("path contains an unsafe segment")
    return "/".join(parts)


@dataclass(frozen=True)
class ProtocolEnvelope:
    """Versioned message envelope with bounded, JSON-compatible payload."""

    message_type: str
    payload: Mapping[str, Any]
    message_id: str
    protocol_version: str = PROTOCOL_VERSION

    @classmethod
    def create(cls, message_type: str, payload: Mapping[str, Any]) -> "ProtocolEnvelope":
        return cls(message_type, dict(payload), str(uuid4()))

    def __post_init__(self) -> None:
        _bounded_text(self.protocol_version, label="protocol_version", limit=16)
        _bounded_text(self.message_type, label="message_type", limit=96)
        try:
            UUID(self.message_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise EditorInteropError("message_id must be a UUID") from exc
        if not isinstance(self.payload, Mapping) or len(self.payload) > 128:
            raise EditorInteropError("payload must be a mapping with at most 128 keys")
        try:
            encoded = json.dumps(self.payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise EditorInteropError("payload must be JSON-compatible") from exc
        if len(encoded.encode("utf-8")) > MAX_CONTENT_LENGTH:
            raise EditorInteropError("payload exceeds content limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "message_id": self.message_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProtocolEnvelope":
        if not isinstance(value, Mapping):
            raise EditorInteropError("envelope must be an object")
        required = {"protocol_version", "message_type", "message_id", "payload"}
        if set(value) != required:
            raise EditorInteropError("envelope has missing or unknown fields")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise EditorInteropError("unsupported protocol version")
        return cls(
            str(value["message_type"]),
            value["payload"],
            str(value["message_id"]),
            str(value["protocol_version"]),
        )


@dataclass(frozen=True)
class ImplementationInfo:
    """Bounded ACP-compatible peer identity exchanged during initialization."""

    name: str
    version: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _bounded_text(self.name, label="implementation name", limit=64)
        _bounded_text(self.version, label="implementation version", limit=64)
        valid = all(
            isinstance(item, str)
            and 0 < len(item) <= 64
            and all(char.isalnum() or char in "._-" for char in item)
            for item in self.capabilities
        )
        if len(self.capabilities) > 64 or not valid:
            raise EditorInteropError("capabilities must be bounded identifiers")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ImplementationInfo":
        """Decode peer metadata without accepting extension-shaped fields."""
        if not isinstance(value, Mapping) or set(value) != {"name", "version", "capabilities"}:
            raise EditorInteropError("implementation metadata has missing or unknown fields")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, (list, tuple, set, frozenset)):
            raise EditorInteropError("implementation capabilities must be a sequence")
        if any(not isinstance(item, str) for item in capabilities):
            raise EditorInteropError("implementation capabilities must be strings")
        return cls(str(value["name"]), str(value["version"]), frozenset(capabilities))

    def negotiate(self, peer: "ImplementationInfo") -> frozenset[str]:
        """Return capabilities explicitly advertised by both peers."""
        if not isinstance(peer, ImplementationInfo):
            raise TypeError("peer must be ImplementationInfo")
        return frozenset(self.capabilities & peer.capabilities)


@dataclass(frozen=True)
class CancellationRequest:
    """Per-request cancellation message for reconnectable editor sessions."""

    request_id: str
    session_id: str
    reason: str = "cancelled"

    def __post_init__(self) -> None:
        try:
            UUID(self.request_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise EditorInteropError("request_id must be a UUID") from exc
        _bounded_text(self.session_id, label="session_id", limit=128)
        _bounded_text(self.reason, label="reason", limit=256)

    def to_envelope(self) -> ProtocolEnvelope:
        return ProtocolEnvelope(
            "session/cancel_request",
            {
                "request_id": self.request_id,
                "session_id": self.session_id,
                "reason": self.reason,
            },
            str(uuid4()),
        )


@dataclass(frozen=True)
class RuleDocument:
    path: str
    content: str
    kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_relative_path(self.path))
        _bounded_text(self.content, label="content", limit=MAX_CONTENT_LENGTH)
        _bounded_text(self.kind, label="kind", limit=32)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _document_kind(path: str) -> str:
    name = Path(path).name
    if name in _ALLOWED_NAMES:
        return name[:-3].lower()
    return Path(path).suffix.lower().lstrip(".")


_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL
    | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _same_file(opened: os.stat_result, path: Path) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _contained(path: Path, root_path: Path) -> Path:
    """Resolve ``path`` strictly and require it to stay below ``root_path``."""
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_path)
    except (OSError, ValueError) as exc:
        raise EditorInteropError("document is missing or outside root") from exc
    return resolved


def _read_bounded(path: Path, root_path: Path) -> str:
    """Read at most MAX_CONTENT_LENGTH bytes from the validated file.

    The descriptor is bound to the checked path: after opening, the path is
    re-resolved inside the root and must still name the very file that was
    opened, so a component swapped between check and open is refused.
    """
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise EditorInteropError("document could not be opened") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise EditorInteropError("document is not a regular file")
        if not _same_file(opened, _contained(path, root_path)):
            raise EditorInteropError("document changed during import")
        if opened.st_size > MAX_CONTENT_LENGTH:
            raise EditorInteropError(
                f"content exceeds {MAX_CONTENT_LENGTH}-byte limit"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONTENT_LENGTH + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_CONTENT_LENGTH:
        raise EditorInteropError(f"content exceeds {MAX_CONTENT_LENGTH}-byte limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditorInteropError("document is not valid UTF-8") from exc


def import_documents(root: str | Path, paths: Iterable[str]) -> tuple[RuleDocument, ...]:
    """Safely import bounded rule files below ``root``."""
    root_path = Path(root).resolve()
    requested = list(paths)
    if len(requested) > MAX_DOCUMENTS:
        raise EditorInteropError("document count exceeds limit")
    result: list[RuleDocument] = []
    for raw_path in requested:
        relative = _safe_relative_path(raw_path)
        resolved = _contained(root_path / relative, root_path)
        if resolved.name not in _ALLOWED_NAMES and resolved.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise EditorInteropError("unsupported rule file format")
        content = _read_bounded(resolved, root_path)
        result.append(RuleDocument(relative, content, _document_kind(relative)))
    return tuple(result)


def _write_contained(destination: Path, content: str, root_path: Path) -> None:
    """Write ``content`` to ``destination`` without following a swapped path.

    The bytes go to a fresh exclusive temp file in the destination directory;
    before any content is written, that directory must still resolve inside
    the root and still contain the very file that was created.  The final
    rename replaces the name itself and never writes through a symlink.
    """
    parent = _contained(destination.parent, root_path)
    temp = parent / f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        fd = os.open(temp, _CREATE_FLAGS, 0o666)
    except OSError as exc:
        raise EditorInteropError("document could not be written") from exc
    published = False
    try:
        try:
            created = os.fstat(fd)
            if not _same_file(created, _contained(destination.parent, root_path) / temp.name):
                raise EditorInteropError("document is outside root")
            view = memoryview(content.encode("utf-8"))
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        final_parent = _contained(destination.parent, root_path)
        if final_parent != parent:
            raise EditorInteropError("document is outside root")
        os.replace(final_parent / temp.name, final_parent / destination.name)
        published = True
    finally:
        if not published:
            try:
                os.unlink(temp)
            except OSError:
                pass


def export_documents(root: str | Path, documents: Iterable[RuleDocument]) -> tuple[str, ...]:
    """Write validated documents below ``root`` and return normalized paths."""
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    docs = tuple(documents)
    if len(docs) > MAX_DOCUMENTS:
        raise EditorInteropError("document count exceeds limit")
    written: list[str] = []
    for document in docs:
        if not isinstance(document, RuleDocument):
            raise EditorInteropError("documents must be RuleDocument values")
        relative = _safe_relative_path(document.path)
        if Path(relative).name not in _ALLOWED_NAMES and Path(relative).suffix.lower() not in _ALLOWED_SUFFIXES:
            raise EditorInteropError("unsupported rule file format")
        destination = root_path / relative
        try:
            destination.resolve().relative_to(root_path)
        except ValueError as exc:
            raise EditorInteropError("document is outside root") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_contained(destination, document.content, root_path)
        written.append(relative)
    return tuple(written)
