"""Choose engines and build host-owned step plans for crash and profile digests.

Engine selection is ``capture kind x platform x inventory``. Every argv comes
from ``domain.debugging.templates`` with ``{nonce}``/``{rundir}``
placeholders; the launcher binds them at start. The planner refuses before
anyone is asked to approve:

- engines that do not run on this platform (``ENGINE_UNSUPPORTED_ON_PLATFORM``)
  or are not in the host inventory (``ENGINE_UNAVAILABLE``);
- cdb on a managed dump (clr/coreclr/mscorwks in the module list): dbgeng
  would load the DAC from the symbol path, which is code execution
  (``ENGINE_REFUSED_MANAGED_DUMP``); the Store WinDbg cdb under WindowsApps;
- a debugger that needs the executable when none was given
  (``EXECUTABLE_REQUIRED``);
- symbol dirs failing the lexical or containment rules
  (``SYMBOL_PATH_REJECTED``) and malformed operator stores
  (``SYMBOL_STORE_REJECTED``).

llvm-symbolizer and dump_syms are planned only for modules whose PE and PDB
identities verify in pure Python first (GUID and age, ``pdb_matches``); the
verified pair is staged side by side in the run directory so the symbolizer's
exe-directory lookup always wins over the binary-controlled RSDS path. A
mismatched PDB gives ``symbols=mismatch`` and no step.

Operator symbol stores come only from platform consent (``SONDER_SYMBOL_STORES``)
and only when the service has already established network consent. Linux
steps without network run inside a no-network user namespace whenever the
inventory has ``unshare`` and the one-time probe passed.
"""
from __future__ import annotations

import os
import platform as _platform
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from ...application.context import OperationContext
from ...application.debugging.ports import (
    CRASH_ENGINES,
    ENGINE_REFUSED_MANAGED_DUMP,
    ENGINE_UNAVAILABLE,
    ENGINE_UNSUPPORTED_ON_PLATFORM,
    EXECUTABLE_REQUIRED,
    MINIDUMP_KINDS,
    PDB_MISMATCH,
    PROFILE_ENGINES,
    PURE_PROFILE_KINDS,
    SYMBOL_STORE_REJECTED,
    CaptureIdentity,
    CrashDigestRequest,
    DebugPlan,
    DebugStep,
    ProfileDigestRequest,
    debug_error,
)
from ...domain.binaries.pdb_info import pdb_matches, read_pdb_identity
from ...domain.binaries.pe_debug import read_pe_identity
from ...domain.binaries.reader import BinaryFormatError
from ...domain.binaries.symstore import breakpad_debug_id
from ...domain.common.errors import InvalidInput, SonderError
from ...domain.debugging import templates as T
from ...domain.debugging.plan_digest import debug_command_digest
from ...domain.debugging.symbol_path import (
    DEBUGINFOD_BY_DISTRO,
    SymbolStoreRejected,
    build_cdb_symbol_path,
    lexical_store,
    store_display,
)
from ..host_tools.guards import is_windows_apps_alias
from .symbol_dirs import contain_symbol_dirs

GIB = 1 << 30
MAX_SYMBOLIZER_STEPS = 3
MAX_DUMP_SYMS_MODULES = 16
PROFILER_MEMORY_BYTES = 2 * GIB
WINDOWS_DEBUGGER_MEMORY_BYTES = 4 * GIB
MIN_TIMEOUT = 10
MAX_TIMEOUT = 900
MANAGED_RUNTIME_MODULES = frozenset({"clr.dll", "coreclr.dll", "mscorwks.dll"})
WPA_PROFILE_NAME = "cpu_sampled.wpaProfile"
PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

