"""Generate deterministic documentation authority and runtime references."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "docs" / "architecture" / "generated"
# The six GeneratedCatalogs projections plus their SHA-256 manifest, rendered
# by application.tools.catalog_artifacts from the native typed tool registry.
RUNTIME_CATALOGS = GENERATED / "runtime-catalogs"
PACKAGE = ROOT / "sonder_runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FOCUSED_CONTRACTS = (
    ("ARCHITECTURE.md", "Product boundary and current runtime architecture."),
    ("SECURITY.md", "Current trust, authorization, and isolation contract."),
    ("SELFMOD.md", "Guarded self-modification lifecycle and recovery contract."),
    ("TRAINING.md", "Attended training, evaluation, deployment, and rollback contract."),
    ("CLIENT.md", "Thin-client and remote API client contract."),
    ("MOBILE_HOST_CONTROL.md", "Mobile-to-host launcher and control contract."),
)
HISTORICAL_DOCUMENTS = (
    "docs/architecture/SPEC-5-End-State-Architecture.md",
    "docs/architecture/SPEC-5-MIGRATION-RUNBOOK.md",
    "docs/architecture/PROGRAM-STATUS.md",
)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _jsonable(value.value)
    if isinstance(value, type) or callable(value):
        return getattr(value, "__name__", None)
    return value


def _sha(path: Path) -> str:
    # Source hashes are an authority signal, so they must not change merely
    # because Git checked the same file out with CRLF on Windows and LF on CI.
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source_hashes() -> dict[str, str]:
    paths = (
        PACKAGE / "application" / "tools" / "generated_catalogs.py",
        PACKAGE / "domain" / "common" / "events.py",
        PACKAGE / "platform" / "config.py",
        PACKAGE / "platform" / "memory_replication_config.py",
        ROOT / "command_catalog.py",
        ROOT / "server.py",
        PACKAGE / "domain" / "operational_capabilities.py",
        PACKAGE / "interfaces" / "sdk" / "discovery.py",
        PACKAGE / "bootstrap" / "native_mcp.py",
    )
    return {path.relative_to(ROOT).as_posix(): _sha(path) for path in paths if path.is_file()}


def _reference_shape(value: Any) -> Any:
    """Return a bounded structural reference without publishing live values."""
    if isinstance(value, dict):
        return {str(key): _reference_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_reference_shape(item) for item in value[:1]]
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def _runtime_reference() -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "sonder-runtime-document-reference-v1",
        "sources": _source_hashes(),
    }
    command_catalog = importlib.import_module(
        "sonder_runtime.adapters.command_catalog"
    ).command_catalog
    result["commands"] = sorted(
        ({
            "aliases": list(command.aliases),
            "category": command.category,
            "name": command.name,
            "native": bool(command.native),
            "params": _jsonable(command.params),
            "risk": command.risk,
            "summary": command.summary,
            "tool": command.tool,
        } for command in command_catalog.catalog()),
        key=lambda item: item["name"],
    )
    try:
        server = importlib.import_module("server")
        result["tools"] = sorted(({
            "description": tool.description or "",
            "name": tool.name,
            "parameters": _jsonable(tool.parameters or {}),
        } for tool in server.mcp._tool_manager.list_tools()), key=lambda item: item["name"])
        result["tool_source"] = "server.mcp._tool_manager.list_tools"
    except Exception as exc:
        raise RuntimeError("runtime tool source unavailable") from exc
    result["tool_source"] = "server.mcp._tool_manager.list_tools"
    # The opt-in ``mcp --native`` surface has its own, much smaller catalog.
    # Hand-written migration notes drifted from it (they kept saying a tool
    # was absent after it shipped), so it is projected here like the legacy
    # one and the notes point at this table instead of enumerating names.
    native_mcp = importlib.import_module("sonder_runtime.bootstrap.native_mcp")
    result["native_tools"] = sorted(({
        "description": descriptor.description or "",
        "name": descriptor.name,
        "parameters": _jsonable(dict(descriptor.input_schema or {})),
    } for descriptor in native_mcp.native_tool_registry().list_all()),
        key=lambda item: item["name"])
    result["native_tool_source"] = (
        "sonder_runtime.bootstrap.native_mcp.native_tool_registry"
    )

    events = importlib.import_module("sonder_runtime.domain.common.events")
    result["events"] = [{
        "name": kind.value,
        "optional": sorted(events.payload_schema(kind).optional),
        "required": sorted(events.payload_schema(kind).required),
    } for kind in events.EventKind]

    config = importlib.import_module("sonder_runtime.platform.config")
    memory_replication = importlib.import_module(
        "sonder_runtime.platform.memory_replication_config"
    )
    configuration = []
    specialized_sections = (
        ("memory_replication", memory_replication.MemoryReplicationConfig),
        (
            "memory_replication.peers[]",
            memory_replication.MemoryReplicationPeerConfig,
        ),
    )
    for section, cls in (
        ("root", config.SonderConfig),
        ("secrets", config.Secrets),
        *sorted((*config._SECTION_TYPES.items(), *specialized_sections)),
    ):
        for field in dataclasses.fields(cls):
            default = field.default if field.default is not dataclasses.MISSING else None
            if section == "secrets":
                default = "[redacted]"
            configuration.append({
                "default": _jsonable(default),
                "field": field.name,
                "section": section,
                "type": str(field.type),
            })
    result["configuration"] = configuration
    result["configuration_source"] = (
        "sonder_runtime.platform.config._SECTION_TYPES plus "
        "sonder_runtime.platform.memory_replication_config"
    )
    from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
    from sonder_runtime.application.tools.generated_catalogs import CatalogLimits, GeneratedCatalogs
    from sonder_runtime.interfaces.sdk.discovery import CapabilitySnapshot
    from sonder_runtime.domain.operational_capabilities import build_operational_capabilities

    descriptors = tuple(
        ToolDescriptor(item["name"], item["description"], item["parameters"])
        for item in result["tools"]
    )
    catalogs = GeneratedCatalogs.generate(
        InMemoryToolRegistry(descriptors),
        commands=result["commands"],
        event_kinds=events.EventKind,
        limits=CatalogLimits(
            max_tools=max(256, len(result["tools"])),
            max_events=max(128, len(result["events"])),
            max_commands=max(512, len(result["commands"])),
            max_bytes=2_000_000,
        ),
    )
    result["schemas"] = {
        "schema": "sonder-runtime-schema-reference-v1",
        "catalog_digest": catalogs.digest,
        "mcp": catalogs.mcp,
        "openai": catalogs.openai,
        "client": catalogs.client,
        "events": catalogs.client["events"],
    }
    capability_snapshot = CapabilitySnapshot.from_catalogs(
        catalogs, runtime_version="source-reference",
    )
    result["capabilities"] = {
        "schema": "sonder-runtime-capability-reference-v1",
        "catalog_digest": catalogs.digest,
        "sdk": capability_snapshot.as_dict(),
        "operational": _reference_shape(
            build_operational_capabilities(config=None)
        ),
    }
    result["counts"] = {
        name: len(result[name])
        for name in ("commands", "tools", "native_tools", "events", "configuration")
    } | {
        "schemas": 4,
        "capabilities": len(result["capabilities"]["sdk"]["tools"]),
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    result["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def _architecture_map() -> dict[str, Any]:
    layers = []
    for directory in sorted(path for path in PACKAGE.iterdir() if path.is_dir()):
        files = sorted(path.relative_to(ROOT).as_posix() for path in directory.rglob("*.py"))
        layers.append({"name": directory.name, "python_files": files, "file_count": len(files)})
    ownership_module = importlib.import_module(
        "sonder_runtime.application.architecture.ownership_catalog"
    )
    ownership = ownership_module.default_layer_ownership_catalog(
        row["name"] for row in layers if row["name"] != "__pycache__"
    )
    return {
        "schema": "sonder-architecture-map-v1",
        "authority": "docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md",
        "composition_roots": ["sonder_runtime/__main__.py", "sonder_runtime/bootstrap/", "sonder_runtime/interfaces/"],
        "layers": layers,
        "ownership": {
            "schema": "sonder-ownership-catalog-v1",
            "source": "sonder_runtime.application.architecture.ownership_catalog.default_layer_ownership_catalog",
            "records": ownership.snapshot(),
        },
        "focused_contracts": [{"path": path, "summary": summary} for path, summary in FOCUSED_CONTRACTS],
        "historical_documents": list(HISTORICAL_DOCUMENTS),
    }


def _inventory() -> dict[str, Any]:
    return {
        "schema": "sonder-focused-document-inventory-v1",
        "authority_index": "docs/architecture/DOCUMENT-AUTHORITY-INDEX.md",
        "documents": [{
            "classification": "current", "path": path, "summary": summary,
            "exists": (ROOT / path).is_file(),
        } for path, summary in FOCUSED_CONTRACTS],
        "historical": [{
            "classification": "superseded", "path": path, "exists": (ROOT / path).is_file(),
        } for path in HISTORICAL_DOCUMENTS],
    }


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _markdown_reference(reference: dict[str, Any]) -> str:
    lines = [
        "# Generated runtime references", "",
        "Generated by `scripts/generate_documentation_catalogs.py`; do not edit manually.",
        "", f"Digest: `{reference['digest']}`", "",
        "| Reference | Count | Source |", "|---|---:|---|",
        f"| Tools | {reference['counts']['tools']} | `{reference['tool_source'] if isinstance(reference['tool_source'], str) else 'unavailable'}` |",
        f"| Native MCP tools | {reference['counts']['native_tools']} | `{reference['native_tool_source']}` |",
        f"| Commands | {reference['counts']['commands']} | `command_catalog.catalog()` |",
        f"| Events | {reference['counts']['events']} | `EventKind` and `payload_schema()` |",
        f"| Configuration fields | {reference['counts']['configuration']} | `{reference['configuration_source']}` |",
        f"| Schemas | {reference['counts']['schemas']} projections | `GeneratedCatalogs` |",
        f"| Capabilities | {reference['counts']['capabilities']} tools plus operational shape | `CapabilitySnapshot` and `build_operational_capabilities` |",
        "", "## Tools", "", "| Name | Description |", "|---|---|",
    ]
    for item in reference["tools"]:
        description = str(item["description"]).splitlines()[0].replace("|", "\\|")
        lines.append(f"| `{item['name']}` | {description} |")
    lines += ["", "## Native MCP tools", "",
              "Served by `python -m sonder_runtime mcp --native`; the table above is the legacy default surface.",
              "", "| Name | Description |", "|---|---|"]
    for item in reference["native_tools"]:
        description = (str(item["description"]).splitlines() or [""])[0].replace("|", "\\|")
        lines.append(f"| `{item['name']}` | {description} |")
    lines += ["", "## Commands", "", "| Name | Category | Risk | Tool |", "|---|---|---|---|"]
    for item in reference["commands"]:
        lines.append(f"| `{item['name']}` | {item['category']} | {item['risk']} | `{item['tool']}` |")
    lines += ["", "## Events", "", "| Name | Required payload | Optional payload |", "|---|---|---|"]
    for item in reference["events"]:
        lines.append(f"| `{item['name']}` | {', '.join(item['required']) or '—'} | {', '.join(item['optional']) or '—'} |")
    lines += ["", "## Configuration", "", "| Section | Field | Type | Default |", "|---|---|---|---|"]
    for item in reference["configuration"]:
        lines.append(f"| `{item['section']}` | `{item['field']}` | `{item['type']}` | `{item['default']}` |")
    lines += ["", "## Schemas", "", "Generated schema projections share the catalog digest:", "",
              f"- Catalog digest: `{reference['schemas']['catalog_digest']}`",
              "- MCP tool schema, OpenAI function schema, client schema, and event schema are generated from the typed catalog.",
              "", "## Capabilities", "",
              f"- Catalog digest: `{reference['capabilities']['catalog_digest']}`",
              "- SDK capabilities are typed tool descriptors and are descriptive; authorization remains runtime-evaluated.",
              "- Operational capability fields are represented structurally from the typed operational capability projection."]
    return "\n".join(lines) + "\n"


def _architecture_markdown(value: dict[str, Any]) -> str:
    lines = ["# Generated architecture map", "", "Generated; do not edit manually.", "", f"Authority: `{value['authority']}`", "", "## Package layers", "", "| Layer | Python files |", "|---|---:|"]
    lines += [f"| `{row['name']}` | {row['file_count']} |" for row in value["layers"]]
    lines += ["", "## Composition roots", ""] + [f"- `{item}`" for item in value["composition_roots"]]
    lines += ["", "## Layer ownership", "", "| Package | State | Public port | Provider | Schema | Lifecycle |", "|---|---|---|---|---|---|"]
    for row in value["ownership"]["records"]:
        lines.append(
            "| `{package}` | `{state}` | `{public_port}` | `{provider}` | `{schema}` | `{lifecycle}` |".format(
                package=row["package"]["name"], state=row["state"]["name"],
                public_port=row["public_port"]["name"], provider=row["provider"]["name"],
                schema=row["schema"]["name"], lifecycle=row["lifecycle"]["name"],
            )
        )
    return "\n".join(lines) + "\n"


def _inventory_markdown(value: dict[str, Any]) -> str:
    lines = ["# Focused contract-document inventory", "", "Generated; current behavior belongs in these documents.", "", "| Classification | Path | Exists |", "|---|---|---|"]
    for row in value["documents"] + value["historical"]:
        lines.append(f"| {row['classification']} | `{row['path']}` | {'yes' if row['exists'] else 'no'} |")
    return "\n".join(lines) + "\n"


def runtime_catalog_bundle() -> Any:
    """The catalog bundle of the live typed sources.

    Tools come from the native typed registry (``mcp --native``), the only
    registry whose descriptors carry effects and an execution class, so the
    permissions projection is real. Commands come from the slash-command
    catalog and events from ``EventKind``.
    """
    from sonder_runtime.application.tools.generated_catalogs import CatalogLimits, GeneratedCatalogs

    native_mcp = importlib.import_module("sonder_runtime.bootstrap.native_mcp")
    command_catalog = importlib.import_module(
        "sonder_runtime.adapters.command_catalog"
    ).command_catalog
    events = importlib.import_module("sonder_runtime.domain.common.events")
    registry = native_mcp.native_tool_registry()
    commands = tuple(command_catalog.catalog())
    return GeneratedCatalogs.generate(
        registry,
        commands=commands,
        event_kinds=events.EventKind,
        limits=CatalogLimits(
            max_tools=max(256, len(registry.list_all())),
            max_events=max(128, len(events.EventKind)),
            max_commands=max(512, len(commands)),
            max_bytes=2_000_000,
        ),
    )


def _runtime_catalog_files() -> dict[Path, str]:
    from sonder_runtime.application.tools.catalog_artifacts import (
        render_catalog_artifacts, render_manifest,
    )

    bundle = runtime_catalog_bundle()
    artifacts = render_catalog_artifacts(bundle)
    files = {RUNTIME_CATALOGS / name: content for name, content in artifacts.items()}
    files[RUNTIME_CATALOGS / "manifest.json"] = render_manifest(bundle, artifacts)
    return files


def expected() -> dict[Path, str]:
    reference, architecture, inventory = _runtime_reference(), _architecture_map(), _inventory()
    return {
        **_runtime_catalog_files(),
        GENERATED / "runtime-reference.json": _dump(reference),
        GENERATED / "runtime-reference.md": _markdown_reference(reference),
        GENERATED / "architecture-map.json": _dump(architecture),
        GENERATED / "architecture-map.md": _architecture_markdown(architecture),
        GENERATED / "focused-contract-inventory.json": _dump(inventory),
        GENERATED / "focused-contract-inventory.md": _inventory_markdown(inventory),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    problems = []
    for path, content in expected().items():
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(path.relative_to(ROOT).as_posix())
    for problem in problems:
        print(f"stale or missing generated documentation: {problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
