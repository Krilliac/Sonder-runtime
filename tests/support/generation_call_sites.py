"""Every place the runtime can generate with a model, and how each one is classified.

The tool-lane deny list is enforced at the bottom of the generation stack:
``server._chat_request`` refuses a denied model unless its caller declared the
call text-only (``text_only=True``).  ``tests/test_generation_call_sites.py``
walks the source tree with this module's scanner and fails when a generation
call site appears that ``REGISTRY`` below does not classify, so a new lane
cannot silently opt out of the check.

A *site* is one reference, in one enclosing function, to one of:

* a choke point of the generation stack (``CHOKE_POINTS``);
* a text entry point whose reply a caller may run or apply (``TEXT_ENTRY_POINTS``);
* a raw model HTTP path (``ENDPOINT_PATHS``) in a string without whitespace;
* a two-argument ``.generate(request, context)`` call under ``sonder_runtime``
  (the packaged ``ModelGateway`` port);
* the raw (unwrapped) packaged gateway handle in the composition root.

References are calls, bare names (``chat_request=_chat_request``), imports
and the string spelling ``getattr(server, "_make_generate")``.  The registry
key is ``(path, enclosing qualified name, callee)`` with the number of
references; no line numbers, so unrelated edits never disturb it.

Classes:

``TEXT``
    The reply is only returned, shown, stored as prose or used to rank; never
    run, applied, written as code or turned into a tool call.  A TEXT site of
    a choke point passes a literal ``text_only=True``.
``EXEC``
    The reply may be run, applied, written or turned into a tool call or task.
    A choke-point EXEC site leaves the default (refuse a denied model).  An
    EXEC site of a text entry point or a raw HTTP path, which the bottom check
    does not see, sits in a function (or inside one) that calls a refusal from
    ``tool_lane_guard`` first.
``CHOKE``
    The generation stack itself: the wrappers that forward ``text_only``, the
    recursions, imports, re-exports and the guard's own plumbing.
``ACCEPTED``
    A named exception with its reason (for example output that is applied only
    after the user confirms it).
``GAP``
    Known to execute model output without a refusal.  The test allows only the
    entries in ``ALLOWED_GAPS``.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CHOKE_POINTS = (
    "_post_model",
    "_chat_request",
    "_chat_request_with_cloud_fallback",
    "_make_generate",
    "_make_tier_generate",
    "_generate_text",
    "_gateway_generate_text",
)
TEXT_ENTRY_POINTS = (
    "sonder",
    "answer_with_history",
    "structured_answer_with_history",
    "ensemble_answer",
    "consult",
    "_offload_impl",
    "model_fanout",
)
ENDPOINT_PATHS = ("/api/chat", "/api/generate", "/v1/chat/completions")
GATEWAY_GENERATE = ".generate(request, context)"
# The composition root's name for the unwrapped packaged gateway.
RAW_GATEWAY_FILE = "sonder_runtime/bootstrap/app.py"
RAW_GATEWAY_NAME = "gateway"
# Calls that count as an early tool-lane refusal for an EXEC consumer of a
# text entry point or a raw HTTP path.
REFUSAL_CALLS = frozenset({
    "selector_refusal", "resolved_refusal", "denied", "refusal",
    "gateway_refusal", "require_generation",
})
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
    refused: bool = False       # an enclosing function calls a refusal first


@dataclass(frozen=True)
class Entry:
    count: int
    cls: str
    reason: str
    # ``path::qualname`` of functions that refuse before this site is reached,
    # for an EXEC consumer whose refusal sits in its callers.
    guarded_by: tuple[str, ...] = ()


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
        self._refusing: dict[int, bool] = {}
        # Module-level helpers in this file that call a refusal themselves
        # (``nightly_selfmod._model_refusal``) count as a refusal one level up.
        self.refusal_names = REFUSAL_CALLS | {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(inner, ast.Call) and _called_name(inner) in REFUSAL_CALLS
                for inner in ast.walk(node)
            )
        }

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

    def _function_refuses(self, function: ast.AST) -> bool:
        key = id(function)
        if key not in self._refusing:
            self._refusing[key] = any(
                isinstance(node, ast.Call) and _called_name(node) in self.refusal_names
                for node in ast.walk(function)
            )
        return self._refusing[key]

    def _record(self, callee: str, kind: str, node: ast.AST, call: ast.Call | None) -> None:
        site = self.sites.setdefault((self.path, self.qualname, callee), Site())
        site.count += 1
        site.kinds.add(kind)
        if _passes_text_only(call):
            site.text_only += 1
        if any(self._function_refuses(function) for function in self.functions):
            site.refused = True

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
        if name in CHOKE_POINTS or name in TEXT_ENTRY_POINTS:
            kind, call = self._owning_call(node)
            self._record(name, kind, node, call)
        elif (
            name == RAW_GATEWAY_NAME and self.path == RAW_GATEWAY_FILE
            and self.qualname.split(".")[0] == "build_application"
            and isinstance(node, ast.Name)
        ):
            kind, call = self._owning_call(node)
            self._record("raw-gateway:" + name, kind, node, call)

    def visit_Name(self, node):
        self._name(node.id, node)

    def visit_Attribute(self, node):
        self._name(node.attr, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name in CHOKE_POINTS or alias.name in TEXT_ENTRY_POINTS:
                self._record(alias.name, "import", node, None)

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute) and node.func.attr == "generate"
            and len(node.args) == 2 and not node.keywords
            and self.path.startswith("sonder_runtime/")
        ):
            self._record(GATEWAY_GENERATE, "call", node, node)
        if _passes_text_only(node):
            self.text_only_calls.append((self.path, self.qualname, _called_name(node)))
        if (
            _called_name(node) in ("getattr", "hasattr", "setattr") and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in TEXT_ENTRY_POINTS
        ):
            # A text entry point spelled as a string is a site only when looked up.
            self._record(node.args[1].value, "str", node.args[1], None)
        self.generic_visit(node)

    def visit_Constant(self, node):
        value = node.value
        if not isinstance(value, str) or id(node) in self.docstrings:
            return
        if value in CHOKE_POINTS:  # getattr(server, "_make_generate"), __all__, setattr
            self._record(value, "str", node, None)
            return
        if value.split() == [value]:
            for match in _ENDPOINT.finditer(value):
                self._record(match.group(1), "path", node, None)


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

# Until the generation stack takes ``text_only`` the literal-flag checks are
# off; every other check (unlisted, stale, counts, choke points, floors) runs.
DECLARATIONS_ENFORCED = False

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
    ("curriculum_run.py", "practice_on", "sonder", 1, GAP,
     "runs the code in the default route's reply with no refusal"),
    ("eval_duel.py", "mk", "_make_generate", 1, EXEC, "duel answers are run against task checks"),
    ("eval_harness.py", "OllamaProvider.__init__", "_make_generate", 1, EXEC,
     "harness answers are scored by running them"),
    ("eval_retrieval.py", "baseline_generate", "_make_generate", 1, EXEC,
     "the baseline answer's code is run against the task check"),
    ("eval_retrieval.py", "run_task", "sonder", 1, GAP,
     "runs the code in the reply with no refusal"),
    ("eval_solver.py", "_gen_fn.gen", "_make_generate", 1, EXEC, "solver attempts are run"),
    ("examples/codegen-arena-shooter/build_skeleton.py", "main", "ensemble_answer", 1, GAP,
     "writes and compiles the reply with no refusal"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "generate", "ensemble_answer", 1, GAP,
     "writes and compiles the reply with no refusal"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "repair", "ensemble_answer", 1, GAP,
     "writes and compiles the reply with no refusal"),
    ("examples/codegen-arena-shooter/build_with_sonder.py", "repair_lines", "ensemble_answer", 1, GAP,
     "writes and compiles the reply with no refusal"),
    ("game_ladder.py", "gen_fn", "sonder", 1, GAP, "writes and runs the reply with no refusal"),
    ("promotion_eval.py", "_local_ollama_url", "/api/generate", 1, GAP,
     "the generated SQL is run in an in-memory database with no refusal"),
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
     "the reply is spliced into source; _model_refusal runs first"),
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
    ("server.py", "_agent_turn", "_make_tier_generate", 1, EXEC, "agent decisions become tool calls"),
    ("server.py", "_answer", "_make_generate", 1, TEXT, "chat answer (sonder, by tier or by name)"),
    ("server.py", "_answer_with_history_impl", "_make_generate", 1, TEXT,
     "chat answer with history (MCP and HTTP chat)"),
    ("server.py", "_autopilot_json_model", "_make_tier_generate", 1, EXEC,
     "autopilot plans become tasks"),
    ("server.py", "_autopilot_review_model.generator_factory", "_make_tier_generate", 1, EXEC,
     "autopilot review verdicts steer applied work"),
    ("server.py", "_chat_request", "/api/chat", 1, CHOKE, "the Ollama chat transport"),
    ("server.py", "_chat_request", "_chat_request", 3, CHOKE,
     "continuation and retry recursions; forward text_only"),
    ("server.py", "_chat_request", "_post_model", 1, CHOKE, "below the check"),
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
    ("server.py", "_make_generate.gen", "_chat_request", 1, CHOKE, "forwards text_only"),
    ("server.py", "_make_generate.gen", "_chat_request_with_cloud_fallback", 1, CHOKE,
     "forwards text_only"),
    ("server.py", "_make_tier_generate", "_make_generate", 1, CHOKE,
     "factory for tier_generation; forwards every keyword"),
    ("server.py", "_maybe_title", "_gateway_generate_text", 1, TEXT, "session title"),
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
    ("server.py", "_vision_analyze_impl", "_chat_request", 1, TEXT, "image description"),
    ("server.py", "campaign_generate_compile_execute_record.run_one", "sonder", 1, EXEC, _REFUSES),
    ("server.py", "campaign_repo_repair.run_one", "sonder", 1, EXEC, _REFUSES),
    ("server.py", "codegen_build_loop", "ensemble_answer", 1, EXEC, _REFUSES),
    ("server.py", "consult", "consult", 1, TEXT, "consult answer"),
    ("server.py", "consult", "ensemble_answer", 1, TEXT, "consult answer"),
    ("server.py", "control_command", "ensemble_answer", 1, TEXT, "/ensemble answer"),
    ("server.py", "ensemble_answer", "/api/generate", 1, CHOKE, _UNLOAD),
    ("server.py", "ensemble_answer", "_make_tier_generate", 2, TEXT,
     "ensemble members and synthesis; the answer is returned"),
    ("server.py", "improve_function.ask", "ensemble_answer", 1, EXEC, _REFUSES),
    ("server.py", "offload", "_offload_impl", 1, TEXT, "offload answer"),
    ("server.py", "parallel_generate_run", "_make_tier_generate", 1, EXEC,
     "candidates are run; refuses a denied tier first"),
    ("server.py", "parallel_generate_run_languages.one", "_make_tier_generate", 1, EXEC,
     "candidates are run; refuses a denied tier first"),
    ("server.py", "sonder", "ensemble_answer", 1, TEXT, "sonder(tier='ensemble') answer"),
    ("server.py", "sonder", "model_fanout", 1, TEXT, "sonder fanout answer"),
    ("server.py", "structured_answer_with_history", "_make_generate", 1, TEXT,
     "structured answer, validated against the caller's schema"),
    ("server.py", "unload", "/api/generate", 1, CHOKE, _UNLOAD),
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
     "the unwrapped gateway: built once, wrapped for tool lanes, and handed to "
     "Application.model_gateway and ChatService"),
    ("sonder_runtime/bootstrap/build_tools.py", "_LazyModelGateway.generate", GATEWAY_GENERATE, 1, CHOKE,
     "lazy handle over the gateway build_fix is given"),
    ("sonder_runtime/bootstrap/mesh_agent_tools.py", "generate_mesh.generate", "_generate_text", 1, EXEC,
     "mesh code is run in a sandbox; refuses a denied tier first"),
    ("sonder_runtime/interfaces/http/facades/model_request.py", "<module>", "/v1/chat/completions", 1, TEXT,
     _OWN_CHAT),
    ("sonder_runtime/interfaces/http/facades/model_request.py", "ModelRequestFacade.invoke.complete",
     GATEWAY_GENERATE, 1, TEXT, "HTTP model request; the reply is returned to the client"),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._auth_rate_limited", "/v1/chat/completions", 1,
     TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._handle_post_request", "/v1/chat/completions", 3,
     TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._handle_post_request", "ensemble_answer", 1, TEXT,
     "natural-language ensemble reply"),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._record_chat_completion_metric",
     "/v1/chat/completions", 2, TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "Handler._reject_disallowed_origin", "/v1/chat/completions",
     1, TEXT, _OWN_CHAT),
    ("sonder_runtime/interfaces/http/serve.py", "_do_train", "sonder", 1, GAP,
     "runs the code in the default route's reply with no refusal"),
    ("sonder_runtime/interfaces/http/serve.py", "_handle_slash", "ensemble_answer", 1, TEXT, "/ensemble answer"),
    ("sonder_runtime/interfaces/http/serve.py", "_run_prompt", "answer_with_history", 1, TEXT, "HTTP chat"),
    ("sonder_runtime/interfaces/http/serve.py", "_run_structured_prompt", "structured_answer_with_history", 1,
     TEXT, "HTTP structured chat"),
    ("sonder_runtime/interfaces/repl/repl.py", "<module>", "consult", 1, CHOKE,
     "imports the consult service provider; not a call"),
    ("sonder_runtime/interfaces/repl/repl.py", "_run_train", "sonder", 1, GAP,
     "runs the code in the default route's reply with no refusal"),
    ("sonder_runtime/interfaces/repl/repl.py", "main", "sonder", 1, TEXT, "REPL chat"),
    ("sonder_runtime/interfaces/repl/repl.py", "main.do_consult", "consult", 1, TEXT, "/consult answer"),
    ("sonder_runtime/interfaces/repl/repl.py", "main.do_refactor", "ensemble_answer", 1, ACCEPTED,
     "/refactor shows the diff and writes only after the user confirms it (same standing as /run)"),
]

REGISTRY: dict[tuple[str, str, str], Entry] = {
    (path, qualname, callee): Entry(count, cls, reason, *extra)
    for path, qualname, callee, count, cls, reason, *extra in _ENTRIES
}

# GAP entries the ratchet still tolerates; every other GAP entry fails.
ALLOWED_GAPS: frozenset[tuple[str, str, str]] = frozenset(
    key for key, entry in REGISTRY.items() if entry.cls == GAP
)

# ``text_only=True`` passed somewhere other than a registered TEXT site.
TEXT_ONLY_DECLARATIONS: dict[tuple[str, str, str], str] = {}


def function_refuses(path: str, qualname: str) -> bool | None:
    """Whether ``qualname`` in ``path`` calls a refusal; None when it does not exist."""
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8-sig"), filename=path)
    scanner = _Scanner(path, tree)

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if name == qualname and not isinstance(node, ast.ClassDef):
                    return scanner._function_refuses(node)
                found = walk(node.body, name + ".")
                if found is not None:
                    return found
        return None

    return walk(tree.body, "")


def needs_refusal(callee: str) -> bool:
    """EXEC sites the bottom check cannot see: text entry points, raw HTTP, unchecked choke points."""
    return (
        callee in TEXT_ENTRY_POINTS or callee in ENDPOINT_PATHS
        or callee in ("_post_model", "_gateway_generate_text")
    )


def entries(cls: str) -> Iterable[tuple[tuple[str, str, str], Entry]]:
    return ((key, entry) for key, entry in sorted(REGISTRY.items()) if entry.cls == cls)