# engine -> host inventory name
TOOL_NAMES = {
    "cdb": "cdb", "gdb": "gdb", "lldb": "lldb", "eu_stack": "eu-stack",
    "minidump_stackwalk": "minidump-stackwalk", "dump_syms": "dump_syms",
    "llvm_symbolizer": "llvm-symbolizer", "unshare": "unshare", "perf": "perf",
    "heaptrack_print": "heaptrack_print", "tracy_csvexport": "tracy-csvexport",
    "xperf": "xperf", "wpaexporter": "wpaexporter",
}
INSTALL_HINTS = {
    "eu_stack": "install elfutils (apt-get install elfutils)",
    "minidump_stackwalk": "cargo install minidump-stackwalk dump_syms",
    "perf": "install linux-tools for this kernel; capture with `perf record -e cpu-clock -g`",
    "heaptrack_print": "install heaptrack (apt-get install heaptrack)",
    "llvm_symbolizer": "install LLVM (llvm-symbolizer)",
    "cdb": "install the Windows SDK Debugging Tools",
    "tracy_csvexport": "build tracy-csvexport matching the capture's Tracy version",
}
# Which engines read which crash capture kinds, per platform.
_CORE_ENGINES = {
    "Linux": ("gdb", "lldb", "eu_stack"),
    "Darwin": ("lldb",),
    "Windows": (),
}
_LINUX_ONLY = frozenset({"gdb", "eu_stack", "perf", "heaptrack_print"})


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def posix_debugger_memory(input_bytes: int) -> int:
    """RLIMIT_AS counts the mmapped core: ``clamp(input + 3 GiB, 4 GiB, 24 GiB)``."""
    return _clamp(int(input_bytes) + 3 * GIB, 4 * GIB, 24 * GIB)


def _platform_family(system: str) -> str:
    lowered = str(system or "").lower()
    if lowered.startswith("win"):
        return "Windows"
    if lowered.startswith("darwin") or lowered.startswith("mac"):
        return "Darwin"
    return "Linux"


