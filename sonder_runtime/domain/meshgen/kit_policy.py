"""Which kit names a recorded build script may call.  Pure; stdlib only.

A kit is trusted Python the operator supplies.  Model-written scripts never execute: the recorder
(``dsl``) only records calls to the names this policy exposes, and the sandboxed replay calls them with
plain data.  The OS sandbox confines what any kit function can do to files, network and processes, so
this policy does not exclude functions merely because they read or write files.  It excludes only what
would break the harness flow (export, render, save) or touch secrets/processes.  Exposure is an
allow-list derived from the kit's source:

* a function or class is EXCLUDED when anything in it touches scene-level export/render/save
  (``export_scene``, ``import_scene``, ``save*``, the render operator, ``open_mainfile``,
  ``read_homefile``...: the harness exports and renders itself, and a kit helper that does it
  too would clobber the harness' output), process control (``exit``/``quit``, ``exec``/``eval``/
  ``compile``/``__import__``, ``input``/``breakpoint``) or one of the modules that read
  secrets or spawn processes (``os`` except path-string helpers, ``sys``, ``subprocess``,
  ``socket``, ``ctypes``, ``winreg``, ``importlib``, ``builtins``, ``pickle``, ``marshal``,
  ``urllib``, ``http``);
* a module-level constant is excluded when it is a string/f-string/bytes literal, or built from
  anything excluded (``KIT_DIR``, ``REPO_ROOT``, ``PREVIEW_DIR`` ...: locations are never shown);
* exclusion is transitive: a function that uses an excluded kit name is excluded too;
* the operator may override the derivation inside the kit with module-level literals:
  ``MESHGEN_ALLOW = ["box", "lathe"]`` replaces the derived allow-list (only public names the
  kit defines count) and ``MESHGEN_EXCLUDE = ["helper"]`` removes names from whatever the
  allow-list is.  Overrides are the operator's responsibility.
"""
from __future__ import annotations

import ast

DANGEROUS_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "ctypes", "winreg", "_winreg", "_winapi", "msvcrt", "importlib",
    "builtins", "pickle", "marshal", "urllib", "http", "multiprocessing", "signal",
})
_IO_NAMES = frozenset({"exec", "eval", "compile", "__import__", "input", "breakpoint", "exit", "quit"})
# Attributes of a dangerous module (``os``...) that do no I/O: pure path-string manipulation and
# constants.  Every other use of such a module marks the function (``os.system``, ``os.remove``...),
# while ``thing.system`` on an ordinary object does not: the receiver decides, not the bare name.
_SAFE_MODULE_ATTRIBUTES = frozenset({"path", "sep", "linesep", "pathsep", "curdir", "pardir", "name"})
# Scene-level export/import/save/render touch points: the harness owns those steps.
_IO_ATTRIBUTES = frozenset({
    "save", "save_as", "save_render", "save_as_mainfile", "save_mainfile", "export_scene", "import_scene",
    "open_mainfile", "read_homefile", "recover_last_session", "exit", "as_module", "execfile",
    "texts", "preferences", "handlers",
})


def _literal_names(tree: ast.Module, variable: str):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            if isinstance(value, (list, tuple, set, frozenset)) and all(isinstance(v, str) for v in value):
                return frozenset(value)
    return None


def _dangerous_aliases(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] in DANGEROUS_MODULES:
                    names.add((item.asname or item.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in DANGEROUS_MODULES:
            names.update(item.asname or item.name for item in node.names)
    return names


def analyse_kit(kit_source: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(allowed, excluded)`` public names for a kit source; unparseable -> nothing allowed."""
    try:
        tree = ast.parse(kit_source or "")
    except (SyntaxError, ValueError):
        return frozenset(), frozenset()

    defs: dict[str, ast.AST] = {}
    consts: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            consts[node.target.id] = node.value
    public = {name for name in list(defs) + list(consts) if not name.startswith("_") and not name.startswith("MESHGEN_")}
    dangerous = _dangerous_aliases(tree)

    def touches_io(node: ast.AST) -> bool:
        nodes = list(ast.walk(node))
        safe_receivers = {
            id(sub.value) for sub in nodes
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id in dangerous
            and sub.attr in _SAFE_MODULE_ATTRIBUTES}
        for sub in nodes:
            if isinstance(sub, ast.Name) and id(sub) not in safe_receivers and (
                    sub.id in dangerous or sub.id in _IO_NAMES):
                return True
            if isinstance(sub, ast.Attribute):
                if sub.attr in _IO_ATTRIBUTES:
                    return True
                if sub.attr == "render" and isinstance(sub.value, ast.Attribute) and sub.value.attr == "ops":
                    return True
        return False

    bad: set[str] = set()      # tainted: touches I/O, directly or through another bad name
    hidden: set[str] = set()   # text constants: not exposed, but harmless to the kit's own code (COL)
    for name, node in defs.items():
        if touches_io(node):
            bad.add(name)
    for name, value in consts.items():
        if isinstance(value, ast.JoinedStr) or (
                isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes))):
            hidden.add(name)
        elif any(isinstance(n, ast.Name) and (n.id == "__file__" or n.id in dangerous) for n in ast.walk(value)):
            hidden.add(name)  # a location (KIT_DIR, REPO_ROOT...): never shown, and its users are excluded
            bad.add(name)
        elif touches_io(value):
            bad.add(name)

    changed = True
    while changed:  # transitive: using an excluded kit name excludes the user
        changed = False
        for name, node in list(defs.items()) + list(consts.items()):
            if name in bad:
                continue
            if any(isinstance(sub, ast.Name) and sub.id in bad for sub in ast.walk(node)):
                bad.add(name)
                changed = True

    allowed = {name for name in public if name not in bad and name not in hidden}
    override_allow = _literal_names(tree, "MESHGEN_ALLOW")
    if override_allow is not None:
        allowed = {name for name in override_allow if name in public}
    allowed -= set(_literal_names(tree, "MESHGEN_EXCLUDE") or ())
    excluded = frozenset(public - allowed)
    return frozenset(allowed), excluded
