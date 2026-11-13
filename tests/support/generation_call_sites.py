"""Every place the runtime can generate with a model, and how each one is classified.

The tool-lane deny list is enforced at the bottom of the generation stack:
``server._chat_request`` refuses a denied model unless its caller declared the
call text-only (``text_only=True``).  ``tests/test_generation_call_sites.py``
walks the source tree with this module's scanner and fails when a generation
call site appears that ``REGISTRY`` below does not classify, so a new lane
cannot silently opt out of the check.

A *site* is one reference, in one enclosing function, to one of:

* a choke point of the generation stack (``CHOKE_POINTS``);
* a text entry point whose reply a caller may run or apply
  (``TEXT_ENTRY_POINTS``: every public or internal wrapper over a stack that
  declares ``text_only=True`` or sits below the check);
* a raw model HTTP path (``ENDPOINT_PATHS``) in a string without whitespace;
* a ``.generate(...)`` call with two or more positional arguments, or a
  ``request=`` / ``context=`` keyword, anywhere (the packaged ``ModelGateway``
  port);
* the unchecked packaged gateway: the ``raw_gateway`` name and any read of an
  ``.model_gateway`` attribute (``Application.model_gateway``).

References are calls, bare names (``chat_request=_chat_request``), imports,
names bound to one (``from server import ensemble_answer as ea``, ``ask =
server.offload``, ``functools.partial(server.offload)``) and the string
spelling ``getattr(server, "_make_generate")``.  A name a function binds
itself (a parameter or local variable that happens to be called ``offload``)
is not a reference.  The registry key is ``(path, enclosing qualified name,
callee)`` with the number of references; no line numbers, so unrelated edits
never disturb it.

Limit: a name computed at run time (``getattr(server, "ens" + "emble_answer")``)
cannot be seen statically; review has to catch it.

Classes:

``TEXT``
    The reply is only returned, shown, stored as prose or used to rank; never
    run, applied, written as code or turned into a tool call.  A TEXT site of
    a choke point passes a literal ``text_only=True``.
``EXEC``
    The reply may be run, applied, written or turned into a tool call or task.
    A choke-point EXEC site leaves the default (refuse a denied model).  An
    EXEC site of a text entry point or a raw HTTP path, which the bottom check
    does not see, is preceded by a refusal from ``tool_lane_guard`` (in its
    function, an enclosing one, or a ``guarded_by`` caller) whose result
    stops the function (the test of an ``if`` whose body returns, raises,
    continues or breaks, or a call to a same-file helper that raises) and
    which judges the selector the call passes: the same expression (``x or
    ""`` counts as ``x``); a call that passes none takes its default and is
    judged by ``""``, ``"sonder"`` or ``"local"``, which judges every
    configured local tier.  Where the judged expression differs by design the
    entry says why in ``selector_reason``.
``CHOKE``
    The generation stack itself: the wrappers that forward ``text_only``, the
    recursions, imports, re-exports and the guard's own plumbing.
``ACCEPTED``
    A named exception with its reason (for example output that is applied only
    after the user confirms it).
``GAP``
    Known to execute model output without a refusal.  The test allows only the
    entries in ``ALLOWED_GAPS``, which is empty: a new gap fails the ratchet.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# The real server source, for binding call arguments to entry-point
# parameters even when REPO_ROOT points at synthetic sources.
_SERVER_SOURCE = REPO_ROOT / "server.py"

CHOKE_POINTS = (
    "_post_model",
    "_chat_request",
    "_chat_request_with_cloud_fallback",
    "_make_generate",
    "_make_tier_generate",
    "_generate_text",
    "_gateway_generate_text",
)
# Every function over a stack that passes a literal ``text_only=True`` (or sits
# below the check), traced outward to its public callers.
TEXT_ENTRY_POINTS = (
    "sonder",
    "_sonder_impl",
    "_sonder_impl_serialized",
    "_answer",
    "answer_with_history",
    "_answer_with_history_impl",
    "structured_answer_with_history",
    "ensemble_answer",
    "consult",
    "offload",
    "_offload_impl",
    "_offload_tier_impl",
    "model_fanout",
    "_model_fanout_authorized",
    "_execute_fanout_run",
    "model_fanout_resume",
    "model_fanout_synthesize",
    "_fanout_synthesize_run",
    "_fanout_synthesis_generate",
    "vision_analyze",
    "_vision_analyze_impl",
    "_internal_generate_for_route",
)
# Parameters of a text entry point that choose the model or tier.
SELECTOR_PARAMS = frozenset({
    "tier", "tiers", "synth_tier", "model_override", "model", "scope", "profile", "synth_model",
})
# A refusal argument that judges the default route (every configured local tier).
DEFAULT_ROUTE_SELECTORS = frozenset({"''", "'sonder'", "'local'"})
DEFAULT = "<default>"
UNKNOWN = "<unknown selector>"
ENDPOINT_PATHS = ("/api/chat", "/api/generate", "/v1/chat/completions")
GATEWAY_GENERATE = ".generate(request, context)"
# The unchecked packaged gateway.  ``bootstrap/app.py`` builds it once as
# ``raw_gateway`` and hands it to ``Application.model_gateway``; every use of
# either name is counted, so handing it to one more consumer fails the ratchet.
RAW_GATEWAY_FILE = "sonder_runtime/bootstrap/app.py"
RAW_GATEWAY_NAME = "raw_gateway"
RAW_GATEWAY_ATTRIBUTE = "model_gateway"
# Calls that count as an early tool-lane refusal for an EXEC consumer of a
# text entry point or a raw HTTP path.
REFUSAL_CALLS = frozenset({
    "selector_refusal", "resolved_refusal", "denied", "refusal",
    "gateway_refusal", "require_generation", "default_route_refusal", "prompt_refusal",
})
# Refusals whose first argument is not a selector (``prompt_refusal`` judges
# the model request in the prompt's own text).
_NON_SELECTOR_REFUSALS = frozenset({"prompt_refusal"})
_STOPS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
_ENDPOINT = re.compile(
    r"(" + "|".join(re.escape(path) for path in ENDPOINT_PATHS) + r")(?![\w/])"
)

# (root label, glob pattern relative to REPO_ROOT, minimum file count)
ROOTS = (
    ("server.py", "server.py", 1),
    ("root", "*.py", 120),
    ("scripts", "scripts/**/*.py", 50),
    ("examples", "examples/**/*.py", 6),
    ("sonder_runtime", "sonder_runtime/**/*.py", 1250),
)
DOC_POINTER = "docs/wiki/03-configuration.md (Tool-lane deny list: classifying a generation call site)"


@dataclass
class Site:
    count: int = 0
    kinds: set[str] = field(default_factory=set)
    text_only: int = 0          # references that pass a literal text_only=True
    refused: bool = False       # every reference follows a refusal that stops its function
    judged: bool = False        # ... one that judges the selector the reference passes
    selectors: set[str] = field(default_factory=set)  # selector expressions the references pass


@dataclass(frozen=True)
class Entry:
    count: int
    cls: str
    reason: str
    # ``path::qualname`` of functions that refuse before this site is reached,
    # for an EXEC consumer whose refusal sits in its callers.
    guarded_by: tuple[str, ...] = ()
    # Why the refusal rightly judges a different expression than the selector
    # the call passes (an EXEC consumer whose site is not ``judged``).
    selector_reason: str = ""


def source_files() -> dict[str, list[Path]]:
    """Files under each root (tests excluded); the ratchet records a floor per root."""
    found: dict[str, list[Path]] = {}
    for label, pattern, _floor in ROOTS:
        paths = sorted(
            path for path in REPO_ROOT.glob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
            and not path.relative_to(REPO_ROOT).parts[0] == "tests"
        )
        if label == "root":
            paths = [path for path in paths if path.name != "server.py"]
        found[label] = paths
    return found


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _passes_text_only(call: ast.Call | None) -> bool:
    return call is not None and any(
        keyword.arg == "text_only" and _is_true(keyword.value) for keyword in call.keywords
    )


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(function: ast.AST) -> Iterable[ast.AST]:
    """Every node of ``function``'s own body, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _params(function: ast.AST) -> list[str]:
    args = function.args
    return [arg.arg for arg in (*args.posonlyargs, *args.args)]


