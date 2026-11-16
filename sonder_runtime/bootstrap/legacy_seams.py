"""Call-time lookups into a legacy namespace, for code moved out of ``server.py``.

``server.py`` builds a small dependency bundle for each family of code it hands
to a packaged module.  A bundle field that held the server function directly
would keep the object the name had when the bundle was built, so a later
``monkeypatch.setattr(server, name, fake)`` or live-reload rebind would never
reach the moved code.  These two helpers keep the lookup at the call instead:

``late(namespace, name)``
    A callable that looks ``name`` up in ``namespace`` each time it is called
    and forwards the call.
``value(namespace, name)``
    A zero-argument getter that returns the current ``namespace[name]``, for
    mutable or rebound state (``TIERS``, ``_RUNTIME_POLICY``, a module alias).

``server.py`` passes ``globals()`` as the namespace.  Neither helper reads the
namespace when it is created, so a bundle may be built before the names it
mentions exist.  A name that is missing when the call or the read happens
raises ``LegacySeamNameError`` (a ``KeyError``) that names it.

Both helpers hold no state of their own: the namespace is read, never written,
and no lock is taken.  A name that is rebound while a call is in flight is seen
by the next lookup; the call already running keeps the object it resolved.
Resolution is one mapping read, so the helpers are safe to call from any
thread as long as the namespace itself tolerates concurrent reads.

Call-site scanner note: ``tests/support/generation_call_sites.py`` records
``late``/``value`` with a literal tracked name as a site of that name in the
enclosing function, and requires a bundle field fed by ``late`` to carry the
server name (``sonder=late(globals(), "sonder")``).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = ("LegacySeamNameError", "late", "value")


class LegacySeamNameError(KeyError):
    """The namespace has no entry for a name a seam was asked to resolve."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return "legacy seam name %r is not defined in the namespace" % (self.name,)


def _resolve(namespace: Mapping[str, Any], name: str) -> Any:
    try:
        return namespace[name]
    except KeyError:
        raise LegacySeamNameError(name) from None


def late(namespace: Mapping[str, Any], name: str) -> Callable[..., Any]:
    """Return a callable that calls ``namespace[name]`` as it is at call time."""

    def call(*args: Any, **kwargs: Any) -> Any:
        return _resolve(namespace, name)(*args, **kwargs)

    return call


def value(namespace: Mapping[str, Any], name: str) -> Callable[[], Any]:
    """Return a getter for ``namespace[name]`` as it is at read time."""

    def read() -> Any:
        return _resolve(namespace, name)

    return read
