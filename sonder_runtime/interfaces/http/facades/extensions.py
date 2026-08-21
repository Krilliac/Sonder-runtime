"""Typed HTTP presentation for the extension application facade."""
from __future__ import annotations

from dataclasses import dataclass

from ....application.extensions.experiments import (
    ExperimentError,
    ExperimentNotFound,
    ExperimentInvalidDefinition,
    ExperimentInvalidTransition,
    ExperimentStartupDenied,
)
from ....application.extensions.facade import (
    ExtensionApplicationFacade,
    ExtensionAuthority,
    ExtensionAuthorityDenied,
    build_extension_manifest,
)
from ....application.extensions.registry import ExtensionRegistryError


def _manifest_payload(value: object):
    if not isinstance(value, dict):
        raise TypeError("manifest must be an object")
    required = {"extension_id", "version", "protocol"}
    if set(value) - required - {"dependencies", "permissions"} or not required.issubset(value):
        raise ValueError("manifest requires extension_id, version, and protocol")
    extension_id, version, protocol = value["extension_id"], value["version"], value["protocol"]
    if not all(isinstance(item, str) for item in (extension_id, version, protocol)) or extension_id.count(".") != 1:
        raise TypeError("manifest identity, version, and protocol must be bounded text")
    publisher, name = extension_id.split(".")
    dependencies = value.get("dependencies", [])
    permissions = value.get("permissions", [])
    if not isinstance(dependencies, list) or not isinstance(permissions, list):
        raise TypeError("manifest dependencies and permissions must be arrays")
    return build_extension_manifest(
        extension_id, version, protocol,
        dependencies=dependencies, permissions=permissions,
    )


@dataclass(frozen=True, slots=True)
class ExtensionHttpResult:
    body: dict[str, object]
    status_code: int = 200


def _snapshot(snapshot) -> dict[str, object]:
    stats = snapshot.stats
    return {
        "experiment_id": snapshot.experiment_id,
        "state": snapshot.state,
        "description": snapshot.description,
        "starts": snapshot.starts,
        "stops": snapshot.stops,
        "stats": None if stats is None else {
            "launches": stats.launches,
            "calls": stats.calls,
            "restarts": stats.restarts,
            "crashes": stats.crashes,
        },
    }


def _record(record) -> dict[str, object]:
    return {
        "extension_id": record.extension_id,
        "scope": record.scope.value,
        "project_id": record.project_id,
        "version": record.version,
        "manifest_digest": record.manifest_digest,
        "enabled": record.enabled,
        "health_state": record.health_state.value,
        "health_reasons": list(record.health_reasons),
        "quarantine": None if record.quarantine is None else {
            "quarantined": record.quarantine.quarantined,
            "reasons": list(record.quarantine.reasons),
            "cleanup_action": record.quarantine.cleanup_action,
            "retain_state": record.quarantine.retain_state,
        },
        "crash_count": record.crash_count,
    }


def _health(health) -> dict[str, object]:
    return {
        "object": "extension_registry_health",
        "records": [_record(record) for record in health.snapshot.records],
        "digest": health.snapshot.digest,
        "diagnostics": [
            {
                "extension_id": diagnostic.extension_id,
                "scope": diagnostic.scope.value,
                "project_id": diagnostic.project_id,
                "state": diagnostic.state.value,
                "codes": list(diagnostic.codes),
                "recommended_action": diagnostic.recommended_action,
            }
            for diagnostic in health.diagnostics
        ],
        "persistence": health.persistence,
        "promotion": health.promotion,
    }


def _experiment_id(path: str, suffix: str) -> str | None:
    prefix = "/v1/extensions/experiments/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    value = path[len(prefix):-len(suffix)] if suffix else path[len(prefix):]
    if not value or "/" in value:
        return None
    # The HTTP adapter passes its normalized path.  Do not import URL/network
    # helpers into the interface layer; encoded values fail the bounded
    # application identifier validation instead of being silently rewritten.
    return value


