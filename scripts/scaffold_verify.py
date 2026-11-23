"""Render every project scaffold and verify it with an installed toolchain."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "runtime"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import project_scaffold  # noqa: E402


PROJECT_NAME = "ScaffoldVerify"
BUILD_TIMEOUT_SECONDS = 120

# `compileall -q src` over the Python scaffold only proves that an empty
# __init__.py and a print() parse. pyproject.toml -- the file most likely to be
# malformed, and the only one a packaging tool actually reads -- was never
# opened before this script printed VERIFIED. Parsing it is the cheapest way to
# make the Python verdict mean something.
_PYPROJECT_CHECK = (
    "import pathlib,sys,tomllib;"
    "p=pathlib.Path('pyproject.toml');"
    "sys.exit('pyproject.toml is missing') if not p.is_file() else None;"
    "d=tomllib.loads(p.read_text('utf-8'));"
    "sys.exit('pyproject.toml declares no [project] name') "
    "if not d.get('project',{}).get('name') else None"
)


def _commands(kind: str, tool: str) -> list[list[str]]:
    commands = {
        "rust": [[tool, "build"]],
        "csharp": [[tool, "build"]],
        "go": [[tool, "build", "./..."]],
        "node": [[tool, "--check", "index.js"]],
        "python": [
            [tool, "-c", _PYPROJECT_CHECK],
            [tool, "-m", "compileall", "-q", "src"],
        ],
        "cpp-cmake": [
            [tool, "-S", ".", "-B", "build"],
            [tool, "--build", "build", "--config", "Debug"],
        ],
        "c-cmake": [
            [tool, "-S", ".", "-B", "build"],
            [tool, "--build", "build", "--config", "Debug"],
        ],
        "cpp-msvc": [[tool, f"{PROJECT_NAME}.sln", "/p:Configuration=Debug"]],
        "java-maven": [[tool, "-q", "-B", "compile"]],
        "typescript": [[tool, "--noEmit"]],
        "bash-cli": [[tool, "-n", f"bin/{project_scaffold.identifier(PROJECT_NAME)}.sh"]],
        "dart": [[tool, "analyze"]],
        "elixir": [[tool, "compile"]],
        "flutter": [[tool, "analyze"]],
        "kotlin-gradle": [[tool, "compileKotlin"]],
        "lua": [[tool, "-p", "src/main.lua"]],
        "php": [[tool, "-l", "index.php"]],
        "powershell": [[tool, "-NoProfile", "-Command", f"Import-Module ./{PROJECT_NAME}.psm1"]],
        "ruby": [[tool, "-c", f"lib/{project_scaffold.identifier(PROJECT_NAME)}.rb"]],
        "swift": [[tool, "build"]],
        "web-app": [[tool, "--check", "app.js"]],
        "zig": [[tool, "build"]],
    }
    return commands[kind]


def _tool_name(kind: str) -> str:
    return {
        "rust": "cargo",
        "csharp": "dotnet",
        "go": "go",
        "node": "node",
        "python": "python",
        "cpp-cmake": "cmake",
        "c-cmake": "cmake",
        "cpp-msvc": "msbuild",
        "java-maven": "mvn",
        "typescript": "tsc",
        "bash-cli": "bash",
        "dart": "dart",
        "elixir": "mix",
        "flutter": "flutter",
        "kotlin-gradle": "gradle",
        "lua": "luac",
        "php": "php",
        "powershell": "pwsh",
        "ruby": "ruby",
        "swift": "swift",
        "web-app": "node",
        "zig": "zig",
    }[kind]


def _output_tail(result, limit: int = 500) -> str:
    combined = " ".join(((result.stderr or "") + "\n" + (result.stdout or "")).split())
    return combined[-limit:] or "no build output"


def _render(kind: str, destination: Path) -> None:
    for relative_path, content in project_scaffold.render(kind, PROJECT_NAME).items():
        output_path = destination / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")


def main() -> int:
    failed = False
    for kind in project_scaffold.kinds():
        with tempfile.TemporaryDirectory(prefix=f"scaffold-{kind}-") as temp:
            project_dir = Path(temp)
            _render(kind, project_dir)
            tool_name = _tool_name(kind)
            tool = shutil.which(tool_name)
            if tool is None:
                print(f"{kind}: SKIP (toolchain missing) [{tool_name}]")
                continue

            failure = ""
            for command in _commands(kind, tool):
                try:
                    result = subprocess.run(
                        command,
                        cwd=project_dir,
                        capture_output=True,
                        text=True,
                        timeout=BUILD_TIMEOUT_SECONDS,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failure = " ".join(str(exc).split())[-500:] or type(exc).__name__
                    break
                if result.returncode != 0:
                    failure = _output_tail(result)
                    break

        if failure:
            print(f"{kind}: FAILED - {failure}")
            failed = True
        else:
            print(f"{kind}: VERIFIED")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
