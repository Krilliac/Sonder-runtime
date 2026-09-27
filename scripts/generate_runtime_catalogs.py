"""Generate/check provider-neutral runtime catalog artifacts.

Usage:
    python scripts/generate_runtime_catalogs.py --runtime --output docs/architecture/generated/runtime-catalogs --check
    python scripts/generate_runtime_catalogs.py --source catalog-input.json --output generated/catalogs
    python scripts/generate_runtime_catalogs.py --source catalog-input.json --output generated/catalogs --check

``--runtime`` projects the live typed sources (the native tool registry, the
slash-command catalog and ``EventKind``) through the same bundle
``generate_documentation_catalogs.py`` commits under
``docs/architecture/generated/runtime-catalogs``; CI checks that copy through
``check_documentation_authority.py``.  ``--source`` takes a plain JSON input
so a mobile build can use the same contract without an SDK or network
dependency.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.catalog_artifacts import check_catalog_artifacts, write_catalog_artifacts
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.domain.common.events import EventKind
from sonder_runtime.domain.tools.descriptors import ExecutionClass, ToolEffect

try:
    from scripts.generate_documentation_catalogs import runtime_catalog_bundle
except ModuleNotFoundError:  # direct ``python scripts/generate_runtime_catalogs.py``
    from generate_documentation_catalogs import runtime_catalog_bundle  # type: ignore[no-redef]


def _load(path: Path) -> tuple[InMemoryToolRegistry, tuple[dict, ...], tuple[EventKind, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("catalog source must be an object")
    tools = []
    for raw in value.get("tools", []):
        if not isinstance(raw, dict):
            raise ValueError("each tool must be an object")
        effects = frozenset(ToolEffect[str(item)] for item in raw.get("effects", []))
        execution = ExecutionClass[str(raw.get("execution_class", "PURE"))]
        tools.append(ToolDescriptor(
            name=raw["name"], description=raw.get("description", ""),
            input_schema=raw.get("input_schema", {}), effects=effects,
            execution_class=execution,
        ))
    commands = tuple(value.get("commands", ()))
    events = tuple(EventKind(item) for item in value.get("events", ()))
    return InMemoryToolRegistry(tools), commands, events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="plain JSON catalog input")
    source.add_argument("--runtime", action="store_true",
                        help="project the live native tool registry, commands and events")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="fail when output is missing or stale")
    args = parser.parse_args()
    if args.runtime:
        bundle = runtime_catalog_bundle()
    else:
        registry, commands, events = _load(args.source)
        bundle = GeneratedCatalogs.generate(registry, commands=commands, event_kinds=events)
    if args.check:
        drift = check_catalog_artifacts(args.output, bundle)
        if drift:
            print("stale runtime catalog artifacts: " + ", ".join(drift), file=sys.stderr)
            return 1
        print(f"runtime catalogs current: {bundle.digest}")
        return 0
    paths = write_catalog_artifacts(args.output, bundle)
    print(f"wrote {len(paths)} runtime catalog artifacts: {bundle.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