def _all_params(function: ast.AST) -> set[str]:
    args = function.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(arg.arg for arg in (args.vararg, args.kwarg) if arg is not None)
    return names


def _bound_argument(function: ast.AST, call: ast.Call, param: str) -> ast.AST | None:
    """The expression ``call`` passes for ``function``'s parameter ``param``, or None."""
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
    positional = _params(function)
    if param in positional:
        index = positional.index(param)
        if index < len(call.args) and not any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1]):
            return call.args[index]
    return None


def _strip_default(expression: str) -> str:
    """``x or ""`` judges what ``x`` selects (an empty ``x`` is the default route)."""
    for suffix in (" or ''", ' or ""'):
        if expression.endswith(suffix):
            return expression[: -len(suffix)]
    return expression


def selector_matches(selector: str, judged: Iterable[str]) -> bool:
    """Whether a refusal that judged the expressions ``judged`` covers ``selector``."""
    judged = {_strip_default(item) for item in judged}
    if selector == DEFAULT or selector in DEFAULT_ROUTE_SELECTORS:
        return bool(judged & DEFAULT_ROUTE_SELECTORS)
    if selector == UNKNOWN:
        return False
    return _strip_default(selector) in judged


_SIGNATURES: dict[str, ast.AST] | None = None


def entry_signatures() -> dict[str, ast.AST]:
    """The ``server.py`` definitions of the text entry points (for argument binding)."""
    global _SIGNATURES
    if _SIGNATURES is None:
        path = _SERVER_SOURCE
        found: dict[str, ast.AST] = {}
        if path.is_file():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename="server.py")
            found = {
                node.name: node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TEXT_ENTRY_POINTS
            }
        _SIGNATURES = found
    return _SIGNATURES


