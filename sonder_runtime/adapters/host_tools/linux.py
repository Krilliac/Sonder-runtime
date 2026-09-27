"""Linux tool directories commonly missing from a service or agent PATH.

Only directory listings (bounded) and existence checks happen here; nothing
is executed.
"""
from __future__ import annotations

import re

from .probes import HostProbes

MAX_OPT = 64
MAX_NVM = 16
MAX_SDKMAN = 32
MAX_JVM = 16
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9@+._-]{1,128}$")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name)[:6])


def _children(probes: HostProbes, parent: str, limit: int, *, newest_first: bool = False) -> list[str]:
    names = [name for name in probes.list_dir(parent, 512) if _SAFE_ENTRY.match(name)]
    if newest_first:
        names.sort(key=_version_key, reverse=True)
    return names[:limit]


def known_prefix_dirs(probes: HostProbes) -> list[str]:
    """Existing well-known tool directories, most specific first."""
    home = probes.home.rstrip("/") if probes.home and probes.home.startswith("/") else ""
    dirs: list[str] = []
    if home:
        dirs += [
            f"{home}/.local/bin",
            f"{home}/.cargo/bin",
            f"{home}/go/bin",
        ]
    dirs.append("/usr/local/go/bin")
    if home:
        dirs += [f"{home}/.dotnet", f"{home}/.dotnet/tools"]
    dirs += ["/snap/bin", "/var/lib/flatpak/exports/bin"]
    if home:
        dirs.append(f"{home}/.local/share/flatpak/exports/bin")
    for name in _children(probes, "/opt", MAX_OPT):
        dirs.append(f"/opt/{name}/bin")
    if home:
        for name in _children(probes, f"{home}/.nvm/versions/node", MAX_NVM, newest_first=True):
            dirs.append(f"{home}/.nvm/versions/node/{name}/bin")
        dirs.append(f"{home}/.pyenv/shims")
        for name in _children(probes, f"{home}/.sdkman/candidates", MAX_SDKMAN):
            dirs.append(f"{home}/.sdkman/candidates/{name}/current/bin")
    for name in _children(probes, "/usr/lib/jvm", MAX_JVM, newest_first=True):
        dirs.append(f"/usr/lib/jvm/{name}/bin")
    dirs.append("/home/linuxbrew/.linuxbrew/bin")
    if home:
        dirs.append(f"{home}/.local/share/JetBrains/Toolbox/scripts")
    seen: set[str] = set()
    existing: list[str] = []
    for item in dirs:
        if item in seen:
            continue
        seen.add(item)
        if probes.is_dir(item):
            existing.append(item)
    return existing


__all__ = ["known_prefix_dirs"]
