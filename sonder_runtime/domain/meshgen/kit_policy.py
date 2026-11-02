"""Which kit names a model-written script may touch.  Pure; stdlib only.

A kit is trusted Python the operator supplies, but its *public functions* are reachable from
untrusted scripts, and some of them write files (export, preview render, "run the whole prop").
This policy is a first filter (and keeps the prompt honest about what the harness does); the
OS sandbox in ``adapters/meshgen/sandbox_win.py`` is the real boundary.  Exposure is an
allow-list derived from the kit's own source:

* a function or class is EXCLUDED when anything in it (body, defaults, decorators, nested
  code) touches I/O or process control: ``open``/``exec``/``eval``, the ``os``, ``sys``,
  ``shutil``, ``pathlib``, ``subprocess``, ``io``, ``json``, ``socket``, ``ctypes`` modules,
  ``Path``, attribute names such as ``save``, ``write``, ``filepath``, ``makedirs``, ``exit``,
  ``export_scene``, ``import_scene``, ``render``, ``wm``, or any name the gate itself refuses;
* a module-level constant is excluded when it is a string/f-string/bytes literal, or built from
  anything excluded (``KIT_DIR``, ``REPO_ROOT``, ``PREVIEW_DIR`` ...);
* exclusion is transitive: a function that uses an excluded kit name is excluded too;
* the operator may override the derivation inside the kit with module-level literals:
  ``MESHGEN_ALLOW = ["box", "lathe"]`` replaces the derived allow-list (only public names the
  kit defines count) and ``MESHGEN_EXCLUDE = ["helper"]`` removes names from whatever the
  allow-list is.  Overrides are the operator's responsibility.
"""
from __future__ import annotations

import ast

DANGEROUS_MODULES = frozenset({
    "os", "sys", "shutil", "pathlib", "subprocess", "io", "socket", "ctypes", "json", "tempfile",
    "glob", "importlib", "builtins", "pickle", "marshal", "zipfile", "urllib", "http",
})
# Narrower than the script gate on purpose: the kit is trusted code that legitimately uses
# ``type()``, ``bmesh.types`` or ``bpy.data.objects.remove``.  Only file, process and
# script-execution touch points matter here.
_IO_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "exit", "quit", "Path", "PurePath",
})
# Attribute names that are only I/O on a dangerous receiver (``os.system``); generic elsewhere
# (``bpy.types.Operator.system`` style names must not exclude a harmless kit function).
_RECEIVER_ATTRIBUTES = frozenset({"system", "popen", "spawn", "startfile", "walk", "listdir", "remove", "unlink"})
_IO_ATTRIBUTES = frozenset({
    "save", "save_as", "save_render", "save_as_mainfile", "save_mainfile", "write", "writelines", "filepath",
    "filepath_raw", "filename", "directory", "export_scene", "import_scene", "open_mainfile", "read_homefile",
    "recover_last_session", "makedirs", "mkdir", "rmtree",
    "exit", "write_text", "write_bytes", "read_text", "read_bytes", "copyfile", "copy2",
    "load", "unpack", "execfile", "as_module", "texts", "libraries", "preferences", "handlers",
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
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and (sub.id in dangerous or sub.id in _IO_NAMES):
                return True
            if isinstance(sub, ast.Attribute):
                if sub.attr in _IO_ATTRIBUTES:
                    return True
                if sub.attr in _RECEIVER_ATTRIBUTES and isinstance(sub.value, ast.Name) and sub.value.id in dangerous:
                    return True
                if sub.attr == "render" and isinstance(sub.value, ast.Attribute) and sub.value.attr == "ops":
                    return True
        return False

    bad: set[str] = set()      # tainted: touches I/O, directly or through another bad name
    hidden: set[str] = set()   # text constants: not exposed, but harmless to the kit's own code
    for name, node in defs.items():
        if touches_io(node):
            bad.add(name)
    for name, value in consts.items():
        if isinstance(value, ast.JoinedStr) or (
                isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes))):
            hidden.add(name)
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