def _error(error: Exception) -> ExtensionHttpResult:
    if isinstance(error, ExtensionAuthorityDenied):
        return ExtensionHttpResult({"error": {"message": str(error), "type": "forbidden"}}, 403)
    if isinstance(error, ExperimentNotFound):
        return ExtensionHttpResult({"error": {"message": str(error), "type": "not_found"}}, 404)
    if isinstance(error, ExperimentStartupDenied):
        return ExtensionHttpResult({"error": {"message": str(error), "type": "startup_denied"}}, 403)
    if isinstance(error, ExperimentInvalidTransition):
        return ExtensionHttpResult({"error": {"message": str(error), "type": "conflict"}}, 409)
    if isinstance(error, (ExperimentInvalidDefinition, ExperimentError, ExtensionRegistryError, ValueError, TypeError)):
        return ExtensionHttpResult({"error": {"message": str(error), "type": "invalid_request"}}, 400)
    return ExtensionHttpResult({"error": {"message": "extension operation failed", "type": "internal_error"}}, 500)


def dispatch_extension_route(
    facade: ExtensionApplicationFacade,
    method: str,
    path: str,
    payload: dict[str, object] | None,
    authority: ExtensionAuthority,
) -> ExtensionHttpResult | None:
    """Dispatch only the explicit extension routes; return ``None`` otherwise."""
    payload = {} if payload is None else payload
    if path == "/v1/extensions" and method == "GET":
        try:
            return ExtensionHttpResult(_health(facade.registry_health(authority)))
        except Exception as error:
            return _error(error)

    # Registry state changes are explicit, authenticated, and never start a
    # child.  Scope is carried in the body so project targeting is unambiguous.
    for action, operation in (("disable", "disable"), ("enable", "enable"), ("repair", "repair")):
        prefix = f"/v1/extensions/registry/"
        if method == "POST" and path.startswith(prefix) and path.endswith("/" + action):
            extension_id = path[len(prefix):-len(action)-1]
            if not extension_id or "/" in extension_id:
                return None
            try:
                scope = payload.get("scope", "global")
                project_id = payload.get("project_id")
                if not isinstance(scope, str) or (project_id is not None and not isinstance(project_id, str)):
                    raise TypeError("scope must be text and project_id must be text or null")
                record = getattr(facade, operation)(extension_id, scope=scope, project_id=project_id, authority=authority)
                return ExtensionHttpResult({"object": f"extension_{action}", "extension": _record(record)})
            except Exception as error:
                return _error(error)

    if path == "/v1/extensions/registry/update" and method == "POST":
        try:
            scope = payload.get("scope", "global")
            project_id = payload.get("project_id")
            if not isinstance(scope, str) or (project_id is not None and not isinstance(project_id, str)):
                raise TypeError("scope must be text and project_id must be text or null")
            record = facade.update(_manifest_payload(payload.get("manifest")), scope=scope,
                                   project_id=project_id, authority=authority)
            return ExtensionHttpResult({"object": "extension_update", "extension": _record(record)})
        except Exception as error:
            return _error(error)

    actions = {
        "/inspect": ("inspect", "GET"),
        "/start": ("start", "POST"),
        "/stop": ("stop", "POST"),
        "/delete": ("delete", "POST"),
    }
    for suffix, (operation, expected_method) in actions.items():
        experiment_id = _experiment_id(path, suffix)
        if experiment_id is None or method != expected_method:
            continue
        try:
            snapshot = getattr(facade, operation)(experiment_id, authority)
            return ExtensionHttpResult({"object": f"experiment_{operation}", "experiment": _snapshot(snapshot)})
        except Exception as error:
            return _error(error)

    if path == "/v1/extensions/experiments/define" and method == "POST":
        try:
            required = {"experiment_id", "argv"}
            optional = {"description", "environment"}
            if set(payload) - required - optional or not required.issubset(payload):
                raise ValueError("define requires exactly experiment_id and argv plus optional fields")
            experiment_id = payload["experiment_id"]
            argv = payload["argv"]
            if not isinstance(experiment_id, str) or not isinstance(argv, list):
                raise TypeError("experiment_id must be text and argv must be an array")
            description = payload.get("description", "")
            environment = payload.get("environment")
            if not isinstance(description, str) or (environment is not None and not isinstance(environment, dict)):
                raise TypeError("description must be text and environment must be an object")
            snapshot = facade.define(
                experiment_id, tuple(argv), authority=authority,
                description=description, environment=environment,
            )
            return ExtensionHttpResult({"object": "experiment_define", "experiment": _snapshot(snapshot)}, 201)
        except Exception as error:
            return _error(error)
    return None


__all__ = ["ExtensionHttpResult", "dispatch_extension_route"]
