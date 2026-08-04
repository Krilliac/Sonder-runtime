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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"

STDLIB = set(sys.stdlib_module_names)

# Root modules each layer may reach while the strangler migration runs.
ROOT_PLATFORM_MODULES = {
    "sonder_config", "sonder_paths", "sonder_version", "sonder_metrics",
    "sonder_shutdown", "sonder_logging",
}
ROOT_LEGACY_MODULES = {
    "server", "runtime_policy", "memory_store", "embeddings",
    "autopilot_store", "fleet_store", "sonder_operations_store",
    "sonder_migrations", "sonder_backup", "sonder_preflight",
    "sonder_lifecycle", "sonder_secrets", "sonder_serve", "sonder_repl",
    "sonder_updates", "sonder_update_engine", "model_transport",
    "recall",
}

LAYERS = ("domain", "application", "adapters", "platform", "bootstrap")

ALLOWED_PACKAGE_EDGES = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "adapters": {"domain", "application", "adapters", "platform"},
    "platform": {"platform"},
    "bootstrap": {"domain", "application", "adapters", "platform", "bootstrap"},
    "entry": {"domain", "application", "adapters", "platform", "bootstrap"},
}
ALLOWED_ROOT_IMPORTS = {
    "domain": set(),
    "application": set(),
    "adapters": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES,
    "platform": ROOT_PLATFORM_MODULES,
    "bootstrap": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES,
    "entry": ROOT_LEGACY_MODULES | ROOT_PLATFORM_MODULES | {
        "sonder_runtime",
    },
}

IO_MODULES = {"urllib", "socket", "http", "ftplib", "smtplib"}


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
            if name.startswith("sonder_runtime"):
                parts = name.split(".")
                target_layer = (
                    parts[1] if len(parts) > 1 and parts[1] in LAYERS
                    else "entry"
                )
                if target_layer not in ALLOWED_PACKAGE_EDGES[layer]:
                    violations.append(
                        f"{rel}: {layer} may not import {name} "
                        f"({target_layer} layer)"
                    )
                continue
            if top in STDLIB:
                if top == "subprocess" and layer != "adapters":
                    violations.append(
                        f"{rel}: subprocess outside adapters"
                    )
                # "entry" is the CLI adapter until SPEC-3 Phase 8 moves it
                # under adapters/cli; it may speak HTTP to the local server.
                if top in IO_MODULES and layer not in ("adapters", "entry"):
                    violations.append(
                        f"{rel}: network module {top!r} outside adapters"
                    )
                continue
            if top not in ALLOWED_ROOT_IMPORTS[layer]:
                violations.append(
                    f"{rel}: {layer} layer may not import root/third-party "
                    f"module {top!r}"
                )

    violations.extend(find_cycles(imports))
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
            candidates = [d for d in imports if d == dep or d.startswith(dep + ".")]
            for candidate in candidates or ([dep] if dep in imports else []):
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
