#!/usr/bin/env python3
"""Architecture enforcement for the sonder_runtime package (SPEC-3 §12).

Checks, over every module inside sonder_runtime/:

- Layer dependency direction: domain -> (domain, stdlib); application ->
  (domain, application, stdlib); adapters -> (domain, application,
  platform, root legacy modules, stdlib); platform -> (root platform
  modules, stdlib); bootstrap -> anything in the package + stdlib;
  the entry module may reach root modules for delegation.
- No package-internal import cycles.
- No ``sqlite3.connect`` outside adapters.
- No ``subprocess`` use outside adapters.
- No ``urllib``/``socket``/``http.client`` outside adapters.
- No ``os.environ`` reads in domain or application modules.
- Domain modules import nothing outside domain + stdlib at all.

Exit code 0 with no output means the architecture holds; violations are
listed one per line and exit 1.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"

STDLIB = set(sys.stdlib_module_names)

# Root modules each layer may reach while the strangler migration runs.
ROOT_PLATFORM_MODULES = set()
# These platform-owned modules contain the unavoidable standard-library
# edges for parsing configured Ollama URLs and reading the local build stamp.
# They are not root-module compatibility allowances.
PLATFORM_NETWORK_MODULES = frozenset({
    "sonder_runtime/platform/config.py",
})
PLATFORM_SUBPROCESS_MODULES = frozenset({
    "sonder_runtime/platform/system_profile.py",
    "sonder_runtime/platform/version.py",
})
NPU_ACCELERATOR_EXTERNALS = frozenset({"numpy", "onnxruntime", "tokenizers"})
FILESYSTEM_OPTIONAL_EXTERNALS = frozenset({"yaml"})
PLATFORM_OPTIONAL_EXTERNALS = frozenset({"prometheus_client", "psutil"})
DOMAIN_PURE_URL_MODULES = frozenset({
    "sonder_runtime/domain/ollama_policy.py",
})
UPDATES_EXTERNALS = frozenset({"tuf", "web_tools"})
UPDATE_ENGINE_PATH = "sonder_runtime/adapters/updates/engine.py"
VERSION_PLATFORM_PATH = "sonder_runtime/platform/version.py"
HTTP_SERVE_PATH = "sonder_runtime/interfaces/http/serve.py"
HTTP_SERVE_ROOT_MODULES = frozenset({
    "server",
})
REPL_PATH = "sonder_runtime/interfaces/repl/repl.py"
REPL_ROOT_MODULES = frozenset({
    "server",
})
BASELINE_ROOT_LEGACY_MODULES = frozenset({
    "server",
})
ROOT_LEGACY_MODULES = {"server"}
# This is a ratchet, not a target.  Removing a legacy root dependency is
# always allowed; adding one requires an explicit architecture-policy change
# and must never happen as an accidental convenience import.
ROOT_LEGACY_MODULE_LIMIT = 1
WEB_SEARCH_CANONICAL_MODULE = "sonder_runtime.adapters.web_search"
WEB_SEARCH_COMPATIBILITY_ROOT = Path("web_tools.py")
WEB_FETCH_CANONICAL_MODULE = "sonder_runtime.adapters.web_fetch"
WEB_FETCH_COMPATIBILITY_ROOT = Path("web_tools.py")
WEATHER_CANONICAL_MODULE = "sonder_runtime.adapters.weather"
LOCATION_CANONICAL_MODULE = "sonder_runtime.adapters.location"
WEATHER_LOCATION_COMPATIBILITY_ROOT = Path("web_tools.py")

LAYERS = ("domain", "application", "adapters", "interfaces", "platform", "bootstrap")

ALLOWED_PACKAGE_EDGES = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "interfaces": {"application", "interfaces"},
    "adapters": {"domain", "application", "adapters", "platform"},
    "platform": {"platform"},
    "bootstrap": {"domain", "application", "adapters", "interfaces", "platform", "bootstrap"},
    "entry": {"domain", "application", "adapters", "interfaces", "platform", "bootstrap"},
}
ALLOWED_ROOT_IMPORTS = {
    "domain": set(),
    "application": set(),
    "interfaces": set(),
    "adapters": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES,
    "platform": ROOT_PLATFORM_MODULES,
    "bootstrap": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES,
    "entry": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES | {
        "sonder_runtime", "sonder_doctor",
    },
}

IO_MODULES = {"urllib", "socket", "http", "ftplib", "smtplib"}

COMPATIBILITY_ROOT_MODULES = {
    "archive_create": Path("archive_create.py"),
    "artifact_grounding": Path("artifact_grounding.py"),
    "code_runner": Path("code_runner.py"),
    "command_catalog": Path("command_catalog.py"),
    "fanout_store": Path("fanout_store.py"),
    "learning_health": Path("learning_health.py"),
    "memory_store": Path("memory_store.py"),
    "autopilot_store": Path("autopilot_store.py"),
    "fleet_store": Path("fleet_store.py"),
    "queued_actions": Path("queued_actions.py"),
}

# Root modules removed by completed strangler slices must stay removed.  This
# list is deliberately explicit: each entry is a reviewed migration boundary,
# not a broad filename convention that could hide legitimate new entrypoints.
RETIRED_ROOT_MODULES = frozenset({
    Path("context_overflow.py"),
    Path("mmr_rerank.py"),
    Path("reward.py"),
    Path("execution_status.py"),
    Path("process_liveness.py"),
    Path("eval_history.py"),
    Path("recall.py"),
    Path("sonder_backup.py"),
    Path("sonder_preflight.py"),
    Path("workflow_store.py"),
    Path("sonder_storage.py"),
    Path("model_transport.py"),
    Path("runtime_policy.py"),
    Path("ollama_endpoint.py"),
    Path("embed_cache.py"),
    Path("embeddings.py"),
    Path("npu_contract.py"),
    Path("npu_manifest.py"),
    Path("npu_providers.py"),
    Path("npu_broker.py"),
    Path("npu_worker.py"),
    Path("npu_service.py"),
    Path("activity_tracker.py"),
    Path("sonder_operations_store.py"),
    Path("file_ops.py"),
    Path("workbench.py"),
    Path("sonder_secrets.py"),
    Path("sonder_updates.py"),
    Path("sonder_update_engine.py"),
    Path("sonder_lifecycle.py"),
    Path("sonder_serve.py"),
    Path("sonder_repl.py"),
    Path("sonder_migrations.py"),
    Path("sonder_metrics.py"),
    Path("archive_tools.py"),
    Path("content_digest.py"),
    Path("data_query.py"),
    Path("dependency_inventory.py"),
    Path("log_inspect.py"),
    Path("project_detect.py"),
    Path("workspace_compare.py"),
    Path("sonder_runtime/adapters/strangler_services.py"),
    Path("sonder_runtime/adapters/legacy_model_gateway.py"),
    Path("sonder_runtime/adapters/openai_compat/gateway.py"),
    Path("sonder_runtime/adapters/ollama/endpoint.py"),
    Path("text_patch.py"),
    Path("artifact_fetch.py"),
    Path("artifact_risk.py"),
    Path("pdf_risk.py"),
    Path("process_risk.py"),
    Path("live_reload.py"),
})

# Applied migrations are immutable historical artifacts. They may retain an
# import that production code has since moved behind a compatibility adapter;
# rewriting one would invalidate its recorded checksum on deployed systems.
COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS = {
    "learning_health": frozenset({Path("server.py")}),
    # These legacy root consumers are intentionally unchanged in the
    # command-catalog packaging slice.  They are the reverse edges that
    # motivated the catalog's lazy command_registry/permission_modes imports;
    # a later caller migration can remove these exceptions without changing
    # the canonical adapter again.
    "command_catalog": frozenset({
        Path("command_registry.py"),
        Path("command_router.py"),
        Path("permission_modes.py"),
        Path("reloadable_mcp.py"),
        Path("slash_menu.py"),
    }),
    "memory_store": frozenset({Path("migrations/memory/0001_baseline.py")}),
    "autopilot_store": frozenset({Path("migrations/autopilot/0001_baseline.py")}),
    "fleet_store": frozenset({Path("migrations/fleet/0001_baseline.py")}),
    "queued_actions": frozenset({Path("migrations/queued_actions/0001_baseline.py")}),
}


def tracked_production_python_files(
    repo_root: Path = REPO_ROOT,
) -> tuple[Path, ...]:
    """Return the deterministic production-source inventory.

    Packaging is built from ``git ls-files`` rather than a filesystem walk so
    ignored build outputs can never become accidental inputs.  Architecture
    checks use the same boundary.  Missing tracked sources and Git failures
    fail closed instead of silently weakening the gate.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root), "ls-files", "-z", "--cached",
                "--", "*.py",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "git ls-files is required for the architecture source inventory"
        ) from exc

    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if not relative.parts or relative.parts[0] == "tests":
            continue
        path = repo_root / relative
        if path.is_symlink() or not path.is_file():
            if relative in RETIRED_ROOT_MODULES:
                continue
            raise RuntimeError(
                f"tracked production source is missing or not a regular file: {relative}"
            )
        paths.append(path)
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def compatibility_import_offenders(
    module: str,
    compatibility_path: Path,
    repo_root: Path = REPO_ROOT,
    *,
    allowed_paths: frozenset[Path] = frozenset(),
) -> tuple[Path, ...]:
    """Find production callers that bypass a compatibility module's adapter."""
    offenders: list[Path] = []
    for path in tracked_production_python_files(repo_root):
        relative = path.relative_to(repo_root)
        if relative == compatibility_path or relative in allowed_paths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == module for alias in node.names
            ):
                offenders.append(relative)
                break
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == module
            ):
                offenders.append(relative)
                break
    return tuple(offenders)


