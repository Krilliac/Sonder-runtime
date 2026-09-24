"""Archive parsing with bounded metadata work.

``tarfile`` and ``zipfile`` do work proportional to attacker-declared sizes
before a caller sees a single member, so caller-side entry, byte, and ratio
limits run too late.  Every production archive reader goes through this
module so that the parser itself is bounded:

* TAR GNU long-name/long-link and PAX records are rejected when they declare
  more than :data:`MAX_TAR_METADATA_BYTES`, before their payload is read;
* at most :data:`MAX_TAR_METADATA_CHAIN` metadata records may precede one
  member.  ``tarfile`` processes each chained record by recursing, so an
  uncapped chain ends in ``RecursionError``;
* GNU sparse members are rejected outright.  Old-style ``S`` members chain
  extension blocks, and PAX ``GNU.sparse.*`` members, including format 1.0
  whose map sits in the member data, make ``tarfile`` read and parse a sparse
  map of attacker-chosen length.  No Sonder reader needs sparse files;
* a ZIP's end-of-central-directory record is read first, so the declared
  entry count and central-directory size can be checked before ``zipfile``
  materializes one ``ZipInfo`` per entry.
"""
from __future__ import annotations

import os
import tarfile
import zipfile
from typing import IO

MAX_TAR_METADATA_BYTES = 64 * 1024
MAX_TAR_METADATA_CHAIN = 4
# Central-directory bytes allowed per declared entry: a 46-byte fixed record
# plus name, extra field, and comment for ordinary archives.
MAX_ZIP_CENTRAL_DIRECTORY_BYTES_PER_ENTRY = 4096
_SPARSE_PREFIX = "GNU.sparse."
_CHAIN_ATTRIBUTE = "_sonder_metadata_chain"


class TarMetadataLimitError(tarfile.TarError):
    """TAR metadata exceeds what a bounded reader will parse.

    Deliberately not a ``ReadError``: ``tarfile.open("r:*")`` swallows
    ``ReadError`` while it probes compression methods, which would hide the
    cause behind a generic "could not be opened" message.
    """


class ZipCentralDirectoryLimitError(zipfile.BadZipFile):
    """A ZIP declares more central-directory entries or bytes than allowed."""


class BoundedTarInfo(tarfile.TarInfo):
    """``TarInfo`` whose metadata processing is size-, depth-, and type-bounded."""

    def _require_bounded_size(self) -> None:
        if not 0 <= self.size <= MAX_TAR_METADATA_BYTES:
            raise TarMetadataLimitError(
                "TAR metadata record declares %d bytes (maximum %d)"
                % (self.size, MAX_TAR_METADATA_BYTES)
            )

    def _chained(self, tarfile_, process):
        depth = getattr(tarfile_, _CHAIN_ATTRIBUTE, 0) + 1
        if depth > MAX_TAR_METADATA_CHAIN:
            raise TarMetadataLimitError(
                "more than %d TAR metadata records precede one member"
                % MAX_TAR_METADATA_CHAIN
            )
        setattr(tarfile_, _CHAIN_ATTRIBUTE, depth)
        try:
            return process(tarfile_)
        finally:
            setattr(tarfile_, _CHAIN_ATTRIBUTE, depth - 1)

    def _proc_gnulong(self, tarfile_):  # tarfile hook
        self._require_bounded_size()
        return self._chained(tarfile_, super()._proc_gnulong)

    def _proc_pax(self, tarfile_):  # tarfile hook
        self._require_bounded_size()
        result = self._chained(tarfile_, super()._proc_pax)
        # A global header stores its keys on the archive; reject a sparse
        # key there too, before any later member can use it.
        if any(str(key).startswith(_SPARSE_PREFIX) for key in tarfile_.pax_headers):
            raise TarMetadataLimitError("TAR GNU sparse metadata is not supported")
        return result

    def _proc_sparse(self, tarfile_):  # old-style GNU sparse ("S") member
        raise TarMetadataLimitError("TAR GNU sparse members are not supported")

    # PAX sparse formats 0.0, 0.1, and 1.0.  The 1.0 hook reads the map from
    # the member data, so rejecting here happens before that read.
    def _proc_gnusparse_00(self, next, raw_headers):
        raise TarMetadataLimitError("TAR GNU sparse metadata is not supported")

    def _proc_gnusparse_01(self, next, pax_headers):
        raise TarMetadataLimitError("TAR GNU sparse metadata is not supported")

    def _proc_gnusparse_10(self, next, pax_headers, tarfile_):
        raise TarMetadataLimitError("TAR GNU sparse metadata is not supported")

    def _apply_pax_info(self, pax_headers, encoding, errors):
        if any(str(key).startswith(_SPARSE_PREFIX) for key in pax_headers):
            raise TarMetadataLimitError("TAR GNU sparse metadata is not supported")
        try:
            return super()._apply_pax_info(pax_headers, encoding, errors)
        except (ValueError, TypeError) as exc:
            raise TarMetadataLimitError("TAR PAX metadata is malformed: %s" % exc) from None


def open_bounded(
    name: str | os.PathLike | None = None,
    mode: str = "r:*",
    fileobj: IO[bytes] | None = None,
) -> tarfile.TarFile:
    """Open a TAR archive for reading with bounded metadata processing."""
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
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return False


def zip_central_directory(path: str | os.PathLike) -> tuple[int, int]:
    """Return ``(declared entries, central-directory bytes)`` from the EOCD.

    Reads only the end-of-central-directory record (and its ZIP64 locator),
    never the central directory itself.
    """
    with open(path, "rb") as stream:
        record = zipfile._EndRecData(stream)
    if not record:
        raise zipfile.BadZipFile("ZIP end-of-central-directory record not found")
    entries = int(record[zipfile._ECD_ENTRIES_TOTAL])
    size = int(record[zipfile._ECD_SIZE])
    if entries < 0 or size < 0:
        raise zipfile.BadZipFile("ZIP central directory declares invalid sizes")
    return entries, size


def require_zip_entry_bound(path: str | os.PathLike, max_entries: int) -> int:
    """Reject a ZIP whose central directory exceeds ``max_entries`` before parsing it."""
    entries, size = zip_central_directory(path)
    if entries > max_entries:
        raise ZipCentralDirectoryLimitError(
            "ZIP declares %d entries (maximum %d)" % (entries, max_entries)
        )
    if size > max(1, entries) * MAX_ZIP_CENTRAL_DIRECTORY_BYTES_PER_ENTRY:
        raise ZipCentralDirectoryLimitError(
            "ZIP central directory is %d bytes for %d entries" % (size, entries)
        )
    return entries


__all__ = [
    "BoundedTarInfo",
    "MAX_TAR_METADATA_BYTES",
    "MAX_TAR_METADATA_CHAIN",
    "MAX_ZIP_CENTRAL_DIRECTORY_BYTES_PER_ENTRY",
    "TarMetadataLimitError",
    "ZipCentralDirectoryLimitError",
    "is_tarfile_bounded",
    "open_bounded",
    "require_zip_entry_bound",
    "zip_central_directory",
]
