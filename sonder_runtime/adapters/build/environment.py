"""Scrubbed build environments and the Visual Studio ``vcvars`` capture.

Every build job launches with a replacement environment
(``inherit_environment=False``): a small fixed base per host family, the
operator's ``SONDER_BUILD_ENV_PASSTHROUGH`` names minus a denylist of
code-injection, proxy and symbol-server variables, and (on Windows, only for
Ninja/NMake with ``cl`` or ``clang-cl`` and for include traces) the variables
``vcvars64.bat`` sets.

vcvars capture (F14):

* it runs ``<SystemRoot>\\System32\\cmd.exe /d /c <state>\\build-env\\sonder-vcvars.cmd``;
  the wrapper holds fixed bytes, is written once and is verified by sha256
  before every use (a tampered wrapper is refused, never rewritten);
* the batch path and its arguments travel in ``SONDER_VCVARS_BAT`` and
  ``SONDER_VCVARS_ARGS`` through an explicit environment that is the
  scrubbed Windows base plus those two keys -- never the operator's
  environment, whose secrets would come back through ``set``;
* the batch path comes from the host inventory's vswhere record, lies under
  that installation and matches a strict character set; the only argument is
  ``-vcvars_ver=<toolset>``;
* only allowlisted keys are kept, ``PATH`` entries outside the Visual Studio
  installation, the Windows SDK roots and the base ``PATH`` are dropped, and
  the result is cached (0600) per batch file identity, arch, toolset and
  vswhere snapshot.

Environment values never reach the wire; plans carry only key names.
"""
from __future__ import annotations

import errno
import hashlib
import json
import ntpath
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ...application.build.ports import ENV_CAPTURE_FAILED, BuildEnvironment, build_error
from ...platform.private_files import ensure_private_dir

ENV_DIR_NAME = "build-env"
WRAPPER_NAME = "sonder-vcvars.cmd"
CACHE_NAME = "vcvars-cache.json"
# The wrapper's exact bytes. ``/d`` on the cmd.exe command line skips AutoRun;
# the batch path and arguments arrive only through the two environment keys.
WRAPPER_BYTES = (
    b'@echo off & call "%SONDER_VCVARS_BAT%" %SONDER_VCVARS_ARGS% >nul 2>&1 || exit /b 1 & set\r\n'
)
WRAPPER_SHA256 = hashlib.sha256(WRAPPER_BYTES).hexdigest()
VCVARS_TIMEOUT_SECONDS = 30.0
VCVARS_MAX_OUTPUT_CHARS = 256 * 1024
MAX_CACHE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 8
MAX_VALUE_CHARS = 32_767
MAX_PATH_ENTRIES = 256

VCVARS_PATH_RE = re.compile(r'^[A-Za-z]:\\[^&|<>^%"\r\n]+\.bat$')
TOOLSET_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
_VCVARS_SUFFIX = "\\vc\\auxiliary\\build\\vcvars64.bat"

# Keys vcvars sets that a build needs. Everything else in ``set`` is dropped.
VCVARS_KEYS = frozenset(key.casefold() for key in (
    "PATH", "INCLUDE", "LIB", "LIBPATH", "EXTERNAL_INCLUDE",
    "VCINSTALLDIR", "VCIDEInstallDir", "VCToolsInstallDir", "VCToolsVersion", "VCToolsRedistDir",
    "VSINSTALLDIR", "VisualStudioVersion", "DevEnvDir", "Platform", "CommandPromptType",
    "VSCMD_ARG_HOST_ARCH", "VSCMD_ARG_TGT_ARCH", "VSCMD_ARG_app_plat", "VSCMD_VER",
    "WindowsSdkDir", "WindowsSDKVersion", "WindowsSdkBinPath", "WindowsSdkVerBinPath",
    "WindowsLibPath", "WindowsSDKLibVersion", "UniversalCRTSdkDir", "UCRTVersion",
    "ExtensionSdkDir", "FrameworkDir", "FrameworkDir64", "FrameworkVersion",
    "FrameworkVersion64", "NETFXSDKDir",
))

POSIX_BASE_KEYS = ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL")
POSIX_PINNED = (
    ("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8"), ("TERM", "dumb"), ("NO_COLOR", "1"),
    ("CLICOLOR", "0"), ("CMAKE_COLOR_DIAGNOSTICS", "OFF"), ("GCC_COLORS", ""),
    ("NINJA_STATUS", "[%f/%t] "),
)
WINDOWS_BASE_KEYS = (
    "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS", "USERPROFILE", "USERNAME", "USERDOMAIN",
    "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)",
    "CommonProgramW6432", "ALLUSERSPROFILE", "PUBLIC",
)
WINDOWS_PINNED = (
    ("VSLANG", "1033"), ("NO_COLOR", "1"), ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
    ("DOTNET_NOLOGO", "1"), ("MSBUILDDISABLENODEREUSE", "1"), ("VSCMD_SKIP_SENDTELEMETRY", "1"),
)

