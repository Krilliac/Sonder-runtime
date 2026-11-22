"""Policy for identifying environment names unsafe to pass to child tools."""

from __future__ import annotations


_UNSAFE_CHILD_SECRET_MARKERS = (
    "ACCESS_KEY", "API_KEY", "APIKEY", "AUTH", "BEARER", "CONNECTION_STRING",
    "COOKIE", "CREDENTIAL", "DATABASE_URL", "PASSWORD", "PASSWD",
    "PRIVATE_KEY", "SECRET", "SESSION", "TOKEN",
)
_UNSAFE_CHILD_SECRET_SUFFIXES = ("_KEY", "_KEY_ID")
# Matched against whole "_"-separated segments, never as substrings: PAT must
# hit GITHUB_PAT and MY_PAT_VALUE but not PATH, PATHEXT or PATTERN.
_UNSAFE_CHILD_SECRET_SEGMENTS = frozenset({"PAT", "DSN"})
# Search-path and system-location variables whose values are never credentials.
# They are exempt from the URL-userinfo value check so a share path such as
# file://host/... in PATH can never strip the variable a child needs to run.
_SEARCH_PATH_NAMES = frozenset({
    "PATH", "PATHEXT", "PYTHONPATH", "NODE_PATH", "HOMEPATH", "PSMODULEPATH",
    "COMSPEC", "SYSTEMROOT", "VIRTUAL_ENV", "PYTHONHOME",
})
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
        or not _UNSAFE_CHILD_SECRET_SEGMENTS.isdisjoint(upper.split("_"))
        or any(marker in upper for marker in _UNSAFE_CHILD_CONTROL_MARKERS)
    )


def _value_has_url_userinfo(text: str) -> bool:
    """Return whether any ``scheme://...@`` in text carries userinfo.

    No urllib here (architecture rule: network modules live in adapters). Every
    ``://`` is checked, so a list (``A_URLS=a://x,b://u:p@y``) cannot hide its
    second entry. Any "@" after "://" and before the query or fragment is
    treated as userinfo. That is deliberately conservative: it also covers
    passwords containing "/" or "," or longer than any fixed bound, token-only
    userinfo, and unparseable hosts, at the cost of dropping a value whose URL
    path contains "@".
    """
    for segment in text.split("://")[1:]:
        if "@" in segment.split("?", 1)[0].split("#", 1)[0]:
            return True
    return False


def unsafe_child_environment_entry(name: object, value: object) -> bool:
    """Return whether an environment entry must not reach a child process.

    Name-based rules first; then, under ANY name except the search-path
    variables, a value with URL userinfo (a user, a password, or a token-only
    ``scheme://token@host``): HTTP_PROXY, DATABASE, CELERY_BROKER, *_URLS and
    MONGOURI carry ``scheme://user:pass@host`` as often as *_URL does.
    """
    if unsafe_child_secret_name(name):
        return True
    if str(name or "").upper() in _SEARCH_PATH_NAMES:
        return False
    return _value_has_url_userinfo(str(value or ""))


__all__ = ["unsafe_child_environment_entry", "unsafe_child_secret_name"]
