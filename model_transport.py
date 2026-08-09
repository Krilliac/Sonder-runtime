"""Lazy compatibility facade for the packaged transport error adapter."""
from __future__ import annotations

__all__ = ["ModelCallError"]


def __dir__():
    return sorted(set(globals()) | set(__all__))


def __getattr__(name: str):
    if name != "ModelCallError":
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    from sonder_runtime.adapters.model_transport import ModelCallError

    # Cache the alias so repeated lookups and server hot reloads retain exact
    # class identity without re-entering the compatibility path.
    globals()[name] = ModelCallError
    return ModelCallError
