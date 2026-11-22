"""Zero-subprocess language & toolchain capability matrix for Sonder.

Reports every language supported across Sonder's execution, scaffolding,
symbol indexing, build, test, lint, format, typecheck, package manager, and
code-metrics layers, plus whether the primary host executable is currently
discoverable on PATH (using ``shutil.which`` only — never spawns a process).
"""
from __future__ import annotations

import shutil

import harness_tools
import project_scaffold
import symbol_index
from sonder_runtime.adapters.execution_tools import code_runner
from sonder_runtime.adapters.inspection import code_metrics


_LANGUAGE_PROBES: dict[str, tuple[str, ...]] = {
    "python": ("python", "python3", "py"),
    "javascript": ("node",),
    "typescript": ("node", "tsc"),
    "c": ("gcc", "clang", "cl"),
    "cpp": ("g++", "clang++", "cl", "cmake"),
    "csharp": ("dotnet", "csc"),
    "rust": ("rustc", "cargo"),
    "go": ("go",),
    "java": ("java", "javac", "mvn"),
    "kotlin": ("kotlinc", "kotlin", "gradle"),
    "dart": ("dart", "flutter"),
    "flutter": ("flutter", "dart"),
    "swift": ("swift",),
    "zig": ("zig",),
    "ruby": ("ruby", "bundle"),
    "php": ("php", "composer"),
    "lua": ("lua", "lua5.4", "lua5.3"),
    "elixir": ("elixir", "mix"),
    "scala": ("scala", "sbt"),
    "haskell": ("runghc", "ghc", "cabal", "stack"),
    "julia": ("julia",),
    "nim": ("nim", "nimble"),
    "r": ("Rscript",),
    "perl": ("perl",),
    "powershell": ("pwsh", "powershell"),
    "bash": ("bash", "sh"),
    "batch": ("cmd",),
    "sql": ("python", "sqlite3"),
}

_SCAFFOLD_MAP: dict[str, list[str]] = {
    "python": ["python"],
    "javascript": ["node", "web-app"],
    "typescript": ["typescript"],
    "c": ["c-cmake"],
    "cpp": ["cpp-msvc", "cpp-cmake"],
    "csharp": ["csharp"],
    "rust": ["rust"],
    "go": ["go"],
    "java": ["java-maven"],
    "kotlin": ["kotlin-gradle"],
    "dart": ["dart"],
    "flutter": ["flutter"],
    "swift": ["swift"],
    "zig": ["zig"],
    "ruby": ["ruby"],
    "php": ["php"],
    "lua": ["lua"],
    "elixir": ["elixir"],
    "powershell": ["powershell"],
    "bash": ["bash-cli"],
}

_BUILD_TEST_LINT: dict[str, dict[str, list[str]]] = {
    "python": {"build": ["pyproject/compileall"], "test": ["pytest", "unittest"], "lint": ["ruff", "flake8", "pylint", "pyflakes"], "format": ["ruff", "black"], "typecheck": ["mypy", "pyright"], "pkg": ["pip", "uv"]},
    "javascript": {"build": ["npm"], "test": ["jest", "vitest", "mocha", "bun", "node-test"], "lint": ["eslint", "biome"], "format": ["prettier", "biome"], "typecheck": [], "pkg": ["npm", "pnpm", "yarn", "bun"]},
    "typescript": {"build": ["npm"], "test": ["jest", "vitest", "mocha", "bun"], "lint": ["eslint", "biome"], "format": ["prettier", "biome"], "typecheck": ["tsc"], "pkg": ["npm", "pnpm", "yarn", "bun"]},
    "c": {"build": ["cmake", "make", "meson", "bazel"], "test": ["ctest"], "lint": ["clang-tidy"], "format": ["clang-format"], "typecheck": [], "pkg": []},
    "cpp": {"build": ["cmake", "make", "meson", "msbuild", "bazel", "scons"], "test": ["ctest"], "lint": ["clang-tidy"], "format": ["clang-format"], "typecheck": [], "pkg": []},
    "csharp": {"build": ["dotnet"], "test": ["dotnet"], "lint": ["dotnet"], "format": ["dotnet"], "typecheck": ["dotnet"], "pkg": ["dotnet"]},
    "rust": {"build": ["cargo"], "test": ["cargo"], "lint": ["clippy"], "format": ["rustfmt"], "typecheck": ["cargo-check"], "pkg": ["cargo"]},
    "go": {"build": ["go"], "test": ["go"], "lint": ["golangci-lint", "go-vet"], "format": ["gofmt"], "typecheck": ["go-vet"], "pkg": ["go"]},
    "java": {"build": ["maven", "gradle"], "test": ["maven", "gradle"], "lint": [], "format": [], "typecheck": [], "pkg": []},
    "kotlin": {"build": ["gradle"], "test": ["gradle"], "lint": ["ktlint"], "format": ["ktlint"], "typecheck": [], "pkg": []},
    "dart": {"build": ["dart"], "test": ["dart"], "lint": ["dart"], "format": ["dart"], "typecheck": ["dart"], "pkg": ["dart"]},
    "flutter": {"build": ["flutter"], "test": ["flutter"], "lint": ["dart"], "format": ["dart"], "typecheck": ["dart"], "pkg": ["flutter"]},
    "swift": {"build": ["swift"], "test": ["swift"], "lint": ["swiftlint"], "format": ["swift-format"], "typecheck": [], "pkg": ["swift"]},
    "zig": {"build": ["zig"], "test": ["zig"], "lint": [], "format": ["zig"], "typecheck": ["zig"], "pkg": []},
    "ruby": {"build": ["rake"], "test": ["rspec"], "lint": ["rubocop"], "format": ["rubocop"], "typecheck": ["sorbet"], "pkg": ["bundler"]},
    "php": {"build": ["composer"], "test": ["phpunit"], "lint": ["phpstan", "phpcs"], "format": ["phpcbf"], "typecheck": ["phpstan"], "pkg": ["composer"]},
    "lua": {"build": ["luarocks"], "test": [], "lint": ["luacheck"], "format": ["stylua"], "typecheck": [], "pkg": []},
    "elixir": {"build": ["mix"], "test": ["elixir"], "lint": ["credo"], "format": ["mix"], "typecheck": ["dialyzer"], "pkg": ["mix"]},
    "scala": {"build": ["sbt"], "test": ["sbt"], "lint": [], "format": [], "typecheck": [], "pkg": []},
    "haskell": {"build": ["stack", "cabal"], "test": ["stack", "cabal"], "lint": [], "format": [], "typecheck": [], "pkg": []},
    "bash": {"build": [], "test": [], "lint": ["shellcheck"], "format": ["shfmt"], "typecheck": [], "pkg": []},
}


