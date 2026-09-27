"""Is a no-network user namespace available for Tier-1 debugger steps?

Linux Tier-1 steps (gdb, lldb, eu-stack, llvm-symbolizer, perf report) parse
hostile files with native parsers. Without network consent each step runs as
``unshare --user --map-current-user --net -- <tool> ...``: a fresh network
namespace with only a down loopback, so a debuginfod URL, a crafted
``solib`` path or a parser bug cannot reach the network. ``unshare`` execs
without forking, so the job provider's process-tree kill still works.

The probe runs the host-owned argv ``[unshare, --user, --map-current-user,
--net, --, /bin/true]`` once per unshare path through an injected bounded
runner (the -2 ``bounded_process.run_bounded`` in production; this layer may
not launch processes itself) and caches the answer for the process.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable

PROBE_TAIL = ("--user", "--map-current-user", "--net", "--", "/bin/true")
PROBE_TIMEOUT_SECONDS = 3.0

_CACHE: dict[str, bool] = {}
_LOCK = threading.Lock()


def probe_argv(unshare_path: str) -> tuple[str, ...]:
    return (str(unshare_path), *PROBE_TAIL)


def netns_available(unshare_path: str, *, runner: Callable[..., Any] | None = None,
                    platform_name: str | None = None) -> bool:
    """True when the probe exited 0; cached per ``unshare_path``.

    ``runner(argv, timeout_seconds=..., max_output_chars=..., env=...)`` returns
    an object with ``outcome`` and ``exit_code`` (``BoundedRun``). Without a
    runner nothing is launched and the answer is False.
    """
    system = platform_name if platform_name is not None else sys.platform
    if not str(system).startswith("linux") or not unshare_path or runner is None:
        return False
    path = str(unshare_path)
    if not os.path.isabs(path) or "\x00" in path:
        return False
    with _LOCK:
        if path in _CACHE:
            return _CACHE[path]
    try:
        result = runner(probe_argv(path), timeout_seconds=PROBE_TIMEOUT_SECONDS,
                        max_output_chars=512, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        ok = getattr(result, "outcome", "") == "ok" and getattr(result, "exit_code", 1) == 0
    except Exception:
        ok = False
    with _LOCK:
        _CACHE[path] = ok
    return ok


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = ["PROBE_TAIL", "netns_available", "probe_argv", "reset_cache"]
