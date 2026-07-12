"""import_autofix — patch the single most common breakout-class solver failure:
a generated solution calls `random.choice(...)` (or `os.path.join`, `re.match`,
a `typing` annotation, ...) and never imported the module.

grounding.run_code already grounds pass/fail in a real traceback; this module
reads that traceback, recognizes an un-imported stdlib name, and inserts the
missing `import` (or `from X import Y`) line — so solver.solve()'s repair loop
can retry with the trivial fix already applied instead of spending a whole
generation asking the model to notice it forgot an import.

Stdlib only. No model/GPU dependency, so both entry points are pure functions
of (code, traceback_text) and are trivially unit-testable.
"""
import re

# --- name -> (module, import statement) coverage -----------------------------

# Modules used as `modname.attr(...)` — the bare module name itself is what's
# missing, so `import modname` fixes it.
_DIRECT_MODULES = {"random", "math", "os", "sys", "json", "re", "collections",
                    "itertools", "time"}

# Symbols commonly used un-imported via `from module import symbol`, where the
# NameError names the symbol, not the module.
_SYMBOL_IMPORTS = {
    # typing
    "Any": ("typing", "from typing import Any"),
    "Callable": ("typing", "from typing import Callable"),
    "Dict": ("typing", "from typing import Dict"),
    "List": ("typing", "from typing import List"),
    "Optional": ("typing", "from typing import Optional"),
    "Sequence": ("typing", "from typing import Sequence"),
    "Set": ("typing", "from typing import Set"),
    "Tuple": ("typing", "from typing import Tuple"),
    "Type": ("typing", "from typing import Type"),
    "TypeVar": ("typing", "from typing import TypeVar"),
    "Union": ("typing", "from typing import Union"),
    "NamedTuple": ("typing", "from typing import NamedTuple"),
    "TYPE_CHECKING": ("typing", "from typing import TYPE_CHECKING"),
    # collections
    "OrderedDict": ("collections", "from collections import OrderedDict"),
    "defaultdict": ("collections", "from collections import defaultdict"),
    "namedtuple": ("collections", "from collections import namedtuple"),
    "Counter": ("collections", "from collections import Counter"),
    "deque": ("collections", "from collections import deque"),
    # itertools
    "chain": ("itertools", "from itertools import chain"),
    "count": ("itertools", "from itertools import count"),
    "cycle": ("itertools", "from itertools import cycle"),
    "repeat": ("itertools", "from itertools import repeat"),
    "product": ("itertools", "from itertools import product"),
    "permutations": ("itertools", "from itertools import permutations"),
    "combinations": ("itertools", "from itertools import combinations"),
    "groupby": ("itertools", "from itertools import groupby"),
    "islice": ("itertools", "from itertools import islice"),
}

_NAME_ERROR_RE = re.compile(r"NameError: name ['\"](\w+)['\"] is not defined")
_PYGAME_MATH_ATTR_RE = re.compile(
    r"AttributeError: module ['\"]pygame(?:\.math)?['\"] has no attribute ['\"](cos|sin|tan|radians|degrees|atan2|sqrt)['\"]"
)
_PYGAME_MATH_ATTRS = {"cos", "sin", "tan", "radians", "degrees", "atan2", "sqrt"}


def _undefined_name(traceback_text):
    """Return the last (innermost-frame) undefined name in a NameError
    traceback, or None if the text has no NameError at all."""
    matches = _NAME_ERROR_RE.findall(traceback_text or "")
    return matches[-1] if matches else None


def _classify(name):
    """name -> (modname, import_statement), or (None, None) if unsupported."""
    if name in _DIRECT_MODULES:
        return name, "import %s" % name
    if name in _SYMBOL_IMPORTS:
        return _SYMBOL_IMPORTS[name]
    return None, None


def _already_imported(code, modname, name):
    """True if `code` already has an import that would define `name`."""
    mod_re = re.compile(r"^\s*import\s+%s\b" % re.escape(modname))
    from_re = re.compile(r"^\s*from\s+%s\s+import\b.*\b%s\b" % (re.escape(modname), re.escape(name)))
    for line in (code or "").splitlines():
        if mod_re.match(line) or from_re.match(line):
            return True
    return False