def layer_of(path: Path) -> str:
    rel = path.relative_to(PACKAGE_ROOT)
    if len(rel.parts) == 1:
        return "entry"  # __init__.py / __main__.py
    top = rel.parts[0]
    return top if top in LAYERS else "entry"


def module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_relative(module: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base = module.split(".")
    # A module's package is itself minus the module component.
    package_parts = base[:-1] if not module.endswith("__init__") else base
    anchor = package_parts[: len(package_parts) - (node.level - 1)]
    return ".".join(anchor + ([node.module] if node.module else []))


def check() -> list[str]:
    violations: list[str] = []
    tracked = {
        path.relative_to(REPO_ROOT)
        for path in tracked_production_python_files()
    }
    for retired in sorted(RETIRED_ROOT_MODULES, key=Path.as_posix):
        if retired in tracked:
            violations.append(
                f"{retired}: retired root module was reintroduced"
            )
    unexpected_legacy = ROOT_LEGACY_MODULES - BASELINE_ROOT_LEGACY_MODULES
    if unexpected_legacy:
        violations.append(
            "ROOT_LEGACY_MODULES added non-baseline module(s): %s"
            % ", ".join(sorted(unexpected_legacy))
        )
    if len(ROOT_LEGACY_MODULES) > ROOT_LEGACY_MODULE_LIMIT:
        violations.append(
            "ROOT_LEGACY_MODULES grew from its ratchet limit of %d to %d"
            % (ROOT_LEGACY_MODULE_LIMIT, len(ROOT_LEGACY_MODULES))
        )
    root_web_path = REPO_ROOT / WEB_SEARCH_COMPATIBILITY_ROOT
    package_web_path = REPO_ROOT / "sonder_runtime" / "adapters" / "web_search.py"
    if root_web_path.exists() and package_web_path.exists():
        root_web_tree = ast.parse(
            root_web_path.read_text(encoding="utf-8"),
            filename=str(WEB_SEARCH_COMPATIBILITY_ROOT),
        )
        package_web_tree = ast.parse(
            package_web_path.read_text(encoding="utf-8"),
            filename=WEB_SEARCH_CANONICAL_MODULE,
        )
        root_web_functions = {
            node.name
            for node in root_web_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        package_web_functions = {
            node.name
            for node in package_web_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "web_search" in root_web_functions:
            violations.append(
                "web_tools.py: web_search implementation must remain in "
                f"{WEB_SEARCH_CANONICAL_MODULE}"
            )
        if "search_raw" not in package_web_functions:
            violations.append(
                f"{WEB_SEARCH_CANONICAL_MODULE}: missing canonical search_raw entrypoint"
            )
    fetch_root_path = REPO_ROOT / WEB_FETCH_COMPATIBILITY_ROOT
    fetch_package_path = REPO_ROOT / "sonder_runtime" / "adapters" / "web_fetch.py"
    if fetch_root_path.exists() and fetch_package_path.exists():
        fetch_root_tree = ast.parse(
            fetch_root_path.read_text(encoding="utf-8"),
            filename=str(WEB_FETCH_COMPATIBILITY_ROOT),
        )
        fetch_package_tree = ast.parse(
            fetch_package_path.read_text(encoding="utf-8"),
            filename=WEB_FETCH_CANONICAL_MODULE,
        )
        fetch_root_functions = {
            node.name for node in fetch_root_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        fetch_package_functions = {
            node.name for node in fetch_package_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "web_fetch" not in fetch_root_functions:
            violations.append("web_tools.py: missing compatibility web_fetch delegate")
        if "_decode_web_document" in fetch_root_functions:
            violations.append(
                "web_tools.py: web_fetch decoding must remain in "
                f"{WEB_FETCH_CANONICAL_MODULE}"
            )
        if "fetch_raw" not in fetch_package_functions:
            violations.append(
                f"{WEB_FETCH_CANONICAL_MODULE}: missing canonical fetch_raw entrypoint"
            )
    weather_location_root = REPO_ROOT / WEATHER_LOCATION_COMPATIBILITY_ROOT
    weather_path = REPO_ROOT / "sonder_runtime" / "adapters" / "weather.py"
    location_path = REPO_ROOT / "sonder_runtime" / "adapters" / "location.py"
    if weather_location_root.exists() and weather_path.exists() and location_path.exists():
        root_tree = ast.parse(weather_location_root.read_text(encoding="utf-8"), filename=str(WEATHER_LOCATION_COMPATIBILITY_ROOT))
        root_functions = {node.name for node in root_tree.body if isinstance(node, ast.FunctionDef)}
        weather_tree = ast.parse(weather_path.read_text(encoding="utf-8"), filename=WEATHER_CANONICAL_MODULE)
        location_tree = ast.parse(location_path.read_text(encoding="utf-8"), filename=LOCATION_CANONICAL_MODULE)
        weather_functions = {node.name for node in weather_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        location_functions = {node.name for node in location_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        public_weather = {"weather_lookup", "format_weather"}
        public_location = {"normalize_location_hint", "approximate_location_lookup", "location_label", "format_approximate_location"}
        if root_functions & (public_weather | public_location):
            violations.append("web_tools.py: weather/location implementation must remain packaged")
        if not public_weather <= weather_functions:
            violations.append(f"{WEATHER_CANONICAL_MODULE}: missing canonical weather entrypoints")
        if not public_location <= location_functions:
            violations.append(f"{LOCATION_CANONICAL_MODULE}: missing canonical location entrypoints")
    imports: dict[str, set[str]] = {}
    files = sorted(PACKAGE_ROOT.rglob("*.py"))

    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module = module_name(path)
        layer = layer_of(path)
        rel = path.relative_to(REPO_ROOT)
        imported: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                imported.add(resolve_relative(module, node))
            elif isinstance(node, ast.Attribute):
                # os.environ reads in domain/application
                if (
                    layer in ("domain", "application")
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr in ("environ", "getenv", "environb")
                ):
                    violations.append(
                        f"{rel}: {layer} layer reads the environment "
                        f"(os.{node.attr})"
                    )
            elif isinstance(node, ast.Call):
                target = node.func
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "sqlite3"
                    and target.attr == "connect"
                    and layer != "adapters"
                ):
                    violations.append(
                        f"{rel}: sqlite3.connect outside adapters"
                    )

        imports[module] = {i for i in imported if i.startswith("sonder_runtime")}

        for name in sorted(imported):
            if not name:
                continue
            top = name.split(".")[0]
            if (
                layer == "adapters"
                and rel.as_posix().startswith("sonder_runtime/adapters/accelerators/npu/")
                and top in NPU_ACCELERATOR_EXTERNALS
            ):
                continue
            if (
                layer == "adapters"
                and rel.as_posix() == "sonder_runtime/adapters/filesystem/file_ops.py"
                and top in FILESYSTEM_OPTIONAL_EXTERNALS
            ):
                continue
            if (
                layer == "adapters"
                and rel.as_posix() == "sonder_runtime/adapters/updates/service.py"
                and top in UPDATES_EXTERNALS
            ):
                continue
            if (
                layer == "platform"
                and rel.as_posix() == "sonder_runtime/platform/metrics.py"
                and top in PLATFORM_OPTIONAL_EXTERNALS
            ):
                continue
            if layer == "platform" and top in PLATFORM_OPTIONAL_EXTERNALS:
                continue
            if name.startswith("sonder_runtime"):
                if rel.as_posix() in (HTTP_SERVE_PATH, REPL_PATH):
                    continue
                parts = name.split(".")
                target_layer = (
                    parts[1] if len(parts) > 1 and parts[1] in LAYERS
                    else "entry"
                )
                if (
                    rel.as_posix() == UPDATE_ENGINE_PATH
                    and name == "sonder_runtime.bootstrap.app"
                ):
                    continue
                if target_layer not in ALLOWED_PACKAGE_EDGES[layer]:
                    violations.append(
                        f"{rel}: {layer} may not import {name} "
                        f"({target_layer} layer)"
                    )
                continue
            if top in STDLIB:
                if rel.as_posix() in (HTTP_SERVE_PATH, REPL_PATH):
                    continue
                if (
                    top == "subprocess"
                    and layer != "adapters"
                    and rel.as_posix() not in PLATFORM_SUBPROCESS_MODULES
                    and rel.as_posix() not in (UPDATE_ENGINE_PATH, VERSION_PLATFORM_PATH)
                ):
                    violations.append(
                        f"{rel}: subprocess outside adapters"
                    )
                # "entry" is the CLI adapter until SPEC-3 Phase 8 moves it
                # under adapters/cli; it may speak HTTP to the local server.
                if (
                    top in IO_MODULES
                    and layer not in ("adapters", "entry")
                    and rel.as_posix() not in PLATFORM_NETWORK_MODULES
                    and rel.as_posix() not in DOMAIN_PURE_URL_MODULES
                ):
                    violations.append(
                        f"{rel}: network module {top!r} outside adapters"
                    )
                continue
            if rel.as_posix() == HTTP_SERVE_PATH and top in HTTP_SERVE_ROOT_MODULES:
                continue
            if rel.as_posix() == REPL_PATH and top in REPL_ROOT_MODULES:
                continue
            if top not in ALLOWED_ROOT_IMPORTS[layer]:
                if (
                    rel.as_posix() == "sonder_runtime/adapters/learning_health.py"
                    and top in {"calibration", "memory_quality", "retriever"}
                ):
                    continue
                violations.append(
                    f"{rel}: {layer} layer may not import root/third-party "
                    f"module {top!r}"
                )

    violations.extend(find_cycles(imports))
    for module, compatibility_path in COMPATIBILITY_ROOT_MODULES.items():
        for offender in compatibility_import_offenders(
            module,
            compatibility_path,
            allowed_paths=COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS.get(
                module, frozenset()
            ),
        ):
            violations.append(
                f"{offender}: production caller imports compatibility root "
                f"module {module!r}"
            )
    return violations


def find_cycles(imports: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in done or node not in imports:
            return
        if node in visiting:
            cycle = path[path.index(node):] + [node]
            problems.append("import cycle: " + " -> ".join(cycle))
            return
        visiting.add(node)
        for dep in sorted(imports[node]):
            # Normalize package imports to their module entries.
            candidates = [d for d in imports if (d == dep or d.startswith(dep + ".")) and d != node]
            for candidate in candidates or ([dep] if dep in imports and dep != node else []):
                visit(candidate, path + [node])
        visiting.discard(node)
        done.add(node)

    for module in sorted(imports):
        visit(module, [])
    return problems


def main() -> int:
    if not PACKAGE_ROOT.is_dir():
        print(f"package not found: {PACKAGE_ROOT}", file=sys.stderr)
        return 2
    violations = check()
    for violation in violations:
        print(violation)
    if violations:
        print(f"\n{len(violations)} architecture violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
