"""Host-issued live cutover capability; never reconstructed from operator input."""

from .child_migration import MigrationRefused, MigrationUnsupported, digest
from weakref import WeakKeyDictionary, WeakMethod, ref
from threading import RLock

_ISSUER = object()
_ISSUERS = WeakKeyDictionary()
_LOCK = RLock()


def _register_host_issuer(issuer, validate):
    """Trusted constructor hook after acquiring a new namespace launch gate."""
    with _LOCK:
        if issuer in _ISSUERS:
            raise MigrationRefused("migration issuer already registered")
        _ISSUERS[issuer] = WeakMethod(validate)


def _unregister_host_issuer(issuer):
    with _LOCK:
        _ISSUERS.pop(issuer, None)


def _require_issuer(issuer, manifest):
    with _LOCK:
        try:
            validate = _ISSUERS.get(issuer)
        except TypeError:
            validate = None
        validate = validate() if validate is not None else None
    if validate is None:
        raise MigrationUnsupported("migration issuer is not live or registered")
    validate(manifest)


class MigrationQuiescenceGuard:
    __slots__ = ("_manifest", "_validate", "_issuer")

    def __init__(self, token, issuer, manifest):
        if token is not _ISSUER:
            raise MigrationUnsupported(
                "activation requires a live host quiescence issuer"
            )
        self._issuer, self._manifest, self._validate = (
            token,
            digest(manifest),
            ref(issuer),
        )

    def require(self, manifest):
        if self._issuer is not _ISSUER or self._manifest != digest(manifest):
            raise MigrationRefused("activation capability scope changed")
        _require_issuer(self._validate(), manifest)


def issue_host_guard(issuer, manifest):
    """Composition-internal call after a host proves cleanup and launch exclusion."""
    _require_issuer(issuer, manifest)
    return MigrationQuiescenceGuard(_ISSUER, issuer, manifest)


def require_host_guard(guard, manifest):
    if type(guard) is not MigrationQuiescenceGuard:
        raise MigrationUnsupported(
            "configured service manager cannot prove migration quiescence"
        )
    guard.require(manifest)
