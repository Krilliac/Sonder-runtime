"""Tiny source-file live reload helper for long-running sonder processes.

This is deliberately conservative: it reloads already-imported helper modules at
request/tool boundaries when their source file's mtime changes. It does not try
to mutate active stack frames or native extensions. ``reloadable_mcp.py`` owns
the separate atomic whole-server/tool-registry refresh boundary.
"""
import importlib
import os
import sys
import threading


_LOCK = threading.RLock()
_MTIMES = {}
_SIGNATURES = {}
_ERRORS = {}


def enabled():
    return os.environ.get("SONDER_LIVE_RELOAD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _source_path(module):
    path = getattr(module, "__file__", None)
    if not path:
        return None
    if path.endswith((".pyc", ".pyo")):
        path = os.path.splitext(path)[0] + ".py"
    if not path.endswith(".py"):
        return None
    return os.path.abspath(path)


def _mtime(module):
    path = _source_path(module)
    if not path or not os.path.exists(path):
        return None
    return os.path.getmtime(path)


def _signature(module):
    path = _source_path(module)
    if not path or not os.path.exists(path):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def prime_modules(module_names):
    """Record helper source state immediately after the host imports it.

    Without this startup boundary, the first request after an on-disk edit can
    mistake the edited file for the baseline even though ``sys.modules`` still
    contains the older code.  Existing baselines are never overwritten.
    """
    if not enabled():
        return
    with _LOCK:
        for name in module_names:
            module = sys.modules.get(name)
            if module is None:
                continue
            signature = _signature(module)
            if signature is None:
                continue
            _SIGNATURES.setdefault(name, signature)
            _MTIMES.setdefault(name, signature[0] / 1_000_000_000)


def reload_changed_modules(module_names):
    """Reload named modules whose source mtime changed.

    Returns a dict of name -> module for names that were imported/reloaded. The
    first observation records the mtime without reloading, so calling this at
    startup is harmless.
    """
    if not enabled():
        return {}
    changed = {}
    with _LOCK:
        for name in module_names:
            module = sys.modules.get(name)
            if module is None:
                try:
                    module = importlib.import_module(name)
                except Exception:
                    continue
            signature = _signature(module)
            if signature is None:
                continue
            mtime = signature[0] / 1_000_000_000
            old = _MTIMES.get(name)
            old_signature = _SIGNATURES.get(name)
            if old is None or old_signature is None:
                _MTIMES[name] = mtime
                _SIGNATURES[name] = signature
                changed[name] = module
                continue
            if signature == old_signature:
                changed[name] = module
                continue
            try:
                module = importlib.reload(module)
            except Exception as exc:
                _ERRORS[name] = "%s: %s" % (exc.__class__.__name__, exc)
                changed[name] = module
                continue
            _ERRORS.pop(name, None)
            refreshed_signature = _signature(module) or signature
            _SIGNATURES[name] = refreshed_signature
            _MTIMES[name] = refreshed_signature[0] / 1_000_000_000
            changed[name] = module
    return changed


def snapshot(module_names):
    rows = []
    with _LOCK:
        for name in module_names:
            module = sys.modules.get(name)
            path = _source_path(module) if module is not None else None
            rows.append({
                "name": name,
                "path": path or "",
                "mtime": _MTIMES.get(name),
                "loaded": module is not None,
                "error": _ERRORS.get(name, ""),
            })
    return rows
