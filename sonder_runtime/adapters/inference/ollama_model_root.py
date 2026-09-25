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

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable

import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_BLOB_NAME = re.compile(r"sha256[-:][0-9a-fA-F]{64}\Z")

Opener = Callable[..., object]


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
    """Return the daemon-reported model root, or ``None`` when unknown.

    Never raises for transport, policy, or payload faults; the caller labels
    its fallback instead.  ``opener`` defaults to the policy-enforcing
    ``ollama_endpoint.open_url``.
    """
    if not url or not ollama_endpoint.is_loopback(url):
        return None
    open_url = opener or ollama_endpoint.open_url
    base = url.rstrip("/")
    try:
        with open_url(
            urllib.request.Request(base + "/api/tags", method="GET"),
            timeout=timeout, allow_remote=allow_remote,
        ) as response:
            tags = json.loads(response.read(_MAX_RESPONSE_BYTES).decode("utf-8"))
        names = [
            str(item.get("name") or item.get("model") or "")
            for item in (tags.get("models") or [])
            if isinstance(item, dict)
        ]
        for name in [n for n in names if n][:3]:
            request = urllib.request.Request(
                base + "/api/show",
                data=json.dumps({"model": name}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with open_url(
                request, timeout=timeout, allow_remote=allow_remote,
            ) as response:
                shown = json.loads(
                    response.read(_MAX_RESPONSE_BYTES).decode("utf-8")
                )
            root = model_root_from_modelfile(
                shown.get("modelfile", "") if isinstance(shown, dict) else ""
            )
            if root:
                return root
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as exc:
        logger.debug(
            "daemon model-root discovery unavailable: %s", type(exc).__name__
        )
    return None


__all__ = ("discover_daemon_model_root", "model_root_from_modelfile")
