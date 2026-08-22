"""Policy for identifying environment names unsafe to pass to child tools."""

from __future__ import annotations


_UNSAFE_CHILD_SECRET_MARKERS = (
    "ACCESS_KEY", "API_KEY", "AUTH", "BEARER", "CONNECTION_STRING",
    "COOKIE", "CREDENTIAL", "DATABASE_URL", "PASSWORD", "PASSWD",
    "PRIVATE_KEY", "SECRET", "SESSION", "TOKEN",
)
_UNSAFE_CHILD_SECRET_SUFFIXES = ("_KEY", "_KEY_ID")
_UNSAFE_CHILD_CONTROL_MARKERS = (
    "APPROVAL", "BYPASS", "CONTROL", "DANGEROUS", "ELEVAT", "GATE",
    "PERMISSION", "UNSAFE",
)


def unsafe_child_secret_name(name: object) -> bool:
    """Return whether an environment name carries child-process authority."""
    upper = str(name or "").upper()
    return (
        upper.startswith("SONDER_")
        or any(marker in upper for marker in _UNSAFE_CHILD_SECRET_MARKERS)
        or upper.endswith(_UNSAFE_CHILD_SECRET_SUFFIXES)
        or any(marker in upper for marker in _UNSAFE_CHILD_CONTROL_MARKERS)
    )


__all__ = ["unsafe_child_secret_name"]
