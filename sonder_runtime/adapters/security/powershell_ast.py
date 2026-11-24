"""Fail-closed, read-only PowerShell AST inspection.

The helper process only parses text with PowerShell's native parser.  The
candidate is supplied as JSON on stdin; it is never interpolated into the
helper script and the helper has no execution path.

An inspectable verdict only means that none of the helper's checks matched.
They look for dynamic code and command names; the rebinding spellings and
item writers it recognises (function:/alias: paths and variables, alias and
drive cmdlets, item writers whose path operand is not a visible literal);
the engine and reflection member names and type families it lists, and
member names that the member-reading cmdlets it lists receive from
expressions (Compare-Object -Property included); parameter attributes that
bind by property name (ValueFromPipelineByPropertyName); $ExecutionContext,
$PSCmdlet and $PSDefaultParameterValues named as a variable (any scope or
provider spelling) or inside a string literal; *-Variable cmdlets given a
computed, wildcard, piped or missing name, and Tee-Object -Variable given a
computed or splatted name; Get-Item, Get-ChildItem and Get-Content given a computed or piped path;
item readers and writers given a variable: path that is not one literal
variable name, or a home-relative (~) path, which follows a provider's
settable Home; relative item paths in text that also changes the location
to a computed, home-relative or missing path; and short type names this
host cannot resolve.  The checks run under the invariant culture.  This is
a denylist of recognised routes, not proof that the text cannot rebind a
command or reach engine objects in some other way, and scripts, modules,
data files or programs the text runs or loads from disk are not inspected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Final


@dataclass(frozen=True, slots=True)
class PowerShellInspection:
    inspectable: bool
    reason: str

    def __bool__(self) -> bool:
        return self.inspectable


_MAX_SOURCE: Final = 256 * 1024
# Windows cold starts on loaded hosts exceeded five seconds during qualification.
# Only PowerShell execution calls pay this bounded cost; syntax cache hits do not.
_TIMEOUT: Final = 8.0
_CACHE_SIZE: Final = 128
_positive_cache: "OrderedDict[tuple[str, int, int, str], PowerShellInspection]" = OrderedDict()
_cache_lock = Lock()

# Keep the helper in a static package .ps1 file invoked via -File so the
# CreateProcess command line stays well under Windows' 32,767-char ceiling.
# ParseInput constructs an AST and does not invoke commands; all policy
# decisions are AST based.
_HELPER_PATH: Path = Path(__file__).with_name("powershell_ast_helper.ps1")
_HELPER: Final = _HELPER_PATH.read_text(encoding="utf-8")
_HELPER_COMMAND_DEFAULT: Final = "\n".join(
    line.strip() for line in _HELPER.splitlines() if line.strip() and not line.strip().startswith("#")
)
_HELPER_COMMAND: str = _HELPER_COMMAND_DEFAULT


def _powershell_executable() -> str | None:
    if os.name != "nt":
        candidate = shutil.which("pwsh")
        if candidate and os.path.isabs(candidate):
            candidate = os.path.realpath(candidate)
            cwd = os.path.realpath(os.getcwd())
            if not candidate.startswith(cwd + os.sep):
                return candidate
        return None
    roots = [os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")]
    for root in roots:
        if root:
            path = os.path.join(root, "PowerShell", "7", "pwsh.exe")
            if os.path.isfile(path):
                return os.path.abspath(path)
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        path = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _powershell_environment() -> dict[str, str]:
    keys = ("SystemRoot", "WINDIR", "ComSpec", "PATH", "PATHEXT", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramW6432", "HOME")
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def inspect_powershell(source: str) -> PowerShellInspection:
    """Parse *source* without executing it; parser failures require approval."""
    if not isinstance(source, str) or len(source) > _MAX_SOURCE:
        return PowerShellInspection(False, "invalid or oversized PowerShell source")
    executable = _powershell_executable()
    if executable is None:
        return PowerShellInspection(False, "PowerShell parser unavailable")
    try:
        stat = os.stat(executable)
        identity = (executable, stat.st_mtime_ns, stat.st_size, source)
    except OSError:
        return PowerShellInspection(False, "PowerShell parser unavailable")
    with _cache_lock:
        cached = _positive_cache.get(identity)
        if cached is not None:
            _positive_cache.move_to_end(identity)
            return cached
    startupinfo = subprocess.STARTUPINFO() if os.name == "nt" else None
    if startupinfo is not None:
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    if _HELPER_COMMAND != _HELPER_COMMAND_DEFAULT:
        helper_args = ["-Command", _HELPER_COMMAND]
    else:
        helper_path = Path(_HELPER_PATH)
        if not helper_path.is_file():
            return PowerShellInspection(False, "PowerShell inspection helper unavailable")
        helper_args = ["-File", str(helper_path)]
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", *helper_args],
            input=json.dumps({"source": source}, ensure_ascii=True),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT,
            startupinfo=startupinfo, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=_powershell_environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise subprocess.SubprocessError("parser helper exited nonzero")
        result = json.loads(completed.stdout.strip())
        if not isinstance(result, dict) or not isinstance(result.get("inspectable"), bool) or not isinstance(result.get("reason"), str):
            raise ValueError("malformed parser response")
        verdict = PowerShellInspection(result["inspectable"], result["reason"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return PowerShellInspection(False, f"PowerShell inspection failed closed: {type(exc).__name__}")
    if verdict.inspectable:
        with _cache_lock:
            _positive_cache[identity] = verdict
            _positive_cache.move_to_end(identity)
            while len(_positive_cache) > _CACHE_SIZE:
                _positive_cache.popitem(last=False)
    return verdict


def inspect_powershell_argv(argv: list[str] | tuple[str, ...]) -> PowerShellInspection:
    """Inspect a structured PowerShell argv without allowing shell expansion."""
    if not argv or not all(isinstance(item, str) for item in argv):
        return PowerShellInspection(False, "invalid PowerShell argv")

    def quote(item: str) -> str:
        # Preserve parameter tokens so CommandParameterAst remains visible;
        # quote every other argument as a literal PowerShell string.
        if item.startswith("-") and item.replace("-", "").replace("_", "").isalnum():
            return item
        return "'" + item.replace("'", "''") + "'"

    return inspect_powershell("& " + " ".join(quote(item) for item in argv))
