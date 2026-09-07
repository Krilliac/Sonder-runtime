"""Opaque in-process capabilities for a private sealed artifact source.

The values returned to a producer or reader are data-free identity tokens.
They contain no binding, service, configuration, path, context, issuer, or
opposite-role reference.  The operation functions below are the only port
surface: a host composition registers a role-specific invocation behind the
opaque capability and revokes it when its binding closes.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable, NewType
from weakref import WeakKeyDictionary

from ..artifacts.mobility_source import MobilitySourceError, SourceArtifactRange


ArtifactMobilityPublisher = NewType("ArtifactMobilityPublisher", object)
ArtifactMobilityReader = NewType("ArtifactMobilityReader", object)


class _OpaqueMobilityPort:
    """An intentionally data-free identity token.

    This deliberately uses a normal instance rather than a class with hidden
    slots.  An issued value has an empty instance dictionary; adding caller
    data to that dictionary never changes host authority.  A weak registry
    means neither the registry nor its host callback owns the token.
    """


@dataclass(frozen=True)
class _IssuedPort:
    role: str
    invoke: Callable[..., object]


_PORTS: WeakKeyDictionary[_OpaqueMobilityPort, _IssuedPort] = WeakKeyDictionary()
_LOCK = RLock()


def _issue(role: str, invoke: Callable[..., object]) -> object:
    if role not in ("publish", "read") or not callable(invoke):
        raise TypeError("invalid artifact mobility port")
    # The weak registry retains authority separately, while a caller gets a
    # data-free, unforgeable identity token.  It has no binding, service,
    # configuration, path, issuer, opposite port, or callback reference.
    port = _OpaqueMobilityPort()
    with _LOCK:
        _PORTS[port] = _IssuedPort(role=role, invoke=invoke)
    return port


def _issue_publisher(invoke: Callable[..., object]) -> ArtifactMobilityPublisher:
    return ArtifactMobilityPublisher(_issue("publish", invoke))


def _issue_reader(invoke: Callable[..., object]) -> ArtifactMobilityReader:
    return ArtifactMobilityReader(_issue("read", invoke))


def _revoke(port: object) -> None:
    if type(port) is not _OpaqueMobilityPort:
        return
    with _LOCK:
        _PORTS.pop(port, None)


def _invoke(port: object, role: str, *args):
    # Accept only the exact token representation issued above.  This rejects
    # forged objects before their equality or hashing hooks can run against the
    # host registry.
    if type(port) is not _OpaqueMobilityPort:
        raise MobilitySourceError("FORBIDDEN")
    with _LOCK:
        issued = _PORTS.get(port)
    if issued is None or issued.role != role:
        raise MobilitySourceError("FORBIDDEN")
    return issued.invoke(*args)


def publish_sealed(
    port: ArtifactMobilityPublisher,
    stream,
    immutable_spec: dict,
    trusted_provenance: object,
) -> dict:
    """Publish only through a host-issued publisher capability."""
    return _invoke(port, "publish", stream, immutable_spec, trusted_provenance)


def inspect_sealed(port: ArtifactMobilityReader, source_artifact_id: str) -> dict:
    """Inspect only through a host-issued reader capability."""
    return _invoke(port, "read", source_artifact_id)


def read_range(
    port: ArtifactMobilityReader, source_artifact_id: str, offset: int, length: int
) -> SourceArtifactRange:
    """Read one bounded source range through a host-issued reader capability."""
    return _invoke(port, "read", source_artifact_id, offset, length)