# Never passed through, whatever the operator lists: loader and interpreter
# injection, compiler search-path redirection, make flag injection, proxies
# (egress by inheritance), symbol servers and package feeds.
DENIED_EXACT = frozenset(name.casefold() for name in (
    "LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "PS4", "PROMPT_COMMAND",
    "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "PERL5OPT", "PERL5LIB", "RUBYOPT",
    "NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "GCC_EXEC_PREFIX", "COMPILER_PATH",
    "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "MAKEFLAGS", "MFLAGS",
    "GNUMAKEFLAGS", "MAKEFILES", "CL", "_CL_", "LINK", "_LINK_", "IFS", "SONDER_VCVARS_BAT",
    "SONDER_VCVARS_ARGS", "_NT_SYMBOL_PATH", "_NT_ALT_SYMBOL_PATH", "_NT_SOURCE_PATH",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY", "PATH", "COMSPEC",
))
DENIED_PREFIXES = ("dyld_", "symsrv", "nuget_", "sonder_", "git_", "ssh_")
DENIED_SUFFIXES = ("_proxy",)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_().]{0,127}$")


def passthrough_allowed(name: str) -> bool:
    """Whether an operator-listed name may be passed into a build."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return False
    folded = name.casefold()
    if folded in DENIED_EXACT:
        return False
    if folded.startswith(DENIED_PREFIXES) or folded.endswith(DENIED_SUFFIXES):
        return False
    return True


def _clean_value(value: object) -> str | None:
    if not isinstance(value, str) or "\x00" in value or len(value) > MAX_VALUE_CHARS:
        return None
    return value


def _secret_scrubbed(values: Mapping[str, str]) -> dict[str, str]:
    """Drop anything the runtime's child-environment policy treats as a secret."""
    from ...platform.logging import child_environment

    return child_environment(dict(values))


# ---------------------------------------------------------------------------
# JSON cache


class JsonEnvCache:
    """A small owner-only (0600) JSON map, written atomically, read no-follow."""

    def __init__(self, path: str, *, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self._path = Path(path)
        self._max_entries = max_entries
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self._path, flags)
        except OSError:
            return {}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
                return {}
            if os.name != "nt" and hasattr(os, "getuid") and (
                    info.st_uid != os.getuid() or info.st_mode & 0o077):
                return {}  # not ours or readable by others: never trusted
            raw = os.read(fd, MAX_CACHE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict[str, str] | None:
        with self._lock:
            entry = self._load().get(key)
        if not isinstance(entry, dict):
            return None
        values = entry.get("env")
        if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            return None
        return dict(values)

    def put(self, key: str, values: Mapping[str, str], *, stored_at: float) -> None:
        with self._lock:
            data = self._load()
            data[key] = {"env": dict(values), "stored_at": float(stored_at)}
            if len(data) > self._max_entries:
                ordered = sorted(data.items(), key=lambda item: _float(item[1].get("stored_at"))
                                 if isinstance(item[1], dict) else 0.0)
                data = dict(ordered[-self._max_entries:])
            payload = json.dumps(data, sort_keys=True).encode("utf-8")
            if len(payload) > MAX_CACHE_BYTES:
                return
            ensure_private_dir(self._path.parent)
            temporary = self._path.with_name(self._path.name + ".%d.tmp" % os.getpid())
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) \
                | getattr(os, "O_BINARY", 0)
            fd = os.open(temporary, flags, 0o600)
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(temporary, self._path)

    def clear(self) -> None:
        with self._lock:
            try:
                os.unlink(self._path)
            except OSError:
                pass


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# vcvars


def _details(record: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in (getattr(record, "details", ()) or ())}


def _win_norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _win_inside(child: str, parent: str) -> bool:
    child_n, parent_n = _win_norm(child), _win_norm(parent).rstrip("\\")
    return child_n == parent_n or child_n.startswith(parent_n + "\\")