def detect_missing_import(traceback_text):
    """traceback_text (str) -> the stdlib module name a NameError in it is
    missing (e.g. "random", "typing"), or None if there's no NameError or the
    undefined name isn't one of the covered stdlib names."""
    name = _undefined_name(traceback_text)
    if name is None:
        return None
    modname, _ = _classify(name)
    return modname


def fix_missing_imports(code, traceback_text):
    """code, traceback_text -> code with the missing stdlib import prepended.

    Returns `code` unchanged if the traceback has no recognized missing
    import, or if `code` already imports it (nothing left to fix).
    """
    name = _undefined_name(traceback_text)
    if name is None:
        return code
    modname, stmt = _classify(name)
    if stmt is None or _already_imported(code, modname, name):
        return code
    return stmt + "\n" + (code or "")


def _has_import(code, modname):
    pattern = re.compile(r"^\s*import\s+%s\b" % re.escape(modname))
    return any(pattern.match(line) for line in (code or "").splitlines())


def fix_wrong_module_attrs(code, traceback_text):
    """Patch common generated-code calls to APIs on the wrong module.

    The game ladder often catches `pygame.cos(...)` / `pygame.radians(...)`.
    Those are math APIs, not pygame APIs. Replace supported pygame math calls
    with `math.*` and add `import math` if needed.
    """
    if not _PYGAME_MATH_ATTR_RE.search(traceback_text or ""):
        return code
    fixed = code or ""
    for attr in _PYGAME_MATH_ATTRS:
        fixed = re.sub(r"\bpygame\.%s\b" % re.escape(attr), "math.%s" % attr, fixed)
        fixed = re.sub(r"\bpygame\.math\.%s\b" % re.escape(attr), "math.%s" % attr, fixed)
    if fixed != (code or "") and not _has_import(fixed, "math"):
        fixed = "import math\n" + fixed
    return fixed


def fix_common_generation_errors(code, traceback_text):
    """Apply safe, local mechanical fixes before spending a model repair turn."""
    fixed = fix_missing_imports(code, traceback_text)
    fixed = fix_wrong_module_attrs(fixed, traceback_text)
    return fixed


# --- C++: compile-error-driven missing-header fixes ---------------------------
# The C++ game forge hits the same failure class as the Python NameError path:
# generated code calls assert()/sqrt()/memcpy() without the standard header.
# These helpers read the REAL compiler diagnostic (g++/clang++/MSVC) and prepend
# only the headers the compiler itself complained about — same contract as
# fix_missing_imports: pure function of (code, compiler_output), never raises.

_CPP_SYMBOL_HEADERS = {
    "assert": "cassert",
    # <cmath>
    "sqrt": "cmath", "sin": "cmath", "cos": "cmath", "tan": "cmath",
    "atan": "cmath", "atan2": "cmath", "fabs": "cmath", "pow": "cmath",
    "floor": "cmath", "ceil": "cmath", "fmod": "cmath", "hypot": "cmath",
    "round": "cmath",
    # <cstring>
    "memcpy": "cstring", "memset": "cstring", "memmove": "cstring",
    "strcpy": "cstring", "strncpy": "cstring", "strlen": "cstring",
    "strcmp": "cstring", "strncmp": "cstring", "strcat": "cstring",
    # <cstdio>
    "printf": "cstdio", "fprintf": "cstdio", "sprintf": "cstdio",
    "snprintf": "cstdio", "puts": "cstdio",
    # <cstdlib>
    "malloc": "cstdlib", "calloc": "cstdlib", "realloc": "cstdlib",
    "free": "cstdlib", "rand": "cstdlib", "srand": "cstdlib",
    "exit": "cstdlib", "atoi": "cstdlib", "atof": "cstdlib",
    # <algorithm>
    "sort": "algorithm", "max": "algorithm", "min": "algorithm",
    "clamp": "algorithm", "find": "algorithm", "fill": "algorithm",
    "max_element": "algorithm", "min_element": "algorithm",
    "reverse": "algorithm",
    # containers / strings / streams
    "string": "string", "to_string": "string", "getline": "string",
    "stoi": "string", "stod": "string",
    "vector": "vector", "array": "array",
    "map": "map", "multimap": "map", "unordered_map": "unordered_map",
    "pair": "utility", "make_pair": "utility", "move": "utility",
    "swap": "utility",
    "setw": "iomanip", "setprecision": "iomanip", "setfill": "iomanip",
    "cout": "iostream", "cerr": "iostream", "cin": "iostream",
    "endl": "iostream",
    "ifstream": "fstream", "ofstream": "fstream", "fstream": "fstream",
    "stringstream": "sstream", "ostringstream": "sstream",
    "istringstream": "sstream",
    # misc stdlib
    "size_t": "cstddef",
    "uint8_t": "cstdint", "uint16_t": "cstdint", "uint32_t": "cstdint",
    "uint64_t": "cstdint", "int8_t": "cstdint", "int16_t": "cstdint",
    "int32_t": "cstdint", "int64_t": "cstdint",
    "numeric_limits": "limits", "accumulate": "numeric", "iota": "numeric",
    "unique_ptr": "memory", "shared_ptr": "memory",
    "make_unique": "memory", "make_shared": "memory",
    "mt19937": "random", "uniform_int_distribution": "random",
    "uniform_real_distribution": "random",
    "chrono": "chrono", "filesystem": "filesystem",
}

