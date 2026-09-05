"""Immutable private configuration for the supported foreground owner profile."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from ..application.ports.runtime_owner import OwnerRefused, canonical
from ..application.ports.managed_runtime_owner import config_reference
from ..application.runtime_resources import ApplicationResourceOwners
from ..platform.child_storage_config import ChildStorageConfig, child_storage_errors


COMPONENTS = ("application", "child-storage", "http-sockets", "providers", "sqlite", "workers")
CLOSE_ORDER = ("child-storage", "providers", "workers", "http-sockets", "sqlite", "application")
MANIFEST_DIGEST = ApplicationResourceOwners(COMPONENTS, close_order=CLOSE_ORDER).manifest_digest


def validate_configuration(value, *, root, namespace, incarnation):
    keys = {"schema", "namespace", "incarnation", "port", "request_timeout_seconds", "stream_idle_timeout_seconds", "components", "child_storage", "child_path", "child_identity", "artifact_digest"}
    if type(value) is not dict or set(value) != keys:
        raise OwnerRefused("exact managed configuration required")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["namespace"] != namespace or value["incarnation"] != incarnation or value["components"] != MANIFEST_DIGEST:
        raise OwnerRefused("managed configuration ownership or manifest changed")
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise OwnerRefused("fixed numeric loopback port required")
    if any(type(value[name]) is not int or value[name] != 5 for name in ("request_timeout_seconds", "stream_idle_timeout_seconds")):
        raise OwnerRefused("managed HTTP timeout profile changed")
    if type(value["child_identity"]) is not str or len(value["child_identity"]) != 64 or any(ch not in "0123456789abcdef" for ch in value["child_identity"]):
        raise OwnerRefused("managed child identity is invalid")
    if type(value["artifact_digest"]) is not str or len(value["artifact_digest"]) != 64 or any(ch not in "0123456789abcdef" for ch in value["artifact_digest"]):
        raise OwnerRefused("managed artifact identity is invalid")
    try:
        storage = ChildStorageConfig(**value["child_storage"])
        if child_storage_errors(SimpleNamespace(child_storage=storage)):
            raise ValueError()
    except (TypeError, ValueError):
        raise OwnerRefused("managed child storage policy is invalid") from None
    if storage.backend == "sqlite":
        if type(value["child_path"]) is not str:
            raise OwnerRefused("exact SQLite path required")
        path = Path(value["child_path"])
        if not path.is_absolute() or path.parent != Path(root).absolute() or path.suffix != ".sqlite":
            raise OwnerRefused("child database must be in the exact owned namespace")
        if sha256(str(path).encode()).hexdigest() != value["child_identity"]:
            raise OwnerRefused("managed SQLite identity changed")
    elif value["child_path"] is not None:
        raise OwnerRefused("PostgreSQL cannot claim a SQLite path")
    if len(canonical(value)) > 32768:
        raise OwnerRefused("managed configuration exceeds bounds")
    return value


def read_configuration(anchor, reference, *, root, namespace, incarnation):
    config_reference(reference)
    with anchor.open_read("configuration-" + reference["digest"] + ".json") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise OwnerRefused("managed configuration exceeds bounds")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise OwnerRefused("duplicate managed configuration key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    if sha256(canonical(value)).hexdigest() != reference["digest"]:
        raise OwnerRefused("managed configuration digest changed")
    return validate_configuration(value, root=root, namespace=namespace, incarnation=incarnation)
