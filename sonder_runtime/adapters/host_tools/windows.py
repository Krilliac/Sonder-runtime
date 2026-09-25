"""Windows-specific tool discovery beyond PATH.

Sources: vswhere (Visual Studio / MSVC / MSBuild), the Windows SDK
(``KitsRoot10``), registry App Paths, the ``py`` launcher, and well-known
install prefixes (scoop, Chocolatey, winget links, Program Files, MSYS2).

Only fixed argv is ever executed here: vswhere from its fixed Program Files
path and ``py -0p``.  Compiler, linker, MSBuild and IDE records come from
metadata and file existence alone.
"""
from __future__ import annotations

import json
import re

from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
)

from .guards import is_windows_apps_alias
from .probes import HostProbes, probe_environment

VSWHERE_ARGS = ("-products", "*", "-format", "json", "-utf8", "-nologo")
VSWHERE_TIMEOUT_SECONDS = 5.0
VSWHERE_MAX_OUTPUT_CHARS = 256 * 1024
MAX_VS_INSTALLS = 16
MAX_SDK_VERSIONS = 64
MAX_PY_LINES = 32
PY_LAUNCHER_TIMEOUT_SECONDS = 3.0
MAX_PYTHON_GLOB = 8
_SDK_KEY = r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
_VERSION_DIR = re.compile(r"^10\.0\.\d+\.\d+$")
_TOOLSET = re.compile(r"^\d+\.\d+\.\d+$")
_PY_V_FORM = re.compile(r"^\s*-V:(?P<tag>[\w.\-/]+)\s*(?P<default>\*)?\s+(?P<path>.+?)\s*$")
_PY_OLD_FORM = re.compile(r"^\s*-(?P<tag>\d+\.\d+(?:-(?:32|64|arm64))?)\s*(?P<default>\*)?\s+(?P<path>.+?)\s*$")


def _join(*parts: str) -> str:
    head = parts[0].rstrip("\\/")
    tail = [part.strip("\\/") for part in parts[1:] if part]
    return "\\".join([head, *tail])


def _identity(probes: HostProbes, path: str) -> str:
    return probes.stat_identity(path) or ""


def _clip(value: str, limit: int = 200) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:limit]


def _record(
    probes: HostProbes,
    name: str,
    category: ToolCategory,
    path: str,
    source: DiscoverySource,
    version: str,
    details: tuple[tuple[str, str], ...] = (),
) -> ToolRecord:
    return ToolRecord(
        name=name,
        category=category,
        path=path,
        source=source,
        on_path=False,
        version=_clip(version, 64),
        version_status=VersionStatus.FROM_METADATA if version else VersionStatus.NOT_PROBED,
        identity=_identity(probes, path),
        details=tuple(details[:8]),
    )


def vswhere_path(probes: HostProbes) -> str:
    base = probes.env_get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    return _join(base, "Microsoft Visual Studio", "Installer", "vswhere.exe")


def _read_text(probes: HostProbes, path: str, limit: int) -> str:
    data = probes.read_small(path, limit)
    if data is None:
        return ""
    return data.decode("utf-8", "replace").strip()


