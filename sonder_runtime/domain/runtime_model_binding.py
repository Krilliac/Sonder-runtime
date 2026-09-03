"""Pure checks for binding a local tier to an installed catalog model.

Installation is matched with Ollama's ``:latest`` tag semantics, and a
positively declared non-chat model is rejected before an unusable policy is
persisted, with vision-only models allowed for the vision tier. Both checks
are explicit-input and side-effect free. Moved from ``server.py`` in the WP1
Three-Hundred-Tenth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

from sonder_runtime.domain.fanout_policy import nonchat_reason


def model_is_installed(model: str, installed) -> bool:
    requested = str(model or "").strip().casefold()
    available = {str(name or "").strip().casefold() for name in installed}
    if requested in available:
        return True
    # Ollama treats an omitted tag as :latest. Do not accept a different
    # installed tag merely because its repository/base name happens to match.
    if ":" not in requested:
        return "%s:latest" % requested in available
    if requested.endswith(":latest"):
        return requested[:-len(":latest")] in available
    return False


def model_capability_error(tier: str, model: str, records) -> str:
    """Return a proven capability mismatch for a local tier binding.

    Installation alone is not enough to make a model usable by a chat tier:
    an embedding model can be present in Ollama's catalog but cannot satisfy a
    workbench/code request.  Keep unknown catalog metadata compatible with
    existing local models, but reject an explicit non-chat declaration before
    persisting an unusable policy.  A vision tier is the one intentional
    exception: image-conditioned models may declare only ``vision`` while
    still being the correct target for a vision route.
    """
    for name, record in records:
        if not model_is_installed(model, (name,)):
            continue
        reason = nonchat_reason(record)
        if reason and not (tier == "vision" and "vision-only" in reason):
            return reason
        return ""
    return ""
