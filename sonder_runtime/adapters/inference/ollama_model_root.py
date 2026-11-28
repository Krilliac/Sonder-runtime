"""Discover the model root a running local Ollama daemon actually uses.

``OLLAMA_MODELS`` is read by the *daemon* process.  A diagnostic process such
as ``sonder doctor`` usually does not share the daemon's environment, so
resolving the root from its own environment silently reports Ollama's
compiled-in default (``~/.ollama/models``) even when the daemon stores its
blobs elsewhere.  Ollama does not expose the root directly, but ``/api/show``
returns a generated Modelfile whose ``FROM`` line names the absolute blob path
(``<root>/blobs/sha256-...``) for a locally stored model.  That is read-only
and never loads a model.

Only a loopback daemon is consulted: a remote daemon's paths name another
machine's filesystem and cannot be measured here.
"""
from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_BLOB_NAME = re.compile(r"sha256[-:][0-9a-fA-F]{64}\Z")


class _Response(Protocol):
    def read(self, amt: int, /) -> bytes: ...


class Opener(Protocol):
    def __call__(
        self,
        request: urllib.request.Request,
        /,
        *,
        timeout: float,
        allow_remote: bool,
    ) -> AbstractContextManager[_Response]: ...


def model_root_from_modelfile(modelfile: str) -> str | None:
    """Return ``<root>`` from the first ``FROM <root>/blobs/sha256-...`` line."""
    for line in str(modelfile or "").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("FROM "):
            continue
        target = stripped[5:].strip().strip('"')
        # Pick the flavour from the path's own shape so a Windows daemon's
        # path is split correctly on any host.
        windows = len(target) > 2 and target[1] == ":" or "\\" in target
        path = PureWindowsPath(target) if windows else PurePosixPath(target)
        if not path.is_absolute() or not _BLOB_NAME.match(path.name):
            continue
        if path.parent.name.lower() != "blobs":
            continue
        return str(path.parent.parent)
    return None


def discover_daemon_model_root(
    url: str,
    *,
    allow_remote: bool = False,
    timeout: float = 2.0,
    opener: Opener | None = None,
) -> str | None:
    """Local Ollama daemon model-root discovery is retired; SI model store owns paths."""
    return None


__all__ = ("discover_daemon_model_root", "model_root_from_modelfile")