def discover_visual_studio(probes: HostProbes, notes: list[str] | None = None) -> list[ToolRecord]:
    """cl/link/msbuild/devenv records from vswhere metadata (only vswhere runs)."""
    notes = notes if notes is not None else []
    vswhere = vswhere_path(probes)
    if not probes.is_file(vswhere):
        return []
    try:
        planted = bool(probes.project_local(vswhere))
    except Exception:
        planted = True  # fail closed: an unverifiable path is never run
    if planted:
        notes.append("vswhere is inside a project root; not run")
        return []
    run = probes.run((vswhere, *VSWHERE_ARGS), VSWHERE_TIMEOUT_SECONDS, probe_environment(probes),
                     max_output_chars=VSWHERE_MAX_OUTPUT_CHARS)
    if run.outcome != "ok":
        notes.append(f"vswhere {run.outcome}")
        return []
    try:
        installs = json.loads(run.output or "[]")
    except ValueError:
        notes.append("vswhere returned invalid JSON")
        return []
    if not isinstance(installs, list):
        notes.append("vswhere returned an unexpected document")
        return []
    records: list[ToolRecord] = []
    for install in installs[:MAX_VS_INSTALLS]:
        if not isinstance(install, dict):
            continue
        root = install.get("installationPath")
        if not isinstance(root, str) or not re.match(r"^[A-Za-z]:\\", root) or "\x00" in root:
            continue
        display = _clip(install.get("displayName") or "", 200)
        vs_version = _clip(install.get("installationVersion") or "", 64)
        toolset = _read_text(
            probes,
            _join(root, "VC", "Auxiliary", "Build", "Microsoft.VCToolsVersion.default.txt"),
            64,
        )
        base_details: list[tuple[str, str]] = []
        if display:
            base_details.append(("vs_display_name", display))
        if vs_version:
            base_details.append(("vs_version", vs_version))
        if toolset and _TOOLSET.match(toolset):
            bin_dir = _join(root, "VC", "Tools", "MSVC", toolset, "bin", "Hostx64", "x64")
            vcvars = _join(root, "VC", "Auxiliary", "Build", "vcvars64.bat")
            msvc_details = tuple(base_details) + (("toolset", toolset),)
            if probes.is_file(vcvars):
                msvc_details += (("vcvars64", vcvars),)
            cl = _join(bin_dir, "cl.exe")
            link = _join(bin_dir, "link.exe")
            if probes.is_file(cl):
                records.append(_record(probes, "cl", ToolCategory.COMPILER, cl,
                                       DiscoverySource.VSWHERE, toolset, msvc_details))
            if probes.is_file(link):
                records.append(_record(probes, "link", ToolCategory.BUILD_SYSTEM, link,
                                       DiscoverySource.VSWHERE, toolset, msvc_details))
        msbuild = _join(root, "MSBuild", "Current", "Bin", "MSBuild.exe")
        if probes.is_file(msbuild):
            records.append(_record(probes, "msbuild", ToolCategory.BUILD_SYSTEM, msbuild,
                                   DiscoverySource.VSWHERE, vs_version, tuple(base_details)))
        devenv = _join(root, "Common7", "IDE", "devenv.exe")
        if probes.is_file(devenv):
            records.append(_record(probes, "devenv", ToolCategory.EDITOR_IDE, devenv,
                                   DiscoverySource.VSWHERE, vs_version, tuple(base_details)))
    return records


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text))


def discover_windows_sdk(probes: HostProbes) -> list[ToolRecord]:
    """windows-sdk plus rc/signtool and the SDK debuggers (metadata only)."""
    registry = probes.registry
    if registry is None:
        return []
    root = registry.value("HKLM", _SDK_KEY, "KitsRoot10")
    if not root or not re.match(r"^[A-Za-z]:\\", root) or "\x00" in root:
        return []
    versions = [
        key for key in registry.subkeys("HKLM", _SDK_KEY, limit=MAX_SDK_VERSIONS)
        if _VERSION_DIR.match(key)
    ]
    bin_root = _join(root, "bin")
    versions.extend(
        name for name in probes.list_dir(bin_root, MAX_SDK_VERSIONS) if _VERSION_DIR.match(name)
    )
    versions = sorted(set(versions), key=_version_key, reverse=True)
    if not versions:
        return []
    newest = versions[0]
    details = (("sdk_version", newest), ("sdk_versions", ",".join(versions[:8])))
    records = [_record(probes, "windows-sdk", ToolCategory.BUILD_SYSTEM, root.rstrip("\\"),
                       DiscoverySource.WINDOWS_SDK, newest, details)]
    for exe, category in (("rc", ToolCategory.BUILD_SYSTEM), ("signtool", ToolCategory.BUILD_SYSTEM)):
        path = _join(bin_root, newest, "x64", exe + ".exe")
        if probes.is_file(path):
            records.append(_record(probes, exe, category, path, DiscoverySource.WINDOWS_SDK,
                                   newest, (("sdk_version", newest),)))
    for exe in ("cdb", "windbg"):
        path = _join(root, "Debuggers", "x64", exe + ".exe")
        if probes.is_file(path):
            records.append(_record(probes, exe, ToolCategory.DEBUGGER_PROFILER, path,
                                   DiscoverySource.WINDOWS_SDK, newest, (("sdk_version", newest),)))
    return records


