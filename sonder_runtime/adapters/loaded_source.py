"""Process-lifetime record of the source this process actually loaded.

A live source refresh re-executes ``server.py`` and reloads only the modules in
``LIVE_RELOAD_MODULES``; every other imported ``sonder_runtime`` module keeps
the code it had at import time. Anything that must describe the *process*
rather than the *file* therefore has to live in a module the refresh leaves
alone, which is this one (it is imported, never re-executed, and must never be
added to ``LIVE_RELOAD_MODULES``).

Known limit: a module that changed on disk between its import and its first
observation is not noticed, because the baseline then already holds the new
signature.
"""
from __future__ import annotations

import os
import sys
import threading
import weakref

_LOCK = threading.Lock()
_COMMIT_LOCK = threading.Lock()
# Package modules: name -> (st_mtime_ns, st_size) of the source file as first seen.
_BASELINE: dict[str, tuple[int, int]] = {}
# Package modules: name -> normalised source path.
_SOURCES: dict[str, str] = {}
# Every module looked at, package code or not: name -> weak reference.  A name
# whose module object is still the one looked at costs one lookup per call.
_SEEN: dict = {}
_ROOT: list = [None]
_UNSET = object()
_COMMIT: list = [_UNSET]


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _package_source(module, base: str) -> str | None:
    """Return the module's ``.py`` path if it is package code under ``base``.

    ``base`` is the normalised source root.  Package code is a file directly
    inside it or anywhere under its ``sonder_runtime`` folder. A ``venv`` or
    ``site-packages`` below the root is deliberately excluded.
    """
    try:
        path = getattr(module, "__file__", None)
    except Exception:
        return None
    if not path or not isinstance(path, str):
        return None
    if path.endswith((".pyc", ".pyo")):
        path = os.path.splitext(path)[0] + ".py"
    if not path.endswith(".py"):
        return None
    try:
        full = _norm(path)
    except (OSError, ValueError, TypeError):
        return None
    if os.path.dirname(full) == base:
        return full
    if full.startswith(os.path.join(base, "sonder_runtime") + os.sep):
        return full
    return None


def _signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


def _observe(base: str) -> None:
    """Look at modules not looked at yet; the caller holds ``_LOCK``."""
    if _ROOT[0] != base:
        _BASELINE.clear()
        _SOURCES.clear()
        _SEEN.clear()
        _ROOT[0] = base
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        seen = _SEEN.get(name)
        if seen is not None and seen() is module:
            continue
        try:
            ref = weakref.ref(module)
        except TypeError:
            continue
        # A name bound to a new module object is new code: its record starts again.
        _BASELINE.pop(name, None)
        _SOURCES.pop(name, None)
        source = _package_source(module, base)
        signature = _signature(source) if source is not None else None
        if source is not None and signature is None:
            continue  # unreadable now: look again on the next call
        _SEEN[name] = ref
        if source is not None and signature is not None:
            _BASELINE[name] = signature
            _SOURCES[name] = source


def observe(root) -> None:
    """Record the first-seen signature of package modules not yet looked at.

    Cheap when nothing new was imported: a module already looked at costs one
    dictionary lookup, whether it is package code or not, and no file access.
    Never raises.
    """
    try:
        base = _norm(root)
        with _LOCK:
            _observe(base)
    except Exception:
        return


def stale_modules(root, *, exclude=(), exclude_files=()) -> list[str]:
    """Sorted names of loaded package modules whose source changed since first seen.

    ``exclude`` names modules that are reloaded by other means: a changed one
    is not stale, it is waiting for its reload.  Every other name bound to the
    same module object is excluded with it (a root shim and its packaged
    module are one object under two names).  ``exclude_files`` are source
    files refreshed by other means, whatever name they are loaded under.  A
    module whose source file is gone counts as changed.  Never raises.
    """
    try:
        base = _norm(root)
        skip = set(exclude or ())
        skip_objects = {id(module) for module in map(sys.modules.get, skip) if module is not None}
        skip_files = {_norm(path) for path in (exclude_files or ()) if path}
        with _LOCK:
            _observe(base)
            known = [(name, signature, _SOURCES.get(name), _SEEN.get(name))
                     for name, signature in _BASELINE.items()]
        stale = []
        for name, baseline, source, seen in known:
            if name in skip or source is None or source in skip_files:
                continue
            module = sys.modules.get(name)
            if module is None or seen is None or seen() is not module:
                continue
            if id(module) in skip_objects:
                continue
            if _signature(source) != baseline:
                stale.append(name)
        return sorted(stale)
    except Exception:
        return []


def running_commit(compute) -> str | None:
    """Return the commit captured by the first call; later calls reuse it."""
    with _COMMIT_LOCK:
        if _COMMIT[0] is _UNSET:
            try:
                _COMMIT[0] = compute()
            except Exception:
                _COMMIT[0] = None
        return _COMMIT[0]


def reset() -> None:
    """Clear the baseline and stored commit. For tests only."""
    with _LOCK:
        _BASELINE.clear()
        _SOURCES.clear()
        _SEEN.clear()
        _ROOT[0] = None
    with _COMMIT_LOCK:
        _COMMIT[0] = _UNSET
