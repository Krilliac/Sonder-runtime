"""Typed errors of the security-hypothesis evaluation (subclasses of the common taxonomy)."""
from __future__ import annotations

from ..common.errors import Conflict, Forbidden, InvalidInput, NotFound


class DatasetError(InvalidInput):
    """The dataset is missing, oversize, unreadable, or holds no valid record."""


class DatasetRecordError(InvalidInput):
    """One dataset line is not a valid record; collected per line, not raised past the loader."""

    def __init__(self, message: str, line_no: int = 0) -> None:
        super().__init__(message)
        self.line_no = line_no


class RepoRootRefused(Forbidden):
    """A repo root is not absolute, not a plain directory, or outside the configured allowlist."""


class PathEscapeRefused(Forbidden):
    """A path would leave the repo root (``..``, absolute, link, junction, handle outside root)."""


class SourceRefused(Forbidden):
    """A file inside the repo is sensitive, secret or binary and is never read."""


class SourceUnavailable(NotFound):
    """The named file or line range does not exist or is too large to read."""


class OutputDirRefused(InvalidInput):
    """The output directory is unsafe (relative, linked, inside a repo root, or the dataset)."""


class ResidencyRefused(Forbidden):
    """The route may leave this machine or its residency is unknown; nothing was sent."""


class TemplateError(InvalidInput):
    """A prompt template has a missing or malformed field."""


class ResumeMismatch(Conflict):
    """The output directory holds rows produced by a different configuration."""


__all__ = [
    "DatasetError", "DatasetRecordError", "OutputDirRefused", "PathEscapeRefused",
    "RepoRootRefused", "ResidencyRefused", "ResumeMismatch", "SourceRefused",
    "SourceUnavailable", "TemplateError",
]
