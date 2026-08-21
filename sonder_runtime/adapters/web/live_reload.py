"""Tiny source-file live reload helper for long-running sonder processes.

This is deliberately conservative: it reloads already-imported helper modules at
request/tool boundaries when their source file's mtime changes. It does not try
to mutate active stack frames or native extensions. ``reloadable_mcp.py`` owns
the separate atomic whole-server/tool-registry refresh boundary.
"""
import ast
import importlib
import importlib.util
import os
import sys
import threading
import types


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
                # Unchanged: do NOT report it as changed. This branch used to
                # add the module to `changed`, contradicting the function's name
                # and its docstring ("names that were imported/reloaded") and
                # making every caller rebind every watched module on every
                # request. Rebinding an unchanged module to itself is harmless,
                # so the bug was latent, but the returned set was wrong.
                continue
            try:
                module = _stage_module_reload(module)
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


def _stage_module_reload(module):
    """Execute replacement source before publishing a module to callers.

    ``importlib.reload`` deliberately preserves a module dictionary.  That is
    convenient for interactive sessions, but it makes source removal unsafe in
    a long-running service: deleting a permission check or a deprecated helper
    from a deployed file leaves the old symbol callable in the live module.

    Build a fresh module object instead.  Its source executes without changing
    ``sys.modules``; only a fully initialized candidate replaces every alias
    that referred to the old object.  A syntax/import/runtime failure therefore
    leaves the old module and all of its aliases exactly as they were.
    """
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    if spec is None or loader is None:
        raise ImportError("module has no reloadable import specification")
    candidate = importlib.util.module_from_spec(spec)
    # A helper may deliberately preserve a process-owned resource with the
    # established ``if "_STATE" not in globals()`` guard.  A clean module is
    # still required to make source removals effective, so carry over only
    # those private names which the *new* source explicitly retains.  Removed
    # guards/resources are not inherited and public retired symbols never are.
    for name in _reload_preserved_state_names(spec):
        if name in module.__dict__:
            candidate.__dict__[name] = module.__dict__[name]
    loader.exec_module(candidate)
    # Compatibility aliases (for example ``workflow_store`` pointing at its
    # packaged adapter) must move together.  Publish only after execution has
    # succeeded so no alias observes a half-executed candidate.
    # Modules that used ``import helper`` keep the old module object in their
    # globals even after sys.modules moves.  Rebind those direct references
    # before publication so unchanged importers cannot execute stale helpers.
    for importer in tuple(sys.modules.values()):
        if not isinstance(importer, types.ModuleType) or importer is module:
            continue
        for attr, value in tuple(vars(importer).items()):
            if value is module:
                setattr(importer, attr, candidate)
    aliases = [name for name, value in tuple(sys.modules.items()) if value is module]
    for alias in aliases:
        sys.modules[alias] = candidate
    return candidate


def _reload_preserved_state_names(spec):
    """Return private globals the replacement source explicitly preserves."""
    try:
        source = spec.loader.get_source(spec.name)
        tree = ast.parse(source or "")
    except (AttributeError, OSError, SyntaxError, TypeError):
        return ()
    names = []
    for statement in tree.body:
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.NotIn)
            and len(test.comparators) == 1
            and isinstance(test.left, ast.Constant)
            and isinstance(test.left.value, str)
            and test.left.value.startswith("_")
        ):
            continue
        target = test.comparators[0]
        if not (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id == "globals"
            and not target.args
            and not target.keywords
        ):
            continue
        names.append(test.left.value)
    return tuple(names)


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