def inspect_language_matrix(language: str = "") -> dict:
    """Return Sonder's full language capability matrix and live host tool availability."""
    query = str(language or "").strip().casefold()
    run_langs = set(code_runner.SUPPORTED_LANGUAGES)
    sym_langs = set(symbol_index.LANGUAGE_ALIASES.values())
    metric_langs = {lang for lang, _ in code_metrics._EXT_LANG.values()}
    scaffold_kinds = set(project_scaffold.kinds())

    all_langs = sorted(
        set(_LANGUAGE_PROBES) | run_langs | sym_langs | set(_SCAFFOLD_MAP)
    )
    if query:
        all_langs = [lang for lang in all_langs if query in lang]

    entries: list[dict] = []
    installed_count = 0
    for lang in all_langs:
        probes = _LANGUAGE_PROBES.get(lang, ())
        found = [exe for exe in probes if shutil.which(exe)]
        if lang == "sql":
            found = ["python:sqlite3"]
        is_installed = bool(found)
        if is_installed:
            installed_count += 1
        scaffolds = [s for s in _SCAFFOLD_MAP.get(lang, []) if s in scaffold_kinds]
        tools = _BUILD_TEST_LINT.get(lang, {})
        entries.append({
            "language": lang,
            "host_ready": is_installed,
            "detected_binaries": found,
            "run_code": lang in run_langs,
            "symbol_index": lang in sym_langs,
            "code_metrics": lang in metric_langs or (lang == "bash" and "shell" in metric_langs),
            "scaffolds": scaffolds,
            "build": tools.get("build", []),
            "test": tools.get("test", []),
            "lint": tools.get("lint", []),
            "format": tools.get("format", []),
            "typecheck": tools.get("typecheck", []),
            "package_manager": tools.get("pkg", []),
        })

    return {
        "ok": True,
        "filter": query,
        "language_count": len(entries),
        "host_ready_count": installed_count,
        "totals": {
            "run_code_languages": len(run_langs),
            "scaffold_templates": len(scaffold_kinds),
            "symbol_index_languages": len(sym_langs),
            "test_frameworks": len(harness_tools._TEST_FRAMEWORKS),
            "linters": len(harness_tools._LINT_TOOLS),
            "formatters": len(harness_tools._FORMATTERS),
            "typecheckers": len(harness_tools._TYPECHECKERS),
            "code_metrics_extensions": len(code_metrics._EXT_LANG),
        },
        "languages": entries,
    }


def format_result(data: dict) -> str:
    totals = data.get("totals") or {}
    lines = [
        f"sonder language matrix ({data.get('language_count', 0)} languages; {data.get('host_ready_count', 0)} ready on this host)",
        (
            f"  surface: run_code={totals.get('run_code_languages', 0)}  "
            f"scaffolds={totals.get('scaffold_templates', 0)}  "
            f"symbol_index={totals.get('symbol_index_languages', 0)}  "
            f"test={totals.get('test_frameworks', 0)}  "
            f"lint={totals.get('linters', 0)}  "
            f"format={totals.get('formatters', 0)}  "
            f"typecheck={totals.get('typecheckers', 0)}  "
            f"metrics_ext={totals.get('code_metrics_extensions', 0)}"
        ),
    ]
    for item in data.get("languages") or []:
        ready = "READY" if item["host_ready"] else "-----"
        caps = []
        if item["run_code"]:
            caps.append("run")
        if item["scaffolds"]:
            caps.append("scaffold:" + ",".join(item["scaffolds"]))
        if item["symbol_index"]:
            caps.append("symbols")
        if item["build"]:
            caps.append("build")
        if item["test"]:
            caps.append("test")
        if item["lint"]:
            caps.append("lint")
        if item["format"]:
            caps.append("fmt")
        if item["typecheck"]:
            caps.append("type")
        if item["package_manager"]:
            caps.append("pkg")
        lines.append(f"  [{ready}] {item['language']:<11} {' '.join(caps)}")
    return "\n".join(lines)


__all__ = [
    "format_result",
    "inspect_language_matrix",
]
