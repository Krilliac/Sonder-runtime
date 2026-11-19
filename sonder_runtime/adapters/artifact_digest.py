"""Small, dependency-free digest primitive for packaged artifact adapters."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os


DEFAULT_CHUNK_SIZE = 262144


def file_sha256(path: str | os.PathLike[str], chunk: int = DEFAULT_CHUNK_SIZE) -> str:
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
