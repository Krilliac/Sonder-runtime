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
ROOT_PLATFORM_MODULES = {
    "sonder_config", "sonder_paths", "sonder_version", "sonder_metrics",
    "sonder_shutdown", "sonder_logging",
}
BASELINE_ROOT_LEGACY_MODULES = frozenset({
    "server", "runtime_policy", "embeddings",
    "autopilot_store", "fleet_store", "sonder_operations_store",
    "sonder_migrations",
    "sonder_lifecycle", "sonder_secrets", "sonder_serve", "sonder_repl",
    "sonder_updates", "sonder_update_engine",
    "workbench", "file_ops",
})
ROOT_LEGACY_MODULES = set(BASELINE_ROOT_LEGACY_MODULES)
# This is a ratchet, not a target.  Removing a legacy root dependency is
# always allowed; adding one requires an explicit architecture-policy change
# and must never happen as an accidental convenience import.
ROOT_LEGACY_MODULE_LIMIT = 16

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
    "eval_history": Path("eval_history.py"),
    "memory_store": Path("memory_store.py"),
    "recall": Path("recall.py"),
}

# Applied migrations are immutable historical artifacts. They may retain an
# import that production code has since moved behind a compatibility adapter;
# rewriting one would invalidate its recorded checksum on deployed systems.
COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS = {
    "memory_store": frozenset({Path("migrations/memory/0001_baseline.py")}),
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