def _basename(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


class _Build:
    """Mutable plan state while one plan is assembled."""

    def __init__(self) -> None:
        self.steps: list[DebugStep] = []
        self.templates: list[tuple[str, ...]] = []
        self.engines: list[str] = []
        self.tools: list[str] = []
        self.checked: list[str] = []
        self.notes: list[str] = []
        self.bindings: dict[str, str] = {}
        self.staged: list[tuple[str, str]] = []
        self.mkdirs: list[str] = []
        self.verified: list[str] = []
        self.module_symbols: list[tuple[str, str]] = []
        self.identities: list[str] = []

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def mkdir(self, rel: str) -> None:
        if rel not in self.mkdirs:
            self.mkdirs.append(rel)


class HostDebugPlanner:
    """``DebugPlanner`` over the host tool inventory and the lane A templates."""

    def __init__(self, lookup, *, redact: Callable[[str], str], system: str | None = None,
                 isolation_probe: Callable[[str], bool] | None = None,
                 source: Any = None, state_dir: str = "",
                 stores: Callable[[], Iterable[str]] | None = None,
                 drive_type: Callable[[str], int] | None = None,
                 distro: str = "", system_root: str = "",
                 profiles_dir: Path | None = None) -> None:
        self._lookup = lookup
        self._redact = redact
        self._system = _platform_family(system or _platform.system())
        self._isolation_probe = isolation_probe
        self._source = source
        self._state_dir = state_dir
        self._stores = stores or (lambda: ())
        self._drive_type = drive_type
        self._distro = str(distro or "").lower()
        self._system_root = system_root or "C:\\Windows"
        self._profiles_dir = profiles_dir or PROFILES_DIR
        self._host_family = _platform_family(_platform.system())

    @property
    def system(self) -> str:
        return self._system

    # -- shared --------------------------------------------------------------

    def _tool(self, engine: str, *, required: bool):
        name = TOOL_NAMES[engine]
        record = None
        try:
            record = self._lookup.lookup(name)
        except Exception:
            record = None
        if record is not None and is_windows_apps_alias(getattr(record, "path", "")):
            if required:
                raise debug_error(ENGINE_UNAVAILABLE,
                                  "%s under WindowsApps (Store WinDbg) is refused; install the "
                                  "SDK Debugging Tools" % name)
            return None
        if record is None and required:
            hint = INSTALL_HINTS.get(engine, "")
            raise debug_error(ENGINE_UNAVAILABLE, "%s is not in the host tool inventory%s"
                              % (name, ("; " + hint) if hint else ""))
        return record

    def _timeout(self, requested, default: int) -> int:
        if requested is None:
            return _clamp(default, MIN_TIMEOUT, MAX_TIMEOUT)
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise InvalidInput("timeout_seconds must be an integer")
        return _clamp(requested, MIN_TIMEOUT, MAX_TIMEOUT)

    def _symbol_dirs(self, dirs) -> tuple[str, ...]:
        return contain_symbol_dirs(tuple(dirs or ()), system=self._system,
                                   drive_type=self._drive_type,
                                   exists=self._system == self._host_family)

    def _executable(self, path: str) -> str:
        if not path:
            return ""
        if self._source is None:
            raise debug_error(EXECUTABLE_REQUIRED, "executables cannot be verified here")
        return self._source.contained_file(path)

    def _stat_identity(self, path: str) -> str:
        try:
            info = os.stat(path)
        except OSError:
            return path
        return "%s|%d|%d" % (path, info.st_size, info.st_mtime_ns)

    def _environment(self, template, *, network: bool, tool_path: str,
                     symbol_path: str = "") -> tuple[tuple[str, str], ...]:
        if self._system == "Windows":
            tool_dir = str(PureWindowsPath(tool_path).parent)
            base = T.windows_environment(self._system_root, tool_dir, symbol_path=symbol_path,
                                         with_symcache=template.engine in ("xperf", "wpaexporter"))
        else:
            url = DEBUGINFOD_BY_DISTRO.get(self._distro, "") if network else ""
            base = T.posix_environment(network=network, debuginfod_url=url)
        merged = dict(base)
        merged.update(dict(template.env))
        return tuple(merged.items())

    def _isolate(self, template, build: _Build, *, network: bool):
        if network or self._system != "Linux":
            return template, "none"
        unshare = self._tool("unshare", required=False)
        if unshare is None or self._isolation_probe is None:
            return template, "none"
        try:
            available = bool(self._isolation_probe(unshare.path))
        except Exception:
            available = False
        if not available:
            return template, "none"
        if unshare.path not in build.checked:
            build.checked.append(unshare.path)
        return T.with_netns(template, unshare.path), "netns"

    def _add_step(self, build: _Build, template, record, *, network: bool, timeout: int,
                  memory: int, symbol_path: str = "") -> None:
        environment = self._environment(template, network=network, tool_path=record.path,
                                        symbol_path=symbol_path)
        template, isolation = self._isolate(template, build, network=network)
        display = tuple(self._redact(item) for item in template.display_argv)
        build.steps.append(DebugStep(
            engine=template.engine, template_argv=tuple(template.argv), display_argv=display,
            environment=environment, timeout_seconds=timeout,
            max_output_bytes=int(template.max_output_bytes), memory_limit_bytes=int(memory),
            parser=template.parser, isolation=isolation,
            reads_output_via=str(template.reads_capture_via or "argv"),
        ))
        build.templates.append(tuple(template.argv))
        if template.engine not in build.engines:
            build.engines.append(template.engine)
        identity = "%s:%s:%s" % (template.engine, record.path,
                                 getattr(record, "identity", "") or getattr(record, "version", ""))
        if identity not in build.tools:
            build.tools.append(identity)
        if record.path not in build.checked:
            build.checked.append(record.path)

    def _finish(self, build: _Build, *, kind: str, identity: CaptureIdentity, network: bool,
                stores: tuple[str, ...]) -> DebugPlan:
        engines = ["pure"] + [engine for engine in build.engines if engine != "pure"]
        digest = debug_command_digest(
            build.templates, tuple(build.tools) + tuple(build.identities), identity.sha256,
            engines, network, stores)
        isolations = {step.isolation for step in build.steps}
        egress = "n/a" if not build.steps else ("netns" if isolations == {"netns"} else "none")
        staging = "path"
        if build.steps:
            staging = self._staging(identity)
        return DebugPlan(
            kind=kind, source_kind=identity.kind, input_label=identity.label,
            input_sha256=identity.sha256, input_bytes=identity.size, input_identity=identity,
            staging=staging, steps=tuple(build.steps), network=bool(network),
            stores_display=tuple(store_display(store) for store in stores),
            verified_modules=tuple(build.verified), checked_executables=tuple(build.checked),
            notes=tuple(build.notes), command_digest=digest,
            bindings=tuple(sorted(build.bindings.items())), staged_files=tuple(build.staged),
            mkdirs=tuple(build.mkdirs), module_symbols=tuple(build.module_symbols),
            egress_isolation=egress, engines=tuple(engines),
        )

    def _staging(self, identity: CaptureIdentity) -> str:
        from .capture_source import COPY_STAGING_MAX_BYTES

        if identity.size <= COPY_STAGING_MAX_BYTES:
            return "copy"
        if self._system != "Windows" and self._state_dir:
            try:
                if os.stat(self._state_dir).st_dev == identity.dev:
                    return "hardlink"
            except OSError:
                pass
        return "path"

    # -- crash -----------------------------------------------------------------

    def plan_crash(self, request: CrashDigestRequest, context: OperationContext, *,
                   network_allowed: bool, identity: CaptureIdentity, tier0=None) -> DebugPlan:
        del context
        engine = str(request.engine or "auto")
        if engine not in CRASH_ENGINES:
            raise InvalidInput("engine must be one of %s" % ", ".join(CRASH_ENGINES))
        network = bool(network_allowed and request.symbol_server)
        stores: tuple[str, ...] = ()
        if network:
            try:
                stores = tuple(lexical_store(item, system=self._system)
                               for item in tuple(self._stores())[:4])
            except SymbolStoreRejected as exc:
                raise debug_error(SYMBOL_STORE_REJECTED, str(exc)) from None
        dirs = self._symbol_dirs(request.symbol_dirs)
        build = _Build()
        build.identities.extend("sym:%s" % item for item in dirs)
        timeout_request = request.timeout_seconds
        kind = identity.kind
        if engine == "pure":
            return self._finish(build, kind="crash", identity=identity, network=network, stores=stores)
        if kind in MINIDUMP_KINDS:
            self._plan_minidump(build, engine, request, identity, tier0, dirs, network, stores,
                                timeout_request)
        elif kind == "elf_core":
            self._plan_core(build, engine, request, identity, tier0, dirs, network, timeout_request)
        else:
            if engine != "auto":
                raise debug_error(ENGINE_UNAVAILABLE,
                                  "%s does not read %s captures (pure reader only)" % (engine, kind))
        return self._finish(build, kind="crash", identity=identity, network=network, stores=stores)

    def _managed(self, tier0) -> bool:
        if tier0 is None:
            return True  # unverifiable: treat as managed for cdb
        return any(getattr(module, "managed_runtime", False)
                   or str(getattr(module, "name", "")).lower() in MANAGED_RUNTIME_MODULES
                   for module in getattr(tier0, "modules", ()))

    def _plan_minidump(self, build, engine, request, identity, tier0, dirs, network, stores,
                       timeout_request) -> None:
        windows = self._system == "Windows"
        if engine in ("gdb", "lldb", "eu_stack"):
            raise debug_error(ENGINE_UNAVAILABLE, "%s does not read minidumps" % engine)
        if engine == "cdb" and not windows:
            raise debug_error(ENGINE_UNSUPPORTED_ON_PLATFORM, "cdb runs only on Windows")
        order = [engine] if engine != "auto" else (
            ["cdb", "minidump_stackwalk", "llvm_symbolizer"] if windows
            else ["minidump_stackwalk", "llvm_symbolizer"])
        for candidate in order:
            explicit = engine != "auto"
            if candidate == "cdb":
                if self._managed(tier0):
                    if explicit:
                        raise debug_error(
                            ENGINE_REFUSED_MANAGED_DUMP,
                            "cdb is refused for managed dumps (clr/coreclr/mscorwks): dbgeng "
                            "would load the DAC from the symbol path")
                    build.note("cdb skipped: managed (or unverifiable) dump")
                    continue
                record = self._tool("cdb", required=explicit)
                if record is None:
                    continue
                self._add_cdb(build, record, identity, dirs, network, stores, timeout_request)
                return
            if candidate == "minidump_stackwalk":
                record = self._tool("minidump_stackwalk", required=explicit)
                if record is None:
                    continue
                self._add_stackwalk(build, record, identity, tier0, dirs, timeout_request)
                return
            if candidate == "llvm_symbolizer":
                record = self._tool("llvm_symbolizer", required=explicit)
                if record is None:
                    continue
                if self._add_pe_symbolizer(build, record, identity, tier0, dirs, timeout_request):
                    return
                if explicit:
                    build.note("llvm_symbolizer: no module with a verified PE+PDB pair; no step")
                    return
        if engine == "auto" and not build.steps:
            build.note("no Tier-1 engine applies; the pure reader's result is returned")

    def _add_cdb(self, build, record, identity, dirs, network, stores, timeout_request) -> None:
        template = T.cdb_template(record.path, network=network)
        if network:
            cache = str(Path(self._state_dir) / "debug-symcache") if self._state_dir else "{rundir}\\symcache"
        else:
            cache = "{rundir}\\symcache"
        build.mkdir("symcache")
        build.mkdir("img")
        build.bindings["sympath"] = build_cdb_symbol_path(cache, dirs, stores, network=network)
        build.bindings["imagepath"] = ";".join(dirs) if dirs else "{rundir}\\img"
        timeout = self._timeout(timeout_request, template.timeout_default)
        self._add_step(build, template, record, network=network, timeout=timeout,
                       memory=WINDOWS_DEBUGGER_MEMORY_BYTES)

    # -- verified symbol files -------------------------------------------------

    def _find_in_dirs(self, dirs, name: str) -> str | None:
        wanted = name.lower()
        for directory in dirs:
            try:
                with os.scandir(directory) as iterator:
                    for index, entry in enumerate(iterator):
                        if index >= 4096:
                            break
                        if entry.name.lower() == wanted and entry.is_file(follow_symlinks=False):
                            return os.path.join(directory, entry.name)
            except OSError:
                continue
        return None

    def _read_identity(self, path: str, reader_function):
        if self._source is None:
            return None
        try:
            reader, _identity = self._source.open_reader(path, max_bytes=4 * GIB)
        except SonderError:
            return None
        try:
            return reader_function(reader)
        except (BinaryFormatError, InvalidInput, ValueError):
            return None
        finally:
            try:
                reader.close()
            except SonderError:
                pass

    def _verified_pe_modules(self, build, tier0, dirs, limit: int):
        """[(module, image_path, pdb_path, pe_identity)] whose PE and PDB verify."""
        out = []
        if tier0 is None:
            return out
        modules = sorted(getattr(tier0, "modules", ()), key=lambda m: not m.in_project)
        for module in modules:
            if len(out) >= limit:
                break
            if not module.debug_id or not module.debug_file:
                continue
            name = _basename(module.name)
            try:
                T._stem(name)
                T._stem(_basename(module.debug_file))
            except T.TemplateError:
                build.note("%s: module name is not a plain file name; skipped" % name)
                continue
            image = self._find_in_dirs(dirs, name)
            pdb_path = self._find_in_dirs(dirs, _basename(module.debug_file))
            if image is None or pdb_path is None:
                continue
            pe = self._read_identity(image, read_pe_identity)
            pdb = self._read_identity(pdb_path, read_pdb_identity)
            if pe is None or pdb is None or not pe.rsds_guid or pe.rsds_age is None:
                continue
            try:
                image_id = breakpad_debug_id(pe.rsds_guid, pe.rsds_age)
            except InvalidInput:
                continue
            if image_id.upper() != module.debug_id.upper():
                build.module_symbols.append((module.name, "mismatch"))
                build.note("%s: %s image in the symbol dirs is from another build" % (PDB_MISMATCH, name))
                continue
            if not pdb_matches(pe, pdb):
                build.module_symbols.append((module.name, "mismatch"))
                build.note("%s: %s does not match %s (GUID/age)" % (PDB_MISMATCH,
                                                                     _basename(pdb_path), name))
                continue
            out.append((module, image, pdb_path, pe))
            build.verified.append(module.name)
            build.identities.append("verified:%s:%s" % (module.name, module.debug_id))
        return out

    def _stage_pair(self, build, module, image, pdb_path, pdb_name: str | None = None) -> str:
        name = _basename(module.name)
        stem = _stem(name)
        build.mkdir("sym/%s" % stem)
        build.staged.append((image, "sym/%s/%s" % (stem, name)))
        build.staged.append((pdb_path, "sym/%s/%s" % (stem, pdb_name or _basename(pdb_path))))
        build.identities.append("stage:%s" % self._stat_identity(image))
        build.identities.append("stage:%s" % self._stat_identity(pdb_path))
        return name

    @staticmethod
    def _addresses(tier0, module_name: str) -> list[int]:
        values: list[int] = []
        wanted = module_name.lower()
        for thread in getattr(tier0, "threads", ()):
            for frame in thread.frames:
                if (frame.module or "").lower() == wanted and frame.module_offset is not None:
                    if frame.module_offset not in values:
                        values.append(int(frame.module_offset))
                if len(values) >= T.MAX_SYMBOLIZER_ADDRESSES:
                    return values
        return values

    def _add_pe_symbolizer(self, build, record, identity, tier0, dirs, timeout_request) -> bool:
        added = False
        for module, image, pdb_path, _pe in self._verified_pe_modules(build, tier0, dirs,
                                                                       MAX_SYMBOLIZER_STEPS * 4):
            if len([s for s in build.steps if s.engine == "llvm_symbolizer"]) >= MAX_SYMBOLIZER_STEPS:
                break
            addresses = self._addresses(tier0, module.name)
            if not addresses:
                continue
            name = self._stage_pair(build, module, image, pdb_path)
            template = T.symbolizer_template(record.path, name, addresses)
            timeout = self._timeout(timeout_request, template.timeout_default)
            self._add_step(build, template, record, network=False, timeout=timeout,
                           memory=WINDOWS_DEBUGGER_MEMORY_BYTES if self._system == "Windows"
                           else posix_debugger_memory(0))
            added = True
        return added

    def _add_stackwalk(self, build, record, identity, tier0, dirs, timeout_request) -> None:
        breakpad_cpp = _basename(record.path).lower().startswith("minidump_stackwalk")
        dump_syms = self._tool("dump_syms", required=False)
        build.mkdir("syms")
        if dump_syms is not None and not breakpad_cpp:
            for module, image, pdb_path, pe in self._verified_pe_modules(build, tier0, dirs,
                                                                          MAX_DUMP_SYMS_MODULES):
                name = _basename(module.name)
                stem = _stem(name)
                if _basename(pdb_path).lower() != (stem + ".pdb").lower():
                    build.note("%s: PDB name differs from the module; dump_syms skipped" % name)
                    continue
                debug_id = breakpad_debug_id(pe.rsds_guid, pe.rsds_age)
                self._stage_pair(build, module, image, pdb_path, pdb_name=stem + ".pdb")
                build.mkdir("syms/%s.pdb/%s" % (stem, debug_id))
                template = T.dump_syms_template(dump_syms.path, stem, debug_id)
                timeout = self._timeout(timeout_request, template.timeout_default)
                self._add_step(build, template, dump_syms, network=False, timeout=timeout,
                               memory=posix_debugger_memory(0))
        elif dump_syms is None:
            build.note("dump_syms is not installed; minidump-stackwalk walks without symbols")
        template = T.stackwalk_template(record.path, breakpad_cpp=breakpad_cpp)
        timeout = self._timeout(timeout_request, template.timeout_default)
        memory = (WINDOWS_DEBUGGER_MEMORY_BYTES if self._system == "Windows"
                  else posix_debugger_memory(identity.size))
        self._add_step(build, template, record, network=False, timeout=timeout, memory=memory)

    # -- cores -------------------------------------------------------------------

    def _plan_core(self, build, engine, request, identity, tier0, dirs, network,
                   timeout_request) -> None:
        family = self._system
        supported = _CORE_ENGINES.get(family, ())
        if engine == "cdb":
            raise debug_error(ENGINE_UNAVAILABLE, "cdb does not read ELF cores")
        if engine == "minidump_stackwalk":
            raise debug_error(ENGINE_UNAVAILABLE, "minidump-stackwalk does not read ELF cores")
        if engine not in ("auto", "llvm_symbolizer") and engine not in supported:
            raise debug_error(ENGINE_UNSUPPORTED_ON_PLATFORM,
                              "%s is not supported on %s" % (engine, family))
        executable = self._executable(request.executable)
        if executable:
            build.identities.append("exe:%s" % self._stat_identity(executable))
        if engine == "llvm_symbolizer":
            if family == "Windows":
                raise debug_error(ENGINE_UNSUPPORTED_ON_PLATFORM,
                                  "ELF core symbolization runs on Linux or macOS")
            record = self._tool("llvm_symbolizer", required=True)
            if not executable:
                raise debug_error(EXECUTABLE_REQUIRED, "llvm_symbolizer needs the executable")
            self._add_elf_symbolizer(build, record, executable, tier0, timeout_request)
            return
        order = [engine] if engine != "auto" else list(supported)
        if not executable:
            if engine != "auto":
                raise debug_error(EXECUTABLE_REQUIRED,
                                  "%s needs the crashed program's executable (executable=...)" % engine)
            if order:
                build.note("%s: pass the executable to walk the core with a debugger"
                           % EXECUTABLE_REQUIRED)
            return
        for candidate in order:
            record = self._tool(candidate, required=engine != "auto")
            if record is None:
                continue
            if candidate == "gdb":
                template = T.gdb_template(record.path, network=network)
                build.bindings["solibpath"] = ":".join(dirs)
            elif candidate == "lldb":
                template = T.lldb_template(record.path, network=network)
            else:
                template = T.eu_stack_template(record.path)
            build.bindings["exe"] = executable
            build.mkdir("home")
            build.mkdir("tmp")
            timeout = self._timeout(timeout_request, template.timeout_default)
            self._add_step(build, template, record, network=network, timeout=timeout,
                           memory=posix_debugger_memory(identity.size))
            return
        build.note("no debugger for cores is installed (gdb, lldb, eu-stack)")

    def _add_elf_symbolizer(self, build, record, executable, tier0, timeout_request) -> None:
        from ...domain.binaries.elf_ids import read_elf_identity

        elf = self._read_identity(executable, read_elf_identity)
        if elf is None or not elf.build_id or tier0 is None:
            build.note("llvm_symbolizer: the executable has no readable build-id; no step")
            return
        module = next((m for m in tier0.modules if (m.debug_id or "").lower() == elf.build_id.lower()),
                      None)
        if module is None:
            build.note("llvm_symbolizer: the executable's build-id is not in the core; no step")
            return
        name = _basename(module.name)
        try:
            T._stem(name)
        except T.TemplateError:
            build.note("llvm_symbolizer: module name is not a plain file name; no step")
            return
        addresses = self._addresses(tier0, module.name)
        if not addresses:
            build.note("llvm_symbolizer: no frames in %s to symbolize" % name)
            return
        build.mkdir("sym/%s" % name)
        build.staged.append((executable, "sym/%s/%s" % (name, name)))
        build.verified.append(module.name)
        template = T.symbolizer_template(record.path, name, addresses, elf=True)
        timeout = self._timeout(timeout_request, template.timeout_default)
        self._add_step(build, template, record, network=False, timeout=timeout,
                       memory=posix_debugger_memory(0))

    # -- profiles ----------------------------------------------------------------

    def plan_profile(self, request: ProfileDigestRequest, context: OperationContext, *,
                     identity: CaptureIdentity) -> DebugPlan:
        del context
        engine = str(request.engine or "auto")
        if engine not in PROFILE_ENGINES:
            raise InvalidInput("engine must be one of %s" % ", ".join(PROFILE_ENGINES))
        build = _Build()
        kind = identity.kind
        if kind in PURE_PROFILE_KINDS or kind == "profile_csv":
            if engine != "auto":
                build.note("%s is read by the pure reader; engine %s is not used" % (kind, engine))
            return self._finish(build, kind="profile", identity=identity, network=False, stores=())
        wanted = {"perf_data": "perf", "heaptrack_capture": "heaptrack_print",
                  "tracy_capture": "tracy_csvexport", "etw_etl": "xperf"}.get(kind)
        if wanted is None:
            raise debug_error(ENGINE_UNAVAILABLE, "no host tool reads %s captures" % kind)
        if kind == "etw_etl":
            if self._system != "Windows":
                raise debug_error(ENGINE_UNSUPPORTED_ON_PLATFORM,
                                  "ETL traces are read on Windows; export a CSV with WPA and "
                                  "use profile_digest")
            if engine == "wpaexporter":
                profile = self._profiles_dir / WPA_PROFILE_NAME
                if not profile.is_file():
                    raise debug_error(ENGINE_UNAVAILABLE,
                                      "wpaexporter needs the packaged %s, which is not shipped yet"
                                      % WPA_PROFILE_NAME)
                wanted = "wpaexporter"
            elif engine not in ("auto", "xperf"):
                raise debug_error(ENGINE_UNAVAILABLE, "%s does not read ETL traces" % engine)
        elif engine not in ("auto", wanted):
            raise debug_error(ENGINE_UNAVAILABLE, "%s does not read %s captures" % (engine, kind))
        if wanted in _LINUX_ONLY and self._system != "Linux":
            raise debug_error(ENGINE_UNSUPPORTED_ON_PLATFORM, "%s runs only on Linux" % wanted)
        record = self._tool(wanted, required=True)
        if wanted == "perf":
            templates = T.perf_templates(record.path)
            build.mkdir("buildid")
        elif wanted == "heaptrack_print":
            templates = (T.heaptrack_print_template(record.path),)
        elif wanted == "tracy_csvexport":
            templates = T.tracy_csvexport_templates(record.path, frame_zone=request.frame_zone)
        elif wanted == "xperf":
            templates = (T.xperf_template(record.path),)
            build.mkdir("out")
            build.mkdir("symcache")
            build.note("xperf ETL export is experimental until Windows live validation")
        else:
            profile = self._profiles_dir / WPA_PROFILE_NAME
            templates = (T.wpaexporter_template(record.path, WPA_PROFILE_NAME, str(profile)),)
            build.mkdir("out")
            build.mkdir("symcache")
        build.mkdir("home")
        build.mkdir("tmp")
        for template in templates:
            timeout = self._timeout(request.timeout_seconds, template.timeout_default)
            self._add_step(build, template, record, network=False, timeout=timeout,
                           memory=PROFILER_MEMORY_BYTES)
        return self._finish(build, kind="profile", identity=identity, network=False, stores=())


__all__ = [
    "HostDebugPlanner", "INSTALL_HINTS", "MANAGED_RUNTIME_MODULES", "TOOL_NAMES",
    "posix_debugger_memory",
]
