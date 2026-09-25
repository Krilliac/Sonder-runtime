"""macOS-specific tool discovery beyond PATH.

Sources: the Homebrew prefix (``brew --prefix``), the Xcode command line
tools / Xcode developer directory (``xcode-select -p``, ``pkgutil``, and
``xcodebuild -version`` only inside ``Xcode.app``), and ``/Applications``
bundles whose version comes from ``Info.plist`` (never launched).
"""
from __future__ import annotations

import plistlib
import re

from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    parse_version,
)

from .probes import HostProbes, probe_environment

PROBE_TIMEOUT_SECONDS = 3.0
MAX_BREW_OPT = 256
MAX_APPLICATIONS = 512
MAX_PLIST_BYTES = 256 * 1024
CLT_PACKAGE = "com.apple.pkg.CLTools_Executables"

# Bundle name -> (record name, category).  Records are added only for names
# not already found as executables.
APP_BUNDLES: dict[str, tuple[str, ToolCategory]] = {
    "Visual Studio Code.app": ("code", ToolCategory.EDITOR_IDE),
    "IntelliJ IDEA.app": ("idea", ToolCategory.EDITOR_IDE),
    "IntelliJ IDEA CE.app": ("idea", ToolCategory.EDITOR_IDE),
    "PyCharm.app": ("pycharm", ToolCategory.EDITOR_IDE),
    "PyCharm CE.app": ("pycharm", ToolCategory.EDITOR_IDE),
    "CLion.app": ("clion", ToolCategory.EDITOR_IDE),
    "Rider.app": ("rider", ToolCategory.EDITOR_IDE),
    "GoLand.app": ("goland", ToolCategory.EDITOR_IDE),
    "Xcode.app": ("xcode", ToolCategory.EDITOR_IDE),
    "Docker.app": ("docker", ToolCategory.CONTAINER_VM),
}


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _absolute(path: str) -> bool:
    return path.startswith("/") and "\x00" not in path and ".." not in path.split("/")


def discover_brew(probes: HostProbes, brew: str | None = None) -> list[str]:
    """``<prefix>/bin``, ``<prefix>/sbin`` and ``<prefix>/opt/*/bin`` directories."""
    candidates = [brew] if brew else []
    candidates += ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
    executable = next((item for item in candidates if item and probes.is_file(item)), "")
    if not executable:
        return []
    env = probe_environment(probes, (("HOMEBREW_NO_AUTO_UPDATE", "1"),))
    run = probes.run((executable, "--prefix"), PROBE_TIMEOUT_SECONDS, env)
    if run.outcome != "ok":
        return []
    prefix = _first_line(run.output).rstrip("/")
    if not _absolute(prefix) or prefix == "":
        return []
    dirs = [prefix + "/bin", prefix + "/sbin"]
    opt = prefix + "/opt"
    for name in probes.list_dir(opt, MAX_BREW_OPT):
        if re.match(r"^[A-Za-z0-9@+._-]{1,128}$", name):
            candidate = f"{opt}/{name}/bin"
            if probes.is_dir(candidate):
                dirs.append(candidate)
    return [item for item in dirs if probes.is_dir(item)]


def _plist_version(probes: HostProbes, bundle: str) -> str:
    data = probes.read_small(bundle + "/Contents/Info.plist", MAX_PLIST_BYTES)
    if not data:
        return ""
    try:
        info = plistlib.loads(data)
    except Exception:
        return ""
    value = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    return parse_version(value, r"(\d+(?:\.\d+){0,3})") if isinstance(value, str) else ""


def _record(probes, name, category, path, source, version, details=()) -> ToolRecord:
    return ToolRecord(
        name=name,
        category=category,
        path=path,
        source=source,
        on_path=False,
        version=version[:64],
        version_status=VersionStatus.FROM_METADATA if version else VersionStatus.NOT_PROBED,
        # Directories (developer dir, .app bundles) have no file identity.
        identity=probes.stat_identity(path) or "",
        details=tuple(details)[:8],
    )


def discover_xcode(probes: HostProbes) -> list[ToolRecord]:
    """xcode-clt (and Xcode) from the selected developer directory."""
    select = "/usr/bin/xcode-select"
    if not probes.is_file(select):
        return []
    env = probe_environment(probes)
    run = probes.run((select, "-p"), PROBE_TIMEOUT_SECONDS, env)
    if run.outcome != "ok":
        return []
    developer_dir = _first_line(run.output).rstrip("/")
    if not _absolute(developer_dir) or not probes.is_dir(developer_dir):
        return []
    records: list[ToolRecord] = []
    details = (("developer_dir", developer_dir),)
    if ".app/Contents/Developer" in developer_dir and developer_dir.split("/")[-3].endswith(".app"):
        xcodebuild = developer_dir + "/usr/bin/xcodebuild"
        version = ""
        if probes.is_file(xcodebuild):
            result = probes.run((xcodebuild, "-version"), PROBE_TIMEOUT_SECONDS, env)
            if result.outcome == "ok":
                version = parse_version(result.output, r"Xcode (\d+(?:\.\d+){0,3})")
        records.append(_record(probes, "xcode-clt", ToolCategory.COMPILER, developer_dir,
                               DiscoverySource.XCODE, version, details + (("variant", "xcode"),)))
    else:
        pkgutil = "/usr/sbin/pkgutil"
        version = ""
        if probes.is_file(pkgutil):
            result = probes.run((pkgutil, f"--pkg-info={CLT_PACKAGE}"), PROBE_TIMEOUT_SECONDS, env)
            if result.outcome == "ok":
                version = parse_version(result.output, r"version:\s*(\d+(?:\.\d+){0,3})")
        records.append(_record(probes, "xcode-clt", ToolCategory.COMPILER, developer_dir,
                               DiscoverySource.XCODE, version, details + (("variant", "command_line_tools"),)))
    return records


def discover_app_bundles(probes: HostProbes, root: str = "/Applications") -> list[ToolRecord]:
    """Presence and plist version for well-known bundles (never launched)."""
    records: list[ToolRecord] = []
    seen: set[str] = set()
    for entry in probes.list_dir(root, MAX_APPLICATIONS):
        mapping = APP_BUNDLES.get(entry)
        if mapping is None:
            continue
        name, category = mapping
        if name in seen:
            continue
        bundle = f"{root.rstrip('/')}/{entry}"
        if not probes.is_dir(bundle):
            continue
        seen.add(name)
        version = _plist_version(probes, bundle)
        records.append(_record(probes, name, category, bundle, DiscoverySource.APP_BUNDLE,
                               version, (("bundle", entry),)))
    return records


__all__ = [
    "APP_BUNDLES",
    "CLT_PACKAGE",
    "discover_app_bundles",
    "discover_brew",
    "discover_xcode",
]
