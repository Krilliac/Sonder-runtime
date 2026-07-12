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
            mtime = _mtime(module)
            if mtime is None:
                continue
            old = _MTIMES.get(name)
            if old is None:
                _MTIMES[name] = mtime
                changed[name] = module
                continue
            if mtime <= old:
                changed[name] = module
                continue
            try:
                module = importlib.reload(module)
            except Exception as exc:
                _ERRORS[name] = "%s: %s" % (exc.__class__.__name__, exc)
                changed[name] = module
                continue
            _ERRORS.pop(name, None)
            _MTIMES[name] = _mtime(module) or mtime
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
