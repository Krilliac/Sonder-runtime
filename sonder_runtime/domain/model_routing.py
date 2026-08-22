"""Pure model-name classification used by runtime routing."""

from __future__ import annotations


def is_cloud_model_name(model) -> bool:
    """Return whether an Ollama model name denotes a hosted/cloud model.

    This intentionally preserves the server boundary's historical lexical
    contract: hosted names contain ``-cloud`` or end in ``:cloud``.  It does
    not validate, normalize, or select a model.
    """
    name = (model or "").lower()
    return "-cloud" in name or name.endswith(":cloud")
