"""Small, dependency-free digest primitive for packaged artifact adapters."""
from __future__ import annotations

import hashlib
from pathlib import Path


DEFAULT_CHUNK_SIZE = 262144


def file_sha256(path, chunk=DEFAULT_CHUNK_SIZE):
    """Return the SHA-256 hex digest of *path*, streaming in bounded chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


__all__ = ["DEFAULT_CHUNK_SIZE", "file_sha256"]