class VcvarsCapture:
    """Capture the ``vcvars64.bat`` environment for one toolset and arch."""

    def __init__(self, state_dir: str, *, lookup, base_environment: Callable[[], Mapping[str, str]],
                 system_root: Callable[[], str], run: Callable[..., Any] | None = None,
                 cache: JsonEnvCache | None = None, snapshot_digest: Callable[[], str] = lambda: "",
                 stat_file: Callable[[str], os.stat_result] = os.stat,
                 clock: Callable[[], float] | None = None) -> None:
        self._dir = Path(state_dir) / ENV_DIR_NAME
        self._lookup = lookup
        self._base = base_environment
        self._system_root = system_root
        if run is None:
            from ..host_tools.bounded_process import run_bounded as run
        self._run = run
        self._cache = cache or JsonEnvCache(str(self._dir / CACHE_NAME))
        self._snapshot_digest = snapshot_digest
        self._stat = stat_file
        import time

        self._clock = clock or time.time
        self._lock = threading.Lock()

    @property
    def wrapper_path(self) -> Path:
        return self._dir / WRAPPER_NAME

    # -- wrapper ---------------------------------------------------------------

    def _ensure_wrapper(self) -> str:
        """Write the fixed wrapper once; verify its bytes before every use."""
        ensure_private_dir(self._dir)
        path = self.wrapper_path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) \
            | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o700)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise build_error(ENV_CAPTURE_FAILED, "the vcvars wrapper could not be written") from None
        else:
            try:
                os.write(fd, WRAPPER_BYTES)
            finally:
                os.close(fd)
        self._verify_wrapper(path)
        return str(path)

    @staticmethod
    def _verify_wrapper(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise build_error(ENV_CAPTURE_FAILED, "the vcvars wrapper is missing or a link") from None
        try:
            info = os.fstat(fd)
            data = os.read(fd, 4097) if stat.S_ISREG(info.st_mode) else b""
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != WRAPPER_SHA256:
            raise build_error(ENV_CAPTURE_FAILED, "the vcvars wrapper was modified; refusing to run it")

    # -- validation ------------------------------------------------------------

    def _vcvars_for(self, toolset: str) -> tuple[str, str, str]:
        """(vcvars path, installation root, toolset argument)."""
        record = self._lookup.lookup("cl")
        if record is None:
            raise build_error(ENV_CAPTURE_FAILED, "no Visual Studio C++ toolset is in the host inventory")
        details = _details(record)
        vcvars = details.get("vcvars64", "")
        if not VCVARS_PATH_RE.match(vcvars):
            raise build_error(ENV_CAPTURE_FAILED, "the vcvars64.bat path is missing or unsafe")
        if not vcvars.casefold().endswith(_VCVARS_SUFFIX):
            raise build_error(ENV_CAPTURE_FAILED, "vcvars64.bat is not at its Visual Studio location")
        root = vcvars[: len(vcvars) - len(_VCVARS_SUFFIX)]
        if not re.match(r"^[A-Za-z]:\\.+", root) or not _win_inside(str(record.path), root):
            raise build_error(ENV_CAPTURE_FAILED, "vcvars64.bat lies outside the compiler's installation")
        wanted = toolset or ""
        if wanted and not TOOLSET_RE.match(wanted):
            raise build_error(ENV_CAPTURE_FAILED, "the requested toolset is not a version")
        arguments = ("-vcvars_ver=" + wanted) if wanted else ""
        return vcvars, root, arguments

    def _key(self, vcvars: str, arch: str, toolset: str) -> str:
        try:
            info = self._stat(vcvars)
            identity = "%d:%d" % (int(info.st_size), int(getattr(info, "st_mtime_ns", 0) or info.st_mtime))
        except OSError:
            raise build_error(ENV_CAPTURE_FAILED, "vcvars64.bat is not readable") from None
        material = "\x1f".join((_win_norm(vcvars), identity, arch, toolset, self._snapshot_digest() or ""))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # -- capture ---------------------------------------------------------------

    def capture(self, *, family: str = "msvc", arch: str = "x64",
                toolset: str = "") -> tuple[dict[str, str], bool]:
        """(allowlisted vcvars variables, cache_hit)."""
        if arch != "x64":
            raise build_error(ENV_CAPTURE_FAILED, "only x64 vcvars capture is supported in v1")
        del family  # msvc and clang_cl share the MSVC environment
        vcvars, root, arguments = self._vcvars_for(toolset)
        key = self._key(vcvars, arch, toolset)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                # The cache file is private, but a cached entry is still
                # re-filtered: only allowlisted keys and in-installation PATH
                # entries may ever reach a build, whatever the file holds.
                refiltered = self._refilter(cached, root)
                if refiltered is not None:
                    return refiltered, True
            wrapper = self._ensure_wrapper()
            system_root = self._system_root()
            if not re.match(r"^[A-Za-z]:\\[^&|<>^%\"\r\n]+$", system_root or ""):
                raise build_error(ENV_CAPTURE_FAILED, "SystemRoot is not a usable directory")
            base = {str(k): str(v) for k, v in dict(self._base()).items()}
            environment = dict(base)
            environment["SONDER_VCVARS_BAT"] = vcvars
            environment["SONDER_VCVARS_ARGS"] = arguments
            argv = (ntpath.join(system_root, "System32", "cmd.exe"), "/d", "/c", wrapper)
            result = self._run(argv, timeout_seconds=VCVARS_TIMEOUT_SECONDS,
                               max_output_chars=VCVARS_MAX_OUTPUT_CHARS, env=environment,
                               cwd=system_root)
            if getattr(result, "outcome", "") != "ok":
                raise build_error(ENV_CAPTURE_FAILED, "vcvars64.bat failed (%s)"
                                  % getattr(result, "outcome", "error"))
            values = self._parse(str(getattr(result, "output", "") or ""), root, base)
            if "path" not in {key.casefold() for key in values} or not any(
                    key.casefold() == "include" for key in values):
                raise build_error(ENV_CAPTURE_FAILED, "vcvars64.bat did not set PATH and INCLUDE")
            self._cache.put(key, values, stored_at=self._clock())
            self._verify_wrapper(Path(wrapper))
            return values, False

    def _refilter(self, cached: Mapping[str, str], root: str) -> dict[str, str] | None:
        lines = "\n".join("%s=%s" % (name, value) for name, value in cached.items()
                          if isinstance(name, str) and isinstance(value, str)
                          and "\n" not in name and "\n" not in value and "\r" not in value)
        values = self._parse(lines, root, dict(self._base()))
        folded = {name.casefold() for name in values}
        if "path" not in folded or "include" not in folded:
            return None
        return values

    @staticmethod
    def _parse(output: str, root: str, base: Mapping[str, str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for line in output.splitlines():
            name, sep, value = line.partition("=")
            if not sep or not name or name.startswith(" "):
                continue
            if name.casefold() not in VCVARS_KEYS:
                continue
            cleaned = _clean_value(value.rstrip("\r"))
            if cleaned is None:
                continue
            found[name] = cleaned
        sdk_roots = [value for name, value in found.items()
                     if name.casefold() in {"windowssdkdir", "universalcrtsdkdir", "netfxsdkdir"}
                     and re.match(r"^[A-Za-z]:\\", value)]
        base_path = next((value for name, value in base.items() if name.casefold() == "path"), "")
        base_entries = {_win_norm(entry) for entry in base_path.split(";") if entry}
        allowed_roots = [root, *sdk_roots]
        for name in list(found):
            if name.casefold() != "path":
                continue
            kept: list[str] = []
            for entry in found[name].split(";"):
                entry = entry.strip()
                if not entry or not re.match(r"^[A-Za-z]:\\", entry):
                    continue
                if _win_norm(entry) in base_entries or any(_win_inside(entry, item) for item in allowed_roots):
                    if entry not in kept:
                        kept.append(entry)
                if len(kept) >= MAX_PATH_ENTRIES:
                    break
            found[name] = ";".join(kept)
        return found


# ---------------------------------------------------------------------------
# Provider


def _posix_path_entries(value: str, *, project_local: Callable[[str], bool]) -> list[str]:
    kept: list[str] = []
    for entry in (value or "").split(":"):
        if not entry or not entry.startswith("/") or "\x00" in entry:
            continue
        normal = os.path.normpath(entry)
        try:
            info = os.stat(normal)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if info.st_mode & 0o002 and not info.st_mode & stat.S_ISVTX:
            continue  # world-writable PATH directory: anyone could plant a compiler
        if project_local(normal):
            continue
        if normal not in kept:
            kept.append(normal)
        if len(kept) >= MAX_PATH_ENTRIES:
            break
    return kept


def _windows_path_entries(value: str, system_root: str,
                          project_local: Callable[[str], bool] | None = None) -> list[str]:
    kept: list[str] = []
    if system_root:
        kept.extend((ntpath.join(system_root, "System32"), system_root,
                     ntpath.join(system_root, "System32", "Wbem")))
    seen = {_win_norm(entry) for entry in kept}
    for entry in (value or "").split(";"):
        entry = entry.strip().strip('"')
        if not entry or not re.match(r"^[A-Za-z]:\\", entry) or "%" in entry:
            continue
        if _win_norm(entry) in seen:
            continue
        if project_local is not None and project_local(entry):
            continue  # a checkout directory on PATH could plant cl.exe or link.exe
        seen.add(_win_norm(entry))
        kept.append(entry)
        if len(kept) >= MAX_PATH_ENTRIES:
            break
    return kept


class ScrubbedEnvironmentProvider:
    """``BuildEnvironmentProvider`` over a host environment snapshot."""

    def __init__(self, *, host: str | None = None,
                 source: Callable[[], Mapping[str, str]] | None = None,
                 passthrough: Iterable[str] = (),
                 vcvars: VcvarsCapture | None = None,
                 project_local: Callable[[str], bool] | None = None) -> None:
        self._host = host or ("windows" if os.name == "nt" else "posix")
        self._source = source or (lambda: dict(os.environ))
        self._passthrough = tuple(name for name in dict.fromkeys(passthrough) if passthrough_allowed(name))
        self._vcvars = vcvars
        # A Windows-shaped PATH is only checked against the project roots on
        # Windows itself (or with an injected check): off Windows the default
        # guard would read ``C:\\...`` as a path relative to the cwd.
        self._windows_project_local = project_local if project_local is not None or os.name == "nt" \
            else None
        if project_local is None:
            from ..host_tools.guards import project_local
        self._project_local = project_local

    def use_vcvars(self, capture: VcvarsCapture | None) -> None:
        """Attach the capture after construction (it needs ``self.base``)."""
        self._vcvars = capture

    @property
    def passthrough(self) -> tuple[str, ...]:
        return self._passthrough

    def _lookup(self, source: Mapping[str, str], key: str) -> str | None:
        if self._host != "windows":
            return source.get(key)
        folded = key.casefold()
        for name, value in source.items():
            if name.casefold() == folded:
                return value
        return None

    def base(self) -> dict[str, str]:
        """The fixed base for this host family (no passthrough, no vcvars)."""
        source = dict(self._source())
        environment: dict[str, str] = {}
        if self._host == "windows":
            for key in WINDOWS_BASE_KEYS:
                value = _clean_value(self._lookup(source, key))
                if value is not None and "\n" not in value:
                    environment[key] = value
            system_root = environment.get("SystemRoot", "")
            entries = _windows_path_entries(self._lookup(source, "PATH") or "", system_root,
                                            self._windows_project_local)
            environment["PATH"] = ";".join(entries)
            environment.update(WINDOWS_PINNED)
        else:
            for key in POSIX_BASE_KEYS:
                value = _clean_value(source.get(key))
                if value is not None and "\n" not in value:
                    environment[key] = value
            entries = _posix_path_entries(source.get("PATH", ""), project_local=self._project_local)
            environment["PATH"] = ":".join(entries or ["/usr/local/bin", "/usr/bin", "/bin"])
            environment.update(POSIX_PINNED)
        return environment

    def environment(self, *, system: str, family: str, toolchain_hint: str = "",
                    arch: str = "x64") -> BuildEnvironment:
        environment = self.base()
        notes: list[str] = []
        source = dict(self._source())
        passed = {}
        for name in self._passthrough:
            value = _clean_value(self._lookup(source, name))
            if value is not None:
                passed[name] = value
        passed = _secret_scrubbed(passed)
        environment.update(passed)
        kind = "scrubbed"
        cache_hit = False
        if self._host == "windows" and family in ("msvc", "clang_cl"):
            if self._vcvars is None:
                raise build_error(ENV_CAPTURE_FAILED, "vcvars capture is not configured on this host")
            toolset = toolchain_hint if TOOLSET_RE.match(toolchain_hint or "") else ""
            values, cache_hit = self._vcvars.capture(family=family, arch=arch, toolset=toolset)
            for name, value in values.items():
                for existing in [key for key in environment if key.casefold() == name.casefold()]:
                    environment.pop(existing, None)
                environment[name] = value
            kind = "vcvars"
            notes.append("vcvars environment %s" % ("reused from cache" if cache_hit else "captured"))
        elif family in ("msvc", "clang_cl") and self._host != "windows":
            notes.append("vcvars capture needs Windows; using the scrubbed environment")
        del system
        pairs = tuple(sorted(environment.items()))
        return BuildEnvironment(pairs=pairs, keys=tuple(key for key, _ in pairs), source=kind,
                                cache_hit=cache_hit, notes=tuple(notes))


__all__ = [
    "CACHE_NAME", "DENIED_EXACT", "ENV_DIR_NAME", "JsonEnvCache", "POSIX_PINNED",
    "ScrubbedEnvironmentProvider", "VCVARS_KEYS", "VCVARS_PATH_RE", "VcvarsCapture",
    "WINDOWS_PINNED", "WRAPPER_BYTES", "WRAPPER_NAME", "WRAPPER_SHA256", "passthrough_allowed",
]