# g++/clang++/MSVC "missing declaration" diagnostics, one capture group each.
_CPP_UNDECLARED_RES = (
    # g++: error: 'assert' was not declared in this scope
    re.compile(r"'([A-Za-z_][A-Za-z0-9_:]*)' was not declared in this scope"),
    # g++: error: 'filesystem' has not been declared
    re.compile(r"error: '([A-Za-z_][A-Za-z0-9_:]*)' has not been declared"),
    # g++/clang++: error: 'sort' is not a member of 'std'
    re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)' is not a member of '?std'?"),
    # clang++: use of undeclared identifier 'memcpy'
    re.compile(r"use of undeclared identifier '([A-Za-z_][A-Za-z0-9_:]*)'"),
    # MSVC: error C3861: 'sqrt': identifier not found
    re.compile(r"error C3861: '([A-Za-z_][A-Za-z0-9_]*)': identifier not found"),
    # MSVC: error C2039: 'sort': is not a member of 'std'
    re.compile(r"error C2039: '([A-Za-z_][A-Za-z0-9_]*)'"),
)


def _cpp_missing_symbols(compiler_output):
    """Yield undeclared symbol names from real compiler output, in order,
    with any std:: qualifier stripped."""
    seen = []
    for pattern in _CPP_UNDECLARED_RES:
        for name in pattern.findall(compiler_output or ""):
            name = name.rsplit("::", 1)[-1]
            if name and name not in seen:
                seen.append(name)
    return seen


def _cpp_has_include(code, header):
    return re.search(
        r"^\s*#\s*include\s*<%s>" % re.escape(header), code or "", re.M,
    ) is not None


def fix_cpp_missing_headers(code, compiler_output):
    """code, compiler_output -> code with missing standard headers prepended.

    Returns `code` unchanged when the compiler output names no covered symbol
    or every needed header is already included. Only ever adds `#include <...>`
    lines for the C++ standard library — never third-party headers.
    """
    includes = []
    for name in _cpp_missing_symbols(compiler_output):
        header = _CPP_SYMBOL_HEADERS.get(name)
        if not header:
            continue
        line = "#include <%s>" % header
        if line not in includes and not _cpp_has_include(code, header):
            includes.append(line)
    if not includes:
        return code
    return "\n".join(includes) + "\n" + (code or "")


_CPP_ERROR_LINE_RE = re.compile(
    r"(?:\berror\s*:|\berror C\d{4}\b|\bfatal error\b|undefined reference)", re.I,
)


def distill_cpp_errors(text, limit=1800):
    """Reduce raw compiler output to its actual error lines so a bounded
    repair-note truncation cannot cut the error off behind pages of warnings.
    Falls back to the (truncated) original text when no error line is found."""
    lines = [ln for ln in (text or "").splitlines() if _CPP_ERROR_LINE_RE.search(ln)]
    distilled = "\n".join(lines).strip()
    if not distilled:
        return (text or "")[:limit]
    return distilled[:limit]
