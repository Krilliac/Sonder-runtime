"""Explicit-authority CLI for extension experiments and registry diagnostics."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from ...application.extensions.facade import ExtensionApplicationFacade, ExtensionAuthority
from ...application.extensions.experiments import ExperimentError


def _snapshot(value):
    stats = value.stats
    return {
        "experiment_id": value.experiment_id,
        "state": value.state,
        "description": value.description,
        "starts": value.starts,
        "stops": value.stops,
        "stats": None if stats is None else {
            "launches": stats.launches, "restarts": stats.restarts,
            "crashes": stats.crashes, "calls": stats.calls,
        },
    }


class ExtensionCommand:
    """Parse one extension command and delegate all behavior to the facade."""

    def __init__(self, facade: ExtensionApplicationFacade) -> None:
        self._facade = facade

    def run(
        self,
        argv: Sequence[str] | None,
        *,
        authority: ExtensionAuthority,
        out: TextIO = sys.stdout,
    ) -> int:
        parser = argparse.ArgumentParser(prog="sonder extension")
        parser.add_argument("--actor", help="documentation only; authority is injected by the caller")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("health")
        inspect = sub.add_parser("inspect")
        inspect.add_argument("experiment_id")
        define = sub.add_parser("define")
        define.add_argument("experiment_id")
        define.add_argument("--description", default="")
        define.add_argument("--argv", nargs="+", required=True)
        for name in ("start", "stop", "delete"):
            command = sub.add_parser(name)
            command.add_argument("experiment_id")
        args = parser.parse_args(list(argv) if argv is not None else None)
        try:
            if args.command == "health":
                health = self._facade.registry_health(authority)
                result = {
                    "object": "extension_registry_health",
                    "records": [
                        {
                            "extension_id": record.extension_id,
                            "scope": record.scope.value,
                            "project_id": record.project_id,
                            "version": record.version,
                            "health_state": record.health_state.value,
                            "enabled": record.enabled,
                            "health_reasons": list(record.health_reasons),
                            "crash_count": record.crash_count,
                        }
                        for record in health.snapshot.records
                    ],
                    "digest": health.snapshot.digest,
                    "diagnostics": [
                        {
                            "extension_id": item.extension_id,
                            "scope": item.scope.value,
                            "project_id": item.project_id,
                            "state": item.state.value,
                            "codes": list(item.codes),
                            "recommended_action": item.recommended_action,
                        }
                        for item in health.diagnostics
                    ],
                    "persistence": health.persistence,
                    "promotion": health.promotion,
                }
            elif args.command == "define":
                result = _snapshot(self._facade.define(
                    args.experiment_id, tuple(args.argv),
                    description=args.description, authority=authority,
                ))
            elif args.command == "inspect":
                result = _snapshot(self._facade.inspect(args.experiment_id, authority))
            else:
                result = _snapshot(getattr(self._facade, args.command)(args.experiment_id, authority))
            out.write(json.dumps(result, sort_keys=True) + "\n")
            return 0
        except (ExperimentError, PermissionError, ValueError, TypeError) as error:
            out.write(json.dumps({"error": str(error), "type": type(error).__name__}) + "\n")
            return 1


__all__ = ["ExtensionCommand"]