def call_selectors(callee: str, call: ast.Call | None) -> set[str]:
    """The selector expressions one call of a text entry point passes (``DEFAULT`` for none)."""
    if call is None or callee not in TEXT_ENTRY_POINTS:
        return {UNKNOWN}
    if any(keyword.arg is None for keyword in call.keywords):
        return {UNKNOWN}  # **kwargs: cannot be read statically
    function = entry_signatures().get(callee)
    if function is None:
        return {UNKNOWN}
    selectors = set()
    for param in sorted(SELECTOR_PARAMS & _all_params(function)):
        bound = _bound_argument(function, call, param)
        if bound is not None:
            selectors.add(ast.unparse(bound))
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        selectors.add(UNKNOWN)
    return selectors or {DEFAULT}


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module):
        self.path = path
        self.tree = tree
        self.scope: list[str] = []
        self.functions: list[ast.AST] = []
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        self.sites: dict[tuple[str, str, str], Site] = {}
        self.definitions: set[str] = set()
        self.text_only_calls: list[tuple[str, str, str]] = []
        self._stopping: dict[int, list[tuple[int, set[str]]]] = {}
        self._locals: dict[int, set[str]] = {}
        self.helpers = {
            node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # Same-file helpers: one that returns a refusal's result is itself a
        # refusal (the caller's ``if`` must stop on it); one whose refusal
        # raises stops its caller by being called.
        self.returning_helpers: set[str] = set()
        self.raising_helpers: set[str] = set()
        for _ in range(2):  # a helper over a helper
            for name, node in self.helpers.items():
                if any(
                    isinstance(inner, ast.Return) and inner.value is not None
                    and self._contains_refusal(inner.value)
                    for inner in _own_nodes(node)
                ):
                    self.returning_helpers.add(name)
                if any(
                    isinstance(stop, ast.Raise)
                    for guard, _calls in self._guard_tests(node) for stop in self._stops_in(guard)
                ):
                    self.raising_helpers.add(name)
        # Names bound to a choke point or text entry point (import aliases and
        # simple assignments); a reference through one is a site of the original.
        self.aliases: dict[str, tuple[str, bool]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "server":
                for alias in node.names:
                    if alias.asname and self._tracked(alias.name):
                        self.aliases[alias.asname] = (alias.name, True)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                callee = self._referenced(node.value)
                if callee and node.targets[0].id != callee:
                    self.aliases[node.targets[0].id] = (callee, False)

    @staticmethod
    def _tracked(name: str) -> bool:
        return name in CHOKE_POINTS or name in TEXT_ENTRY_POINTS

    def _referenced(self, value: ast.AST) -> str:
        """The tracked callee an assignment's value names (``server.x``, ``x``, ``partial(server.x)``)."""
        if isinstance(value, ast.Call) and _called_name(value) == "partial" and value.args:
            value = value.args[0]
        if isinstance(value, ast.Attribute) and self._tracked(value.attr):
            return value.attr
        if isinstance(value, ast.Name) and self._tracked(value.id):
            return value.id
        return ""

    # -- refusals ------------------------------------------------------------
    def _is_refusal_call(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and (
            _called_name(node) in REFUSAL_CALLS or _called_name(node) in self.returning_helpers
        )

    def _contains_refusal(self, node: ast.AST) -> bool:
        return any(self._is_refusal_call(inner) for inner in ast.walk(node))

    @staticmethod
    def _stops_in(guard: ast.If) -> list[ast.AST]:
        return [
            inner for statement in guard.body for inner in (statement, *_own_nodes(statement))
            if isinstance(inner, _STOPS)
        ]

    def _guard_tests(self, function: ast.AST) -> list[tuple[ast.If, list[ast.Call]]]:
        """Each ``if`` in ``function`` whose test holds refusal calls, directly or through a name."""
        assigned: dict[str, list[ast.Call]] = {}
        found = []
        for node in sorted(
            (node for node in _own_nodes(function) if isinstance(node, (ast.Assign, ast.If))),
            key=lambda node: (node.lineno, node.col_offset),
        ):
            if isinstance(node, ast.Assign):
                calls = [inner for inner in ast.walk(node.value) if self._is_refusal_call(inner)]
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if calls:
                            assigned[target.id] = calls
                        else:
                            assigned.pop(target.id, None)
                continue
            calls = [inner for inner in ast.walk(node.test) if self._is_refusal_call(inner)]
            for inner in ast.walk(node.test):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in assigned:
                    calls.extend(assigned[inner.id])
            if calls:
                found.append((node, calls))
        return found

    def _judged_by(self, call: ast.Call, depth: int = 0) -> set[str]:
        """The selector expressions one refusal (or same-file helper) call judges."""
        name = _called_name(call)
        if name in _NON_SELECTOR_REFUSALS:
            return set()
        helper = self.helpers.get(name)
        if helper is not None and name not in REFUSAL_CALLS:
            if depth > 2:
                return set()
            judged = set()
            params = _all_params(helper)
            for inner in _own_nodes(helper):
                if not self._is_refusal_call(inner):
                    continue
                for expression in self._judged_by(inner, depth + 1):
                    if expression in params:
                        bound = _bound_argument(helper, call, expression)
                        if bound is not None:
                            judged.add(ast.unparse(bound))
                    else:
                        judged.add(expression)
            return judged
        if call.args and not isinstance(call.args[0], ast.Starred):
            return {ast.unparse(call.args[0])}
        for keyword in call.keywords:
            if keyword.arg in ("tiers", "tier", "selector", "model", "name"):
                return {ast.unparse(keyword.value)}
        return set()

    def stopping_refusals(self, function: ast.AST) -> list[tuple[int, set[str]]]:
        """``(line, judged selectors)`` of each refusal whose result stops ``function``."""
        key = id(function)
        if key not in self._stopping:
            found = []
            for guard, calls in self._guard_tests(function):
                if self._stops_in(guard):
                    for call in calls:
                        found.append((guard.lineno, self._judged_by(call)))
            for node in _own_nodes(function):
                if isinstance(node, ast.Call) and _called_name(node) in self.raising_helpers:
                    found.append((node.lineno, self._judged_by(node)))
            self._stopping[key] = found
        return self._stopping[key]

    def _locally_bound(self, function: ast.AST) -> set[str]:
        key = id(function)
        if key not in self._locals:
            names = _all_params(function)
            for node in _own_nodes(function):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    names.add(node.id)
            self._locals[key] = names
        return self._locals[key]

    # -- scope -------------------------------------------------------------
    def _enter(self, node, name):
        self.scope.append(name)
        if not isinstance(node, ast.ClassDef):
            self.functions.append(node)
        self.generic_visit(node)
        if not isinstance(node, ast.ClassDef):
            self.functions.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node):
        if not self.scope:
            self.definitions.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self.scope.append(node.name)
        self.functions.append(node)
        for statement in node.body:
            self.visit(statement)
        self.functions.pop()
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._enter(node, node.name)

    @property
    def qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _record(self, callee: str, kind: str, node: ast.AST, call: ast.Call | None,
                selectors: set[str] | None = None) -> None:
        site = self.sites.setdefault((self.path, self.qualname, callee), Site())
        first = site.count == 0
        site.count += 1
        site.kinds.add(kind)
        if _passes_text_only(call):
            site.text_only += 1
        if selectors is None:
            selectors = call_selectors(callee, call) if kind == "call" else {UNKNOWN}
        site.selectors |= selectors
        line = getattr(node, "lineno", 0)
        preceding = [
            judged for function in self.functions
            for at, judged in self.stopping_refusals(function) if at <= line
        ]
        refused = bool(preceding)
        judged = refused and all(
            any(selector_matches(selector, item) for item in preceding) for selector in selectors
        )
        site.refused = refused if first else site.refused and refused
        site.judged = judged if first else site.judged and judged

    def _owning_call(self, node: ast.AST) -> tuple[str, ast.Call | None]:
        """("call", call) when ``node`` is called, else ("ref", the call it is an argument of)."""
        parent = self.parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            return "call", parent
        while parent is not None and not isinstance(parent, (ast.Call, ast.stmt)):
            parent = self.parents.get(parent)
        return "ref", parent if isinstance(parent, ast.Call) else None

    # -- references --------------------------------------------------------
    def _name(self, name: str, node: ast.AST) -> None:
        if self._tracked(name):
            kind, call = self._owning_call(node)
            self._record(name, kind, node, call)

    def visit_Name(self, node):
        if node.id == RAW_GATEWAY_NAME:
            kind, call = self._owning_call(node)
            self._record("raw-gateway:" + RAW_GATEWAY_NAME, kind, node, call, {UNKNOWN})
            return
        alias = self.aliases.get(node.id)
        parent = self.parents.get(node)
        receiver = isinstance(parent, ast.Attribute) and parent.value is node
        if alias is not None and isinstance(node.ctx, ast.Load) and not receiver:
            callee, binds_arguments = alias
            kind, call = self._owning_call(node)
            self._record(callee, kind, node, call, None if binds_arguments else {UNKNOWN})
            return
        if self.functions and node.id in self._locally_bound(self.functions[-1]):
            return  # a parameter or local variable of that name, not the server function
        self._name(node.id, node)

    def visit_Attribute(self, node):
        if node.attr == RAW_GATEWAY_ATTRIBUTE and isinstance(node.ctx, ast.Load):
            kind, call = self._owning_call(node)
            self._record("raw-gateway:" + RAW_GATEWAY_ATTRIBUTE, kind, node, call, {UNKNOWN})
        else:
            self._name(node.attr, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if self._tracked(alias.name):
                self._record(alias.name, "import", node, None, {UNKNOWN})

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "generate":
            keywords = {keyword.arg for keyword in node.keywords}
            if len(node.args) >= 2 or keywords & {"request", "context"}:
                self._record(GATEWAY_GENERATE, "call", node, node, {UNKNOWN})
        if _passes_text_only(node):
            self.text_only_calls.append((self.path, self.qualname, _called_name(node)))
        if (
            _called_name(node) in ("getattr", "hasattr", "setattr") and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in TEXT_ENTRY_POINTS
        ):
            # A text entry point spelled as a string is a site only when looked up.
            self._record(node.args[1].value, "str", node.args[1], None, {UNKNOWN})
        self.generic_visit(node)

    def visit_Constant(self, node):
        value = node.value
        if not isinstance(value, str) or id(node) in self.docstrings:
            return
        if value in CHOKE_POINTS:  # getattr(server, "_make_generate"), __all__, setattr
            self._record(value, "str", node, None, {UNKNOWN})
            return
        if value.split() == [value]:
            for match in _ENDPOINT.finditer(value):
                self._record(match.group(1), "path", node, None, {UNKNOWN})


@dataclass
class Scan:
    sites: dict[tuple[str, str, str], Site]
    files: dict[str, list[Path]]
    definitions: dict[str, set[str]]
    text_only_calls: list[tuple[str, str, str]]
    unparseable: list[str]


def collect(files: dict[str, list[Path]] | None = None) -> Scan:
    files = source_files() if files is None else files
    sites: dict[tuple[str, str, str], Site] = {}
    definitions: dict[str, set[str]] = {}
    text_only_calls: list[tuple[str, str, str]] = []
    unparseable: list[str] = []
    for paths in files.values():
        for path in paths:
            rel = _rel(path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                unparseable.append("%s: %s" % (rel, exc))
                continue
            scanner = _Scanner(rel, tree)
            scanner.visit(tree)
            sites.update(scanner.sites)
            definitions[rel] = scanner.definitions
            text_only_calls.extend(scanner.text_only_calls)
    return Scan(sites, files, definitions, text_only_calls, unparseable)


# ---- the registry -----------------------------------------------------------

TEXT, EXEC, CHOKE, ACCEPTED, GAP = "TEXT", "EXEC", "CHOKE", "ACCEPTED", "GAP"
CLASSES = (TEXT, EXEC, CHOKE, ACCEPTED, GAP)

# Choke points that take ``text_only``: a TEXT site of one passes a literal
# ``text_only=True`` and an EXEC site never does.  ``_post_model`` (below the
# check) and ``_gateway_generate_text`` (the unwrapped packaged gateway, which
# declares itself text-only) cannot refuse, so an EXEC site of either needs an
# early refusal like a text entry point does.
FLAGGED_CHOKE_POINTS = (
    "_chat_request",
    "_chat_request_with_cloud_fallback",
    "_make_generate",
    "_make_tier_generate",
    "_generate_text",
)

# The literal-flag checks (TEXT passes text_only=True, EXEC never does, and no
# undeclared text_only=True anywhere) are on.
DECLARATIONS_ENFORCED = True

_UNLOAD = "unloads a model (keep_alive 0); generates nothing"
_LABEL = "route or metric label for this path; generates nothing"
_OWN_CHAT = "Sonder's own OpenAI-compatible chat surface (route matching); answers chat"
_WRAPPED = "holds the ToolLaneGateway that bootstrap/app.py hands tool and apply lanes"
_TRANSPORT = "provider transport inside the packaged gateway; judged by the wrapper or the bridge"
_PROBE = "readiness or conformance probe; the reply is compared, never run"
_SHOWN = "the reply is returned or shown as text"
_REFUSES = "runs or applies the reply; refuses a denied model first"

_ENTRIES = [
    # -- repository root -------------------------------------------------------
    ("consult.py", "_default_ask", "ensemble_answer", 1, TEXT, "consult flow: the answer is shown"),
    ("curriculum_run.py", "build_gen_fn", "_make_generate", 1, EXEC,
     "generated tasks carry check code that practice runs"),
    ("curriculum_run.py", "practice_on", "sonder", 1, EXEC,
     "runs the code in the default route's reply; refuses a denied route first"),
    ("eval_duel.py", "mk", "_make_generate", 1, EXEC, "duel answers are run against task checks"),
    ("eval_harness.py", "OllamaProvider.__init__", "_make_generate", 1, EXEC,
     "harness answers are scored by running them"),
    ("eval_retrieval.py", "baseline_generate", "_make_generate", 1, EXEC,
     "the baseline answer's code is run against the task check"),
    ("eval_retrieval.py", "run_task", "sonder", 1, EXEC,
     "runs the code in the reply; refuses a denied model or route first"),
    ("eval_solver.py", "_gen_fn.gen", "_make_generate", 1, EXEC, "solver attempts are run"),
    ("examples/codegen-arena-shooter/build_skeleton.py", "main", "ensemble_answer", 1, EXEC,
     "writes and compiles the reply; refuses a denied tier first"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "generate", "ensemble_answer", 1, EXEC,
     "writes and compiles the reply; refuses a denied tier first"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "repair", "ensemble_answer", 1, EXEC,
     "writes and compiles the reply; refuses a denied tier first"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "repair_lines", "ensemble_answer", 1, EXEC,
     "writes and compiles the reply; refuses a denied tier first"),
    ("game_ladder.py", "gen_fn", "sonder", 1, EXEC, "writes and runs the reply; refuses a denied route first"),
    ("promotion_eval.py", "_local_ollama_url", "/api/generate", 1, EXEC,
     "the generated SQL is run in an in-memory database; evaluate_model refuses a denied model first",
     ("promotion_eval.py::evaluate_model",),
     "a raw HTTP path carries no selector: evaluate_model judges the model it sends (`model`)"),
    ("verifiers.py", "llm_judge", "_make_generate", 1, TEXT, "the judge's verdict is parsed as a score"),
    # -- scripts ---------------------------------------------------------------
    ("scripts/audit_live_slash_commands.py", "main", "/v1/chat/completions", 1, TEXT,
     "audits Sonder's own chat surface; classifies the HTTP reply"),
    ("scripts/benchmark_moat.py", "default_model_fn", "_make_generate", 1, TEXT,
     "benchmark answers are scored as text"),
    ("scripts/benchmark_schema_offload.py", "build_live_call.call_fn", "_offload_impl", 1, TEXT,
     "structured offload output is compared with the expected fields"),
    ("scripts/nightly_self_improve.py", "_prewarm_code_model", "/api/chat", 1, TEXT,
     "readiness prewarm; the reply is discarded"),
    ("scripts/nightly_self_improve.py", "_prewarm_code_model", "_generate_text", 1, TEXT,
     "readiness prewarm; the reply is discarded"),
    ("scripts/nightly_selfmod.py", "_ask", "_make_generate", 1, EXEC,
     "the reply is spliced into source; _model_refusal runs first"),
    ("scripts/nightly_selfmod.py", "_ask", "ensemble_answer", 1, EXEC,
     "the reply is spliced into source; _model_refusal runs first", (),
     "the call passes tiers='code' through **kwargs; _model_refusal judges the 'code' tier "
     "when no model is named (the named-model branch generates through _make_generate)"),
    ("scripts/probe_slash_commands.py", "ask", "/v1/chat/completions", 1, TEXT,
     "probes Sonder's own chat surface; records the HTTP reply"),
    ("scripts/replay_failure_pitfall.py", "main", "_generate_text", 1, TEXT,
     "pitfall lesson text, stored as prose"),
    ("scripts/surface_sweep.py", "Sweep.boot", "_make_generate", 1, ACCEPTED,
     "replaces the generator with a stub that never calls a model (offline sweep)"),
    ("scripts/surface_sweep.py", "Sweep.sweep_http.post", "/v1/chat/completions", 1, TEXT,
     "sweeps Sonder's own chat surface; records the HTTP reply"),
    # -- server.py ---------------------------------------------------------------
    ("server.py", "_agent_negative_claim_review", "_make_tier_generate", 1, EXEC,
     "agent review verdict steers the tool loop"),
    ("server.py", "_agent_dispatch", "offload", 1, TEXT,
     "the agent offload tool: the answer returns to the agent as text, prompt input for its next step"),
    ("server.py", "_agent_turn", "_make_tier_generate", 1, EXEC, "agent decisions become tool calls"),
    ("server.py", "_answer", "_make_generate", 1, TEXT,
     ("chat answer (sonder, by tier or by name); handed to reference_lookup_lanes.chat_generate, "
      "which forwards text_only to the framed call")),
    ("server.py", "_answer_with_history_impl", "_make_generate", 1, TEXT,
     ("chat answer with history (MCP and HTTP chat); handed to reference_lookup_lanes.chat_generate, "
      "which forwards text_only to the framed call")),
    ("server.py", "_answer_with_history_impl", "_answer", 1, TEXT,
     "chat answer with history (MCP and HTTP chat), default route or by tier"),
    ("server.py", "_autopilot_json_model", "_make_tier_generate", 1, EXEC,
     "autopilot plans become tasks"),
    ("server.py", "_autopilot_review_model.generator_factory", "_make_tier_generate", 1, EXEC,
     "autopilot review verdicts steer applied work"),
    ("server.py", "_chat_request", "/api/chat", 1, CHOKE, "the Ollama chat transport"),
    ("server.py", "_chat_request", "_chat_request", 3, CHOKE,
     "continuation and retry recursions; forward text_only"),
    ("server.py", "_chat_request", "_post_model", 1, CHOKE, "below the check"),
    ("server.py", "_chat_request", "raw-gateway:" + RAW_GATEWAY_ATTRIBUTE, 2, CHOKE,
     "the legacy bridge to a non-Ollama rung, after require_generation; legacy_chat_bridge judges "
     "the served model the reply reports unless the call is text-only"),
    ("server.py", "_chat_request_with_cloud_fallback", "_chat_request", 1, CHOKE,
     "injected transport; forwards text_only, so the K3 fallback model is judged again"),
    ("server.py", "_codegen_critic_generation", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "_codegen_critic_generation", "_make_tier_generate", 1, EXEC,
     "codegen critic output steers generated code"),
    ("server.py", "_codegen_observed_generation", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "_codegen_observed_generation", "_make_tier_generate", 1, EXEC,
     "generated code is compiled and run"),
    ("server.py", "_codegen_pinned_generation", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "_codegen_pinned_generation", "_make_generate", 1, EXEC,
     "generated code is compiled and run"),
    ("server.py", "_execute_fanout_run.invoke", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "_execute_fanout_run.invoke", "_make_generate", 1, TEXT,
     "model_fanout rows: each answer is stored and shown"),
    ("server.py", "_execution_route_model", "_make_tier_generate", 1, EXEC,
     "route choice for execution; unclear, so left at the refusing default"),
    ("server.py", "_extract_grounded_impl", "_offload_impl", 1, TEXT,
     "grounded extraction returns structured fields"),
    ("server.py", "_fanout_synthesize_run", "_fanout_synthesis_generate", 1, TEXT, "fanout synthesis text"),
    ("server.py", "_fanout_synthesis_generate", "/api/chat", 1, TEXT, "fanout synthesis text"),
    ("server.py", "_fanout_synthesis_generate", "_post_model", 1, TEXT, "fanout synthesis text"),
    ("server.py", "_game_generate_result", "sonder", 1, EXEC,
     "game code is compiled and run; both callers refuse first",
     ("server.py::game_generate_and_test", "server.py::game_generation_campaign")),
    ("server.py", "_gate_answer_code._repair", "_make_generate", 1, EXEC, "repaired code is run"),
    ("server.py", "_generate_text", "_make_tier_generate", 1, CHOKE, "forwards text_only"),
    ("server.py", "_internal_generate_for_route", "_generate_text", 1, TEXT,
     "titles and history summaries"),
    ("server.py", "_internal_generate_for_route.generate", "_make_generate", 1, TEXT,
     "titles and history summaries on a cloud route"),
    ("server.py", "_loop_dispatch", "sonder", 1, TEXT, "loop 'sonder' action returns the text"),
    ("server.py", "_loop_dispatch", "offload", 1, TEXT, "loop 'offload' action returns the text"),
    ("server.py", "_make_generate.gen", "_chat_request", 1, CHOKE, "forwards text_only"),
    ("server.py", "_make_generate.gen", "_chat_request_with_cloud_fallback", 1, CHOKE,
     "forwards text_only"),
    ("server.py", "_make_tier_generate", "_make_generate", 1, CHOKE,
     "factory for tier_generation; forwards every keyword"),
    ("server.py", "_maybe_title", "_gateway_generate_text", 1, TEXT, "session title"),
    ("server.py", "_model_fanout_authorized", "_execute_fanout_run", 1, TEXT,
     "model_fanout rows: each answer is stored and shown"),
    ("server.py", "_offload_impl", "_offload_tier_impl", 1, TEXT, "offload answer"),
    ("server.py", "_offload_tier_impl", "_make_tier_generate", 2, TEXT, "offload answer"),
    ("server.py", "_offload_tier_impl.invoke_nonlearning", "_chat_request", 1, TEXT, "offload answer"),
    ("server.py", "_offload_tier_impl.invoke_nonlearning", "_chat_request_with_cloud_fallback", 1, TEXT,
     "offload answer"),
    ("server.py", "_orchestrator_worker.worker", "_offload_impl", 1, TEXT,
     "fleet text worker: its output is the fleet's text (repository workers use _agent_turn)"),
    ("server.py", "_post", "/api/chat", 1, CHOKE, "pool transport below _post_model"),
    ("server.py", "_post", "/api/generate", 1, CHOKE, "pool transport below _post_model"),
    ("server.py", "_post.send", "/api/chat", 1, CHOKE, "pool transport below _post_model"),
    ("server.py", "_post.send", "/api/generate", 1, CHOKE, "pool transport below _post_model"),
    ("server.py", "_prepare_lesson_candidate_bounded.generate", "_gateway_generate_text", 1, TEXT,
     "lesson text, stored as prose and injected into prompts"),
    ("server.py", "_record_failure_pitfall", "_gateway_generate_text", 1, TEXT,
     "pitfall text, stored as prose"),
    ("server.py", "_session_history_messages", "_gateway_generate_text", 1, TEXT, "history summary"),
    ("server.py", "_sonder_impl", "_sonder_impl_serialized", 1, TEXT, "sonder chat answer"),
    ("server.py", "_sonder_impl_serialized", "_answer", 1, TEXT,
     "sonder chat answer (default route with escalation, or by tier or name)"),
    ("server.py", "_sonder_impl_serialized", "_internal_generate_for_route", 1, TEXT,
     "titles and history summaries"),
    ("server.py", "answer_with_history", "_answer_with_history_impl", 1, TEXT, "chat answer with history"),
    ("server.py", "_vision_analyze_impl", "_chat_request", 1, TEXT, "image description"),
    ("server.py", "campaign_generate_compile_execute_record.run_one", "sonder", 1, EXEC, _REFUSES),
    ("server.py", "campaign_repo_repair.run_one", "sonder", 1, EXEC, _REFUSES),
    ("server.py", "codegen_build_loop", "ensemble_answer", 1, EXEC, _REFUSES, (),
     "generation_tiers is `tiers` or one of the repair tiers resolved from it; the refusal judges "
     "`tiers`, or every configured local tier when it is empty (the ensemble's default)"),
    ("server.py", "consult", "consult", 1, TEXT, "consult answer"),
    ("server.py", "consult", "ensemble_answer", 1, TEXT, "consult answer"),
    ("server.py", "control_command", "ensemble_answer", 1, TEXT, "/ensemble answer"),
    ("server.py", "control_command", "vision_analyze", 1, TEXT, "/vision answer"),
    ("server.py", "ensemble_answer", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "ensemble_answer", "_make_tier_generate", 3, TEXT,
     ("ensemble members and synthesis, and the factory handed to the shared reference lookup "
      "for its search-terms call (parsed into query words); the answer is returned")),
    ("server.py", "improve_function.ask", "ensemble_answer", 1, EXEC, _REFUSES, (),
     "`ask` is code_improve's ask_fn, called only with tier=chosen, the selector refused above"),
    ("server.py", "model_fanout", "_model_fanout_authorized", 1, TEXT, "model_fanout answer"),
    ("server.py", "model_fanout_resume", "_execute_fanout_run", 1, TEXT,
     "resumed fanout rows: each answer is stored and shown"),
    ("server.py", "model_fanout_synthesize", "_fanout_synthesize_run", 1, TEXT, "fanout synthesis text"),
    ("server.py", "offload", "_offload_impl", 1, TEXT, "offload answer"),
    ("server.py", "parallel_generate_run", "_make_tier_generate", 1, EXEC,
     "candidates are run; refuses a denied tier first"),
    ("server.py", "parallel_generate_run_languages.one", "_make_tier_generate", 1, EXEC,
     "candidates are run; refuses a denied tier first"),
    ("server.py", "sonder", "ensemble_answer", 1, TEXT, "sonder(tier='ensemble') answer"),
    ("server.py", "sonder", "model_fanout", 1, TEXT, "sonder fanout answer"),
    ("server.py", "sonder", "_sonder_impl", 1, TEXT, "sonder chat answer"),
    ("server.py", "structured_answer_with_history", "_make_generate", 1, TEXT,
     "structured answer, validated against the caller's schema"),
    ("server.py", "unload", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "vision_analyze", "_vision_analyze_impl", 1, TEXT, "image description"),
    # -- sonder_runtime ------------------------------------------------------------
    ("sonder_runtime/adapters/build/candidates.py", "ModelCandidateGenerator.propose",
     GATEWAY_GENERATE, 1, EXEC, "build_fix patches are applied; " + _WRAPPED),
    ("sonder_runtime/adapters/client_request.py", "build_chat_request", "/v1/chat/completions", 1, TEXT,
     "client of Sonder's own chat surface; prints the reply"),
    ("sonder_runtime/adapters/conversational_subagents.py", "conversational_runner_factory.bind.run",
     GATEWAY_GENERATE, 1, EXEC, "subagent turns call tools; " + _WRAPPED),
    ("sonder_runtime/adapters/inference/ollama_conformance.py", "OllamaConformanceProbe._chat",
     "/api/chat", 1, TEXT, _PROBE),
    ("sonder_runtime/adapters/inference/ollama_vision.py", "OllamaVisionGateway.analyze",
     "/api/chat", 1, TEXT, "image description"),
    ("sonder_runtime/adapters/inference/openai_compat_gateway.py", "OpenAICompatibleGateway._post",
     "/v1/chat/completions", 1, CHOKE, _TRANSPORT),
    ("sonder_runtime/adapters/inference/openai_compat_gateway.py", "OpenAICompatibleGateway.generate",
     "/v1/chat/completions", 1, CHOKE, _TRANSPORT),
    ("sonder_runtime/adapters/inference/openai_protocol_probe.py", "OpenAICompatibleProtocolProbe._request",
     "/v1/chat/completions", 1, TEXT, _PROBE),
    ("sonder_runtime/adapters/inference/openrouter_gateway.py", "OpenRouterGateway.generate",
     "/v1/chat/completions", 1, CHOKE, _TRANSPORT),
    ("sonder_runtime/adapters/inference/openrouter_gateway.py", "OpenRouterGateway.stream.worker",
     "/v1/chat/completions", 1, CHOKE, _TRANSPORT),
    ("sonder_runtime/adapters/inference/residency_feedback.py", "record_dispatched_context",
     "/api/chat", 1, CHOKE, _LABEL),
    ("sonder_runtime/adapters/inference/residency_feedback.py", "record_dispatched_context",
     "/api/generate", 1, CHOKE, _LABEL),
    ("sonder_runtime/adapters/inference/sonder_inference_gateway.py",
     "SonderInferenceGateway._post_with_busy_retry", "/v1/chat/completions", 1, CHOKE, _TRANSPORT),
    ("sonder_runtime/adapters/inference/sonder_inference_probe.py",
     "SonderInferenceProtocolProbe._request", "/v1/chat/completions", 1, TEXT, _PROBE),
    ("sonder_runtime/adapters/model_bootstrap.py", "LegacyModelBootstrapAdapter.make_generate",
     "_make_generate", 1, CHOKE, "OllamaGateway's generate factory; forwards every keyword"),
    ("sonder_runtime/adapters/provider_dispatch/fallback.py", "PreSendFallbackGateway.generate",
     GATEWAY_GENERATE, 2, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/adapters/provider_dispatch/gateway.py", "ProviderDispatchGateway.generate",
     GATEWAY_GENERATE, 1, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/adapters/provider_dispatch/gateway.py", "ProviderDispatchGateway.generate_strict_alias",
     GATEWAY_GENERATE, 1, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/adapters/repl_services.py", "<module>", "consult", 1, CHOKE,
     "service provider for the consult module; not a call"),
    ("sonder_runtime/adapters/tool_lane_gateway.py", "ToolLaneGateway.generate", GATEWAY_GENERATE, 1, CHOKE,
     "the wrapper itself: refuses before delegating"),
    ("sonder_runtime/application/agents/interactive_lanes.py", "AgentLaneService.run_pending",
     GATEWAY_GENERATE, 1, EXEC, "interactive lane steps call tools; " + _WRAPPED),
    ("sonder_runtime/application/chat/handle_chat.py", "ChatService.complete", GATEWAY_GENERATE, 1, TEXT,
     "packaged chat answer"),
    ("sonder_runtime/application/chat/provider_bridge.py", "generate_via_gateway", GATEWAY_GENERATE, 2, CHOKE,
     "the legacy bridge; legacy_chat_bridge judges the served model it reports"),
    ("sonder_runtime/application/model_batching.py", "generate_batch.invoke", GATEWAY_GENERATE, 1, CHOKE,
     "packaged gateway plumbing"),
    ("sonder_runtime/application/model_gateway/facade.py", "ModelGatewayFacade.generate",
     GATEWAY_GENERATE, 2, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/application/model_gateway/facade.py", "ModelGatewayFacade.generate_for_role",
     GATEWAY_GENERATE, 2, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/application/model_gateway/health_and_roles.py", "ModelGatewayContract.generate",
     GATEWAY_GENERATE, 1, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/application/prefix_cache_report.py", "prewarm_request", "/api/chat", 1, CHOKE,
     "prefix-cache prewarm; the reply is discarded"),
    ("sonder_runtime/application/prefix_cache_report.py", "prewarm_request", "/api/generate", 1, CHOKE,
     "prefix-cache prewarm; the reply is discarded"),
    ("sonder_runtime/application/routing/backend_conformance.py", "run_gateway_probes",
     GATEWAY_GENERATE, 3, TEXT, _PROBE),
    ("sonder_runtime/application/routing/evidence_gateway.py", "CapabilityEvidenceGateway.generate",
     GATEWAY_GENERATE, 4, CHOKE, "packaged gateway plumbing"),
    ("sonder_runtime/bootstrap/app.py", "build_application", "raw-gateway:" + RAW_GATEWAY_NAME, 4, CHOKE,
     "the unwrapped gateway: built once, wrapped as the default handle, and handed only "
     "to Application.model_gateway and ChatService (text consumers)"),
    ("sonder_runtime/bootstrap/build_tools.py", "_LazyModelGateway.generate", GATEWAY_GENERATE, 1, CHOKE,
     "lazy handle over the gateway build_fix is given"),
    ("sonder_runtime/bootstrap/mesh_agent_tools.py", "generate_mesh.generate", "_generate_text", 1, EXEC,
     "mesh code is run in a sandbox; refuses a denied tier first"),
    ("sonder_runtime/bootstrap/mesh_agent_tools.py", "generate_mesh.critique", "_vision_analyze_impl", 1, EXEC,
     "the vision critique steers the next round's script; a refused vision tier turns the critique off",
     (), "the call passes no selector: _vision_analyze_impl always answers on the vision tier, "
     "which the critique judges by name ('vision')"),
    ("sonder_runtime/interfaces/http/facades/model_request.py", "<module>", "/v1/chat/completions", 1, TEXT,
     _OWN_CHAT),
    ("sonder_runtime/interfaces/http/facades/model_request.py", "ModelRequestFacade.invoke.complete",
     GATEWAY_GENERATE, 1, TEXT, "HTTP model request; the reply is returned to the client"),
    ("sonder_runtime/interfaces/http/facades/fanout.py", "serve_post", "_fanout_synthesize_run", 1, TEXT,
     "HTTP fanout synthesis; the text is returned to the client"),
    ("sonder_runtime/interfaces/http/facades/fanout.py", "serve_post.resume", "_execute_fanout_run", 1, TEXT,
     "HTTP fanout resume; each answer is stored and returned"),
    ("sonder_runtime/interfaces/http/facades/observatory_routes.py", "ecosystem_document",
     "raw-gateway:" + RAW_GATEWAY_ATTRIBUTE, 1, CHOKE,
     "reads the gateway's provider status for the ecosystem document; generates nothing"),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._auth_rate_limited", "/v1/chat/completions", 1,
     TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._handle_post_request", "/v1/chat/completions", 3,
     TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._handle_post_request", "ensemble_answer", 1, TEXT,
     "natural-language ensemble reply"),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._handle_post_request", "_model_fanout_authorized", 1,
     TEXT, "HTTP fanout; the answers are returned to the client"),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._record_chat_completion_metric",
     "/v1/chat/completions", 2, TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._reject_disallowed_origin", "/v1/chat/completions",
     1, TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "_do_train", "sonder", 1, EXEC,
     "runs the code in the default route's reply; refuses a denied route first"),
    ("sonder_runtime/interfaces/http/serve.py", "_handle_slash", "ensemble_answer", 1, TEXT, "/ensemble answer"),
    ("sonder_runtime/interfaces/http/serve.py", "_run_prompt", "answer_with_history", 1, TEXT, "HTTP chat"),
    ("sonder_runtime/interfaces/http/serve.py", "_run_structured_prompt", "structured_answer_with_history", 1,
     TEXT, "HTTP structured chat"),
    ("sonder_runtime/interfaces/repl/repl.py", "<module>", "consult", 1, CHOKE,
     "imports the consult service provider; not a call"),
    ("sonder_runtime/interfaces/repl/repl.py", "_run_train", "sonder", 1, EXEC,
     "runs the code in the default route's reply; refuses a denied route first"),
    ("sonder_runtime/interfaces/repl/repl.py", "main", "sonder", 1, TEXT, "REPL chat"),
    ("sonder_runtime/interfaces/repl/repl.py", "main.do_consult", "consult", 1, TEXT, "/consult answer"),
    ("sonder_runtime/interfaces/repl/repl.py", "main.do_refactor", "ensemble_answer", 1, ACCEPTED,
     "/refactor shows the diff and writes only after the user confirms it (same standing as /run)"),
]

# Text entry points that only external clients call (MCP tools with no
# in-repository caller); every other entry point must be referenced somewhere.
UNREFERENCED_ENTRY_POINTS = frozenset({"model_fanout_resume", "model_fanout_synthesize"})

REGISTRY: dict[tuple[str, str, str], Entry] = {
    (path, qualname, callee): Entry(count, cls, reason, *extra)
    for path, qualname, callee, count, cls, reason, *extra in _ENTRIES
}

# GAP entries the ratchet tolerates: none.  Every GAP entry fails.
ALLOWED_GAPS: frozenset[tuple[str, str, str]] = frozenset()

# ``text_only=True`` passed somewhere other than a registered TEXT site.
TEXT_ONLY_DECLARATIONS: dict[tuple[str, str, str], str] = {
    ("sonder_runtime/adapters/inference/ollama_gateway.py", "OllamaGateway.generate", "_generate_factory"):
        "the packaged Ollama gateway: callers that run or apply output hold the "
        "ToolLaneGateway, which judged the request first",
    ("sonder_runtime/adapters/reference_lookup_lanes.py", "chat_generate.expander", "make_generate"):
        "reference lookup's search-terms call on the single-model chat lanes: the reply is only "
        "parsed into query words; make_generate is the lane's _make_generate",
    ("sonder_runtime/adapters/reference_lookup_multi.py", "SharedLookup.retrieve.expand", "make_tier_generate"):
        "reference lookup's search-terms call for ensemble_answer and consult members: the reply "
        "is only parsed into query words; make_tier_generate is the ensemble's _make_tier_generate",
}


def _find(path: str, qualname: str) -> tuple[_Scanner, ast.AST | None]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8-sig"), filename=path)
    scanner = _Scanner(path, tree)

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if name == qualname and not isinstance(node, ast.ClassDef):
                    return node
                found = walk(node.body, name + ".")
                if found is not None:
                    return found
        return None

    return scanner, walk(tree.body, "")


def guard_judgements(path: str, qualname: str) -> list[set[str]] | None:
    """The selectors judged by each refusal that stops ``qualname``; None when it does not exist."""
    scanner, function = _find(path, qualname)
    if function is None:
        return None
    return [judged for _line, judged in scanner.stopping_refusals(function)]


def function_refuses(path: str, qualname: str) -> bool | None:
    """Whether a refusal's result stops ``qualname`` in ``path``; None when it does not exist."""
    judgements = guard_judgements(path, qualname)
    return None if judgements is None else bool(judgements)


def needs_refusal(callee: str) -> bool:
    """EXEC sites the bottom check cannot see: text entry points, raw HTTP, unchecked choke points."""
    return (
        callee in TEXT_ENTRY_POINTS or callee in ENDPOINT_PATHS
        or callee in ("_post_model", "_gateway_generate_text")
        or callee.startswith("raw-gateway:")
    )


def entries(cls: str) -> Iterable[tuple[tuple[str, str, str], Entry]]:
    return ((key, entry) for key, entry in sorted(REGISTRY.items()) if entry.cls == cls)
