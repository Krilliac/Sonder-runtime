"""Network consent for build jobs: ``unshare -rn`` where it works, advisory elsewhere.

With ``allow_network=False`` (the default) every job gets the host-fixed
hardening arguments from the templates (FetchContent disconnected, vcpkg
manifest install and NuGet restore off). On Linux, when ``unshare -rn true``
succeeds as the runtime's own uid, the argv is also prefixed with
``unshare -rn --``: the build runs in a fresh network namespace whose only
interface (loopback) is down, and the receipt says ``enforced_off``. That is
failure containment for downloads, not a security boundary, and it is never
described as one.

Loopback being down breaks compiler launchers that talk to a local daemon
(sccache server, distcc, icecc, FASTBuild cache or brokerage, Incredibuild).
When the tree's cache names one, ``SONDER_BUILD_NETWORK=default`` downgrades
to ``advisory_off`` with a note and ``enforce`` refuses with
``NETWORK_ISOLATION_UNAVAILABLE``. Windows and macOS are always
``advisory_off``.

The probe result is cached for the process. When the runtime runs as root the
namespace probe proves little about operators' unprivileged hosts, so a second
probe under the configured unprivileged uid (via ``setpriv``) is recorded for
reporting only.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ...application.build.ports import NETWORK_ISOLATION_UNAVAILABLE, build_error

ENFORCED_OFF = "enforced_off"
ADVISORY_OFF = "advisory_off"
ALLOWED = "allowed"
NETWORK_MODES = ("enforce", "default", "advisory")
PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_UNPRIVILEGED_UID = 65534
# Compiler launchers and build drivers that need a local daemon or broker.
DAEMON_LAUNCHERS = frozenset({
    "sccache", "distcc", "icecc", "icecream", "pump", "fbuild", "fastbuild",
    "buildconsole", "xgconsole", "incredibuild", "recc", "buildbox",
})
_PROBE_ENV = (("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), ("LANG", "C"), ("LC_ALL", "C"))


@dataclass(frozen=True, slots=True)
class NetworkProbe:
    platform: str
    unshare: str
    enforceable: bool
    runtime_uid: int | None
    unprivileged_uid: int | None = None
    unprivileged_enforceable: bool | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NetworkDecision:
    policy: str
    prefix: tuple[str, ...]
    notes: tuple[str, ...] = ()
    checked_executables: tuple[str, ...] = ()


def launcher_names(values: Iterable[str]) -> tuple[str, ...]:
    """Daemon-backed launcher names found in cache values or profile argv."""
    found: list[str] = []
    for value in values:
        for token in str(value or "").replace(";", " ").split():
            base = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            for suffix in (".exe", ".cmd", ".bat"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            if base in DAEMON_LAUNCHERS and base not in found:
                found.append(base)
    return tuple(found)


class NetworkIsolation:
    """Decide the network policy of one job; probes once per process."""

    def __init__(self, *, mode: str = "default", platform: str | None = None,
                 lookup=None, run: Callable[..., Any] | None = None,
                 geteuid: Callable[[], int] | None = None,
                 unprivileged_uid: int | None = DEFAULT_UNPRIVILEGED_UID,
                 executable_guard: Callable[[str], str] | None = None) -> None:
        if mode not in NETWORK_MODES:
            raise ValueError("network mode must be one of %s" % ", ".join(NETWORK_MODES))
        self._mode = mode
        self._platform = platform or ("windows" if os.name == "nt" else sys.platform)
        self._lookup = lookup
        if run is None:
            from ..host_tools.bounded_process import run_bounded as run
        self._run = run
        self._geteuid = geteuid or getattr(os, "geteuid", lambda: -1)
        self._unprivileged_uid = unprivileged_uid
        if executable_guard is None:
            from ..host_tools.guards import require_host_executable as executable_guard
        self._guard = executable_guard
        self._probe: NetworkProbe | None = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    # -- probe -----------------------------------------------------------------

    def _tool(self, name: str) -> str:
        record = self._lookup.lookup(name) if self._lookup is not None else None
        if record is not None:
            return str(record.path)
        for folder in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    def _ok(self, argv: tuple[str, ...]) -> bool:
        try:
            result = self._run(argv, timeout_seconds=PROBE_TIMEOUT_SECONDS, max_output_chars=2_000,
                               env=dict(_PROBE_ENV), cwd="/")
        except (OSError, ValueError):
            return False
        return getattr(result, "outcome", "") == "ok"

    def probe(self) -> NetworkProbe:
        with self._lock:
            if self._probe is not None:
                return self._probe
            self._probe = self._probe_once()
            return self._probe

    def _probe_once(self) -> NetworkProbe:
        if not self._platform.startswith("linux"):
            return NetworkProbe(self._platform, "", False, None,
                                notes=("network isolation is advisory on %s" % self._platform,))
        unshare = self._tool("unshare")
        uid = self._geteuid()
        if not unshare:
            return NetworkProbe(self._platform, "", False, uid, notes=("unshare is not installed",))
        try:
            self._guard(unshare)
        except PermissionError:
            return NetworkProbe(self._platform, "", False, uid,
                                notes=("unshare failed the host executable guard",))
        enforceable = self._ok((unshare, "-rn", "true"))
        notes: list[str] = []
        if not enforceable:
            notes.append("unshare -rn is not permitted for the runtime's uid")
        unprivileged_uid = None
        unprivileged_ok = None
        if uid == 0 and self._unprivileged_uid is not None:
            setpriv = self._tool("setpriv")
            unprivileged_uid = int(self._unprivileged_uid)
            if setpriv:
                unprivileged_ok = self._ok((
                    setpriv, "--reuid=%d" % unprivileged_uid, "--regid=%d" % unprivileged_uid,
                    "--clear-groups", "--", unshare, "-rn", "true"))
                notes.append("runtime is root; uid %d %s create a network namespace"
                             % (unprivileged_uid, "can" if unprivileged_ok else "cannot"))
            else:
                notes.append("runtime is root; setpriv is missing so the unprivileged probe was skipped")
        return NetworkProbe(self._platform, unshare, enforceable, uid, unprivileged_uid,
                            unprivileged_ok, tuple(notes))

    # -- decision --------------------------------------------------------------

    def decide(self, *, allow_network: bool, launchers: Iterable[str] = ()) -> NetworkDecision:
        if allow_network:
            return NetworkDecision(ALLOWED, (), ("network allowed for this job (approved separately)",))
        found = launcher_names(launchers)
        if found:
            message = ("compiler launcher %s needs a local daemon, which loopback-down "
                       "isolation would break" % ", ".join(found))
            if self._mode == "enforce":
                raise build_error(NETWORK_ISOLATION_UNAVAILABLE,
                                  message + "; SONDER_BUILD_NETWORK=enforce refuses the job")
            return NetworkDecision(ADVISORY_OFF, (), (message + "; network is advisory_off",))
        if self._mode == "advisory":
            return NetworkDecision(ADVISORY_OFF, (), ("SONDER_BUILD_NETWORK=advisory",))
        probe = self.probe()
        if probe.enforceable and probe.unshare:
            return NetworkDecision(ENFORCED_OFF, (probe.unshare, "-rn", "--"), probe.notes,
                                   checked_executables=(probe.unshare,))
        if self._mode == "enforce":
            raise build_error(NETWORK_ISOLATION_UNAVAILABLE,
                              "network isolation is unavailable on this host (%s)"
                              % "; ".join(probe.notes or ("no namespace support",)))
        return NetworkDecision(ADVISORY_OFF, (), probe.notes)


def network_wrapper(**kwargs) -> NetworkIsolation:
    """The network policy decider (factory name kept from the spec)."""
    return NetworkIsolation(**kwargs)


__all__ = [
    "ADVISORY_OFF", "ALLOWED", "DAEMON_LAUNCHERS", "DEFAULT_UNPRIVILEGED_UID", "ENFORCED_OFF",
    "NETWORK_MODES", "NetworkDecision", "NetworkIsolation", "NetworkProbe", "launcher_names",
    "network_wrapper",
]
