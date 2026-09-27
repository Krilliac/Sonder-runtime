"""Host developer-tool discovery (PATH, known prefixes, OS metadata).

Search order per tool: absolute PATH entries (deduplicated), then platform
extra directories (Linux prefixes, Homebrew, scoop/choco/winget/Program
Files), then metadata discoverers (vswhere, Windows SDK, App Paths, the py
launcher, Xcode, app bundles).

Version probes launch only ``(<discovered path>, *spec.version_args)`` from
the host-owned registry, on a bounded pool under a global deadline and a
launch cap.  A path that is project-local, a Store alias, or a batch file
with unsafe arguments is recorded but never started.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass
import re
import time
from typing import Callable, Iterable

from sonder_runtime.domain.host_tools.model import (
    MAX_ALTERNATIVES,
    MAX_DETAILS,
    DiscoverySource,
    InventorySnapshot,
    ToolRecord,
    ToolSpec,
    VersionStatus,
    build_snapshot,
    parse_version,
)
from sonder_runtime.domain.host_tools.registry import HOST_TOOL_SPECS
import sonder_runtime.adapters.host_tools.darwin as darwin
import sonder_runtime.adapters.host_tools.linux as linux
import sonder_runtime.adapters.host_tools.windows as windows
import sonder_runtime.platform.runtime_threads as runtime_threads
import sonder_runtime.platform.toolchain_policy as toolchain_policy

from .guards import is_windows_apps_alias
from .probes import HostProbes, probe_environment

MAX_LAUNCHED_PROBES = 160
_BATCH_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=,+@-]*$")
_BATCH_UNSAFE_PATH = re.compile(r"[%!^&|<>\"\r\n\x00]")
_CACHEABLE = frozenset({VersionStatus.OK, VersionStatus.OUTPUT_LIMIT})
_OUTCOME_STATUS = {
    "ok": VersionStatus.OK,
    "error": VersionStatus.FAILED,
    "timeout": VersionStatus.TIMEOUT,
    "output_limit": VersionStatus.OUTPUT_LIMIT,
    "start_failed": VersionStatus.FAILED,
}


def _is_absolute_for(system: str, path: str) -> bool:
    if not path or "\x00" in path:
        return False
    if system == "Windows":
        return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")
    return path.startswith("/")


def _without_jvm_banners(text: str) -> str:
    """Drop JVM ``Picked up JAVA_TOOL_OPTIONS`` lines that precede versions."""
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("Picked up ")
    )


def batch_arguments_safe(path: str, args: Iterable[str]) -> bool:
    """cmd.exe re-parses a .bat/.cmd command line; allow only inert text."""
    if not path.lower().endswith((".bat", ".cmd")):
        return True
    if _BATCH_UNSAFE_PATH.search(path):
        return False
    return all(_BATCH_SAFE_ARG.match(arg) for arg in args)


@dataclass
class _Found:
    spec: ToolSpec
    path: str
    source: DiscoverySource
    on_path: bool
    alternatives: list[str]
    details: list[tuple[str, str]]
    version: str = ""
    status: VersionStatus = VersionStatus.NOT_PROBED
    identity: str = ""


class HostToolDiscovery:
    """Implements the ``InventoryDiscovery`` port for the real host."""

    def __init__(
        self,
        probes: HostProbes,
        *,
        specs: tuple[ToolSpec, ...] = HOST_TOOL_SPECS,
        budget_seconds: float = 30.0,
        probe_timeout_seconds: float = 3.0,
        max_workers: int = 4,
        max_probes: int = MAX_LAUNCHED_PROBES,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if budget_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ValueError("discovery bounds must be positive")
        if not 1 <= int(max_workers) <= 16:
            raise ValueError("max_workers must be between 1 and 16")
        self._probes = probes
        self._specs = specs
        self._budget = float(budget_seconds)
        self._probe_timeout = float(probe_timeout_seconds)
        self._workers = int(max_workers)
        self._max_probes = max(0, min(int(max_probes), MAX_LAUNCHED_PROBES))
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    # -- search directories -------------------------------------------------

    def _key(self, path: str) -> str:
        path = path.rstrip("\\/") or path
        return path.casefold() if self._probes.system == "Windows" else path

    def search_dirs(self, notes: list[str]) -> list[tuple[str, DiscoverySource, bool]]:
        probes = self._probes
        system = probes.system
        separator = ";" if system == "Windows" else ":"
        dirs: list[tuple[str, DiscoverySource, bool]] = []
        seen: set[str] = set()
        skipped = 0

        def add(path: str, source: DiscoverySource, on_path: bool) -> None:
            nonlocal skipped
            path = (path or "").strip().strip('"') if system == "Windows" else (path or "")
            if not _is_absolute_for(system, path):
                if path:
                    skipped += 1
                return
            key = self._key(path)
            if key in seen:
                return
            seen.add(key)
            dirs.append((path, source, on_path))

        for entry in probes.env_get("PATH").split(separator):
            add(entry, DiscoverySource.PATH, True)
        if skipped:
            notes.append(f"skipped {skipped} relative or invalid PATH entries")
        if system == "Linux":
            for path in linux.known_prefix_dirs(probes):
                add(path, DiscoverySource.KNOWN_PREFIX, False)
        elif system == "Darwin":
            brew = self._which_on(dirs, "brew")
            for path in darwin.discover_brew(probes, brew):
                add(path, DiscoverySource.BREW, False)
            for path in ("/usr/local/bin", "/opt/homebrew/bin"):
                if probes.is_dir(path):
                    add(path, DiscoverySource.KNOWN_PREFIX, False)
        elif system == "Windows":
            for path, source in windows.extra_dir_sources(probes):
                if probes.is_dir(path):
                    add(path, source, False)
        return dirs

    def _which_on(self, dirs, name: str) -> str | None:
        for directory, _source, _on_path in dirs:
            hit = self._probes.which(name, directory)
            if hit:
                return hit
        return None

    # -- discovery ----------------------------------------------------------

    def _find(self, spec: ToolSpec, dirs) -> _Found | None:
        probes = self._probes
        found: _Found | None = None
        seen_real: set[str] = set()
        for directory, source, on_path in dirs:
            for executable in spec.executables:
                hit = probes.which(executable, directory)
                if not hit or not _is_absolute_for(probes.system, hit):
                    continue
                try:
                    real = self._key(probes.realpath(hit))
                except (OSError, ValueError):
                    real = self._key(hit)
                if real in seen_real:
                    continue
                seen_real.add(real)
                if found is None:
                    found = _Found(spec, hit, source, on_path, [], [])
                elif len(found.alternatives) < MAX_ALTERNATIVES:
                    found.alternatives.append(hit)
                break
        return found

    def _merge_metadata(self, found: dict[str, _Found], records: Iterable[ToolRecord], spec_by_name) -> None:
        for record in records:
            existing = found.get(record.name)
            if existing is None:
                spec = spec_by_name.get(record.name) or ToolSpec(
                    name=record.name, category=record.category, executables=(), version_args=None,
                )
                found[record.name] = _Found(
                    spec, record.path, record.source, False, list(record.alternatives),
                    list(record.details), record.version, record.version_status, record.identity,
                )
                continue
            if self._key(existing.path) != self._key(record.path) and \
                    record.path not in existing.alternatives and \
                    len(existing.alternatives) < MAX_ALTERNATIVES:
                existing.alternatives.append(record.path)
            if existing.spec.version_args is None and record.version:
                existing.version = record.version
                existing.status = VersionStatus.FROM_METADATA
            for detail in record.details:
                if detail not in existing.details and len(existing.details) < MAX_DETAILS:
                    existing.details.append(detail)

    def discover(self, *, previous: InventorySnapshot | None, full: bool) -> InventorySnapshot:
        probes = self._probes
        started = self._monotonic()
        deadline = started + self._budget
        notes: list[str] = []
        system = probes.system
        specs = [spec for spec in self._specs if spec.supports(system)]
        spec_by_name = {spec.name: spec for spec in specs}
        dirs = self.search_dirs(notes)
        found: dict[str, _Found] = {}
        for spec in specs:
            if not spec.executables:
                continue
            hit = self._find(spec, dirs)
            if hit is not None:
                found[spec.name] = hit

        if system == "Windows":
            self._windows_metadata(found, specs, spec_by_name, notes)
        elif system == "Darwin":
            self._merge_metadata(found, darwin.discover_xcode(probes), spec_by_name)
            bundles = [r for r in darwin.discover_app_bundles(probes) if r.name not in found]
            self._merge_metadata(found, bundles, spec_by_name)

        previous_by_name = {r.name: r for r in previous.tools} if previous is not None and not full else {}
        jobs: list[_Found] = []
        for item in found.values():
            item.identity = item.identity or (probes.stat_identity(item.path) or "")
            if item.status is VersionStatus.FROM_METADATA or item.source in (
                DiscoverySource.VSWHERE, DiscoverySource.WINDOWS_SDK,
                DiscoverySource.XCODE, DiscoverySource.APP_BUNDLE,
            ):
                continue
            args = item.spec.version_args
            if is_windows_apps_alias(item.path):
                item.status = VersionStatus.ALIAS
                continue
            try:
                local = bool(probes.project_local(item.path))
            except Exception:
                local = True  # fail closed: an unverifiable path is never run
            if local:
                item.status = VersionStatus.PROJECT_LOCAL
                continue
            if args is None:
                item.status = VersionStatus.NOT_PROBED
                continue
            if system == "Windows" and not batch_arguments_safe(item.path, args):
                item.status = VersionStatus.NOT_PROBED
                notes.append(f"{item.spec.name}: batch launcher arguments are not batch-safe")
                continue
            cached = previous_by_name.get(item.spec.name)
            if (
                cached is not None
                and cached.path == item.path
                and cached.identity
                and cached.identity == item.identity
                and cached.version_status in _CACHEABLE
            ):
                item.version, item.status = cached.version, cached.version_status
                continue
            jobs.append(item)

        self._run_probes(jobs, deadline, notes)
        records = [
            ToolRecord(
                name=item.spec.name,
                category=item.spec.category,
                path=item.path,
                source=item.source,
                on_path=item.on_path,
                version=item.version[:64],
                version_status=item.status,
                identity=item.identity[:64],
                alternatives=tuple(item.alternatives[:MAX_ALTERNATIVES]),
                details=tuple((k[:64], v[:200]) for k, v in item.details[:MAX_DETAILS]),
            )
            for item in found.values()
        ]
        duration_ms = int(max(0.0, self._monotonic() - started) * 1000)
        return build_snapshot(
            os=system,
            os_release=probes.release,
            machine=probes.machine,
            created_at=self._wall_clock(),
            duration_ms=duration_ms,
            tools=records,
            notes=notes,
        )

    # -- windows metadata ---------------------------------------------------

    def _windows_metadata(self, found, specs, spec_by_name, notes) -> None:
        probes = self._probes
        try:
            self._merge_metadata(found, windows.discover_visual_studio(probes, notes), spec_by_name)
        except Exception as error:
            notes.append(f"vswhere discovery failed: {type(error).__name__}")
        try:
            self._merge_metadata(found, windows.discover_windows_sdk(probes), spec_by_name)
        except Exception as error:
            notes.append(f"windows sdk discovery failed: {type(error).__name__}")
        missing = [spec.name for spec in specs if spec.executables and spec.name not in found]
        try:
            for name, path in windows.discover_app_paths(probes, missing):
                spec = spec_by_name[name]
                found[name] = _Found(spec, path, DiscoverySource.APP_PATHS, False, [], [])
        except Exception as error:
            notes.append(f"app paths discovery failed: {type(error).__name__}")
        launcher = found.get("py")
        if launcher is None or is_windows_apps_alias(launcher.path):
            return
        try:
            if probes.project_local(launcher.path):
                return
        except Exception:
            return
        try:
            entries = windows.discover_py_launcher(probes, launcher.path)
        except Exception as error:
            notes.append(f"py launcher discovery failed: {type(error).__name__}")
            return
        for tag, path, _default in entries:
            if len(launcher.details) >= MAX_DETAILS:
                break
            launcher.details.append((f"py:{tag}"[:64], path[:200]))
        python = found.get("python")
        if entries and (python is None or is_windows_apps_alias(python.path)):
            tag, path, _default = next((e for e in entries if e[2]), entries[0])
            spec = spec_by_name.get("python")
            if spec is not None and probes.is_file(path):
                alternatives = [python.path] if python is not None else []
                found["python"] = _Found(
                    spec, path, DiscoverySource.PY_LAUNCHER, False, alternatives, [("py_tag", tag)],
                    parse_version(tag, r"(\d+(?:\.\d+){1,3})"), VersionStatus.FROM_METADATA,
                )

    # -- probes -------------------------------------------------------------

    def _run_probes(self, jobs: list[_Found], deadline: float, notes: list[str]) -> None:
        if not jobs:
            return
        launchable = jobs[: self._max_probes]
        for item in jobs[self._max_probes:]:
            item.status = VersionStatus.DEFERRED
        if len(jobs) > self._max_probes:
            notes.append(f"probe cap reached; {len(jobs) - self._max_probes} tools deferred")
        probes = self._probes

        def probe(item: _Found) -> tuple[VersionStatus, str]:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return VersionStatus.DEFERRED, ""
            argv = (item.path, *(item.spec.version_args or ()))
            env = probe_environment(probes, item.spec.probe_env)
            result = probes.run(argv, min(self._probe_timeout, remaining), env)
            status = _OUTCOME_STATUS.get(result.outcome, VersionStatus.FAILED)
            if status in (VersionStatus.OK, VersionStatus.OUTPUT_LIMIT):
                text = toolchain_policy.safe_output(_without_jvm_banners(result.output or ""))
                return status, parse_version(text, item.spec.version_pattern)
            return status, ""

        pool = runtime_threads.ThreadPoolExecutor(
            max_workers=min(self._workers, len(launchable)),
            thread_name_prefix="sonder-tool-probe",
        )
        futures = {}
        try:
            for item in launchable:
                futures[pool.submit(probe, item)] = item
            pending = set(futures)
            # Real-time backstop: every probe is itself bounded, so the pool
            # drains within the budget plus one probe timeout.
            backstop = time.monotonic() + self._budget + self._probe_timeout + 2.0
            while pending:
                remaining = backstop - time.monotonic()
                if remaining <= 0:
                    break
                _done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        deferred = 0
        for future, item in futures.items():
            if future.done() and not future.cancelled() and future.exception() is None:
                item.status, item.version = future.result()
            elif future.done() and not future.cancelled():
                item.status = VersionStatus.FAILED
            else:
                item.status = VersionStatus.DEFERRED
            if item.status is VersionStatus.DEFERRED:
                deferred += 1
        if deferred:
            notes.append(f"discovery budget exhausted; {deferred} version probes deferred")


__all__ = ["HostToolDiscovery", "MAX_LAUNCHED_PROBES", "batch_arguments_safe"]