def discover_app_paths(probes: HostProbes, names) -> list[tuple[str, str]]:
    """``(name, path)`` for names registered under App Paths (HKLM, then HKCU)."""
    registry = probes.registry
    if registry is None:
        return []
    found: list[tuple[str, str]] = []
    for name in names:
        if not re.match(r"^[A-Za-z0-9+._-]{1,64}$", name):
            continue
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        for hive in ("HKLM", "HKCU"):
            value = registry.value(hive, _APP_PATHS_KEY + "\\" + exe, "")
            if not value:
                continue
            path = value.strip().strip('"')
            if re.match(r"^[A-Za-z]:\\", path) and "\x00" not in path and probes.is_file(path):
                found.append((name, path))
                break
    return found


def parse_py_launcher(text: str) -> list[tuple[str, str, bool]]:
    """Parse ``py -0p`` output into ``(tag, path, is_default)`` (both formats)."""
    entries: list[tuple[str, str, bool]] = []
    for line in (text or "").splitlines()[:MAX_PY_LINES]:
        match = _PY_V_FORM.match(line) or _PY_OLD_FORM.match(line)
        if match is None:
            continue
        path = match.group("path").strip()
        if not re.match(r"^[A-Za-z]:\\", path):
            continue
        entries.append((match.group("tag"), path, bool(match.group("default"))))
    return entries


def discover_py_launcher(probes: HostProbes, launcher: str) -> list[tuple[str, str, bool]]:
    """Run ``<launcher> -0p`` (fixed argv) and parse the interpreter list."""
    if not launcher or is_windows_apps_alias(launcher):
        return []
    run = probes.run((launcher, "-0p"), PY_LAUNCHER_TIMEOUT_SECONDS, probe_environment(probes))
    if run.outcome != "ok":
        return []
    return parse_py_launcher(run.output)


def extra_dir_sources(probes: HostProbes) -> list[tuple[str, DiscoverySource]]:
    """Well-known Windows tool directories with the source that names them."""
    env = probes.env_get
    profile = env("USERPROFILE") or probes.home
    program_files = env("ProgramFiles") or r"C:\Program Files"
    program_data = env("ProgramData") or r"C:\ProgramData"
    local = env("LOCALAPPDATA") or (_join(profile, "AppData", "Local") if profile else "")
    dirs: list[tuple[str, DiscoverySource]] = []
    scoop = env("SCOOP")
    dirs.append((_join(scoop, "shims") if scoop else _join(profile, "scoop", "shims"), DiscoverySource.SCOOP))
    choco = env("ChocolateyInstall")
    dirs.append((_join(choco, "bin") if choco else _join(program_data, "chocolatey", "bin"), DiscoverySource.CHOCO))
    if local:
        dirs.append((_join(local, "Microsoft", "WinGet", "Links"), DiscoverySource.WINGET))
    for sub in ("Git\\cmd", "CMake\\bin", "nodejs", "LLVM\\bin", "dotnet", "Docker\\Docker\\resources\\bin"):
        dirs.append((_join(program_files, sub), DiscoverySource.KNOWN_PREFIX))
    if profile:
        dirs.append((_join(profile, ".cargo", "bin"), DiscoverySource.KNOWN_PREFIX))
    if local:
        python_root = _join(local, "Programs", "Python")
        names = [n for n in probes.list_dir(python_root, 64) if re.match(r"^Python3\d*$", n)]
        for name in sorted(names, key=_version_key, reverse=True)[:MAX_PYTHON_GLOB]:
            dirs.append((_join(python_root, name), DiscoverySource.KNOWN_PREFIX))
    dirs.append((r"C:\msys64\usr\bin", DiscoverySource.KNOWN_PREFIX))
    dirs.append((r"C:\msys64\mingw64\bin", DiscoverySource.KNOWN_PREFIX))
    return [(path, source) for path, source in dirs if path and re.match(r"^[A-Za-z]:\\", path)]


def extra_dirs(probes: HostProbes) -> list[str]:
    return [path for path, _source in extra_dir_sources(probes)]


__all__ = [
    "VSWHERE_ARGS",
    "discover_app_paths",
    "discover_py_launcher",
    "discover_visual_studio",
    "discover_windows_sdk",
    "extra_dir_sources",
    "extra_dirs",
    "is_windows_apps_alias",
    "parse_py_launcher",
    "vswhere_path",
]
