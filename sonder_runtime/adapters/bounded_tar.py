"""TAR reading with bounded header metadata.

``tarfile`` reads a GNU long-name/long-link record or a PAX extended header
by calling ``fileobj.read(size)`` with the size declared in that record.
Nothing bounds that size, so a small compressed archive can declare tens of
MiB of highly compressible metadata and make the parser allocate them
before any caller-side entry, byte, or ratio limit runs.  Every production
TAR reader opens archives through :func:`open_bounded` so those records are
rejected by their declared size before their payload is read.
"""
from __future__ import annotations

import os
import tarfile
from typing import IO

MAX_TAR_METADATA_BYTES = 64 * 1024


class TarMetadataLimitError(tarfile.TarError):
    """A TAR metadata record declares more bytes than the reader permits.

    Deliberately not a ``ReadError``: ``tarfile.open("r:*")`` swallows
    ``ReadError`` while it probes compression methods, which would hide the
    cause behind a generic "could not be opened" message.
    """


class BoundedTarInfo(tarfile.TarInfo):
    """``TarInfo`` that refuses oversized long-name, long-link, and PAX records."""

    def _require_bounded_metadata(self) -> None:
        if not 0 <= self.size <= MAX_TAR_METADATA_BYTES:
            raise TarMetadataLimitError(
                "TAR metadata record declares %d bytes (maximum %d)"
                % (self.size, MAX_TAR_METADATA_BYTES)
            )

    def _proc_gnulong(self, tarfile_):  # noqa: D401 - tarfile hook
        self._require_bounded_metadata()
        return super()._proc_gnulong(tarfile_)

    def _proc_pax(self, tarfile_):  # noqa: D401 - tarfile hook
        self._require_bounded_metadata()
        return super()._proc_pax(tarfile_)


def open_bounded(
    name: str | os.PathLike | None = None,
    mode: str = "r:*",
    fileobj: IO[bytes] | None = None,
) -> tarfile.TarFile:
    """Open a TAR archive for reading with bounded header metadata."""
    if not mode.startswith("r"):
        raise ValueError("open_bounded only reads archives")
    return tarfile.open(name, mode, fileobj=fileobj, tarinfo=BoundedTarInfo)


def is_tarfile_bounded(name: str | os.PathLike) -> bool:
    """Return whether ``name`` opens as a TAR archive.

    Raises :class:`TarMetadataLimitError` instead of returning ``False`` so
    callers can report the real reason for the rejection.
    """
    try:
        with open_bounded(name):
            return True
    except TarMetadataLimitError:
        raise
    except (tarfile.TarError, OSError, EOFError):
        return False


__all__ = [
    "BoundedTarInfo",
    "MAX_TAR_METADATA_BYTES",
    "TarMetadataLimitError",
    "is_tarfile_bounded",
    "open_bounded",
]
