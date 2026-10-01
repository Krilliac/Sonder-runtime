"""Golden compatibility checks for built-in tool behavior.

The baseline is the immutable origin/main policy at ``origin_sha``. Tests use
real classification/admission code, with inert tool invokers. Deliberate
concurrency restrictions are individually documented in tool-traits.md.
"""
from __future__ import annotations

import json
import ast
import sys
from itertools import chain
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import permission_modes
from sonder_runtime.domain.speculation_policy import SPECULATABLE_TOOLS, is_speculatable
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from sonder_runtime.domain.agent_mutation_policy import WORK_MUTATION_TOOLS
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway, ToolGatewayRequest, ToolInvocationOutput, ToolPermission, ToolScope,
)
from sonder_runtime.domain.loop_retry_policy import retry_decision
from sonder_speculation import SpeculationEngine


ROOT = Path(__file__).resolve().parent
RISK_FIXTURE = ROOT / "fixtures" / "builtin_risk_golden.json"
BEHAVIOR_FIXTURE = ROOT / "fixtures" / "builtin_behavior_golden.json"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_server_stub():
    """Provide only the registry seam while retaining real catalog derivation."""
    source_paths = [ROOT.parent / "server.py", ROOT.parent / "sonder_runtime" / "bootstrap" / "computer_use_tools.py"]
    from sonder_runtime.bootstrap.agent_help_tools import discover_agent_tool_registrars
    source_paths.extend(ROOT.parent / "sonder_runtime" / "bootstrap" / (name + ".py")
                        for name, _register in discover_agent_tool_registrars())
    tree = ast.parse("\n".join(path.read_text(encoding="utf-8") for path in source_paths))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            factory = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (isinstance(factory, ast.Attribute) and factory.attr == "tool"
                    and isinstance(factory.value, ast.Name) and factory.value.id == "mcp"):
                declared = next((keyword.value for keyword in decorator.keywords
                                 if keyword.arg == "name"), None) if isinstance(decorator, ast.Call) else None
                names.append(ast.literal_eval(declared) if declared is not None else node.name)
                break
    tools = [SimpleNamespace(name=name, description="", parameters={}) for name in sorted(set(names))]
    policies = {"_WORK_MUTATION_TOOLS": WORK_MUTATION_TOOLS}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {
                "_WORK_INSPECTION_TOOLS", "_UNCONFINED_ROOT_TOOLS", "REPOSITORY_READ_ONLY_TOOLS",
            }:
                policies[target.id] = frozenset(ast.literal_eval(node.value.args[0]))
    assert len(policies) == 4
    return SimpleNamespace(
        mcp=SimpleNamespace(_tool_manager=SimpleNamespace(list_tools=lambda: tools)),
        **policies,
    )


@pytest.fixture
def actual_catalog(monkeypatch):
    import sonder_runtime.adapters.command_catalog as catalog_module
    monkeypatch.setitem(sys.modules, "server", _catalog_server_stub())
    catalog_module.catalog.cache_clear()
    try:
        yield catalog_module.catalog()
    finally:
        catalog_module.catalog.cache_clear()


def test_builtin_risk_matches_origin_golden(actual_catalog):
    """Every catalog name retains the origin/main risk class.

    The command catalog is represented by a minimal module seam because the
    installed optional MCP SDK is older than this checkout's runtime SDK.  The
    production classifier itself is exercised; no classifier function is
    monkeypatched.
    """
    golden = _load(RISK_FIXTURE)

    actual_names = {command.name.lstrip("/") for command in actual_catalog}
    assert actual_names == set(golden), json.dumps({
        "removed": sorted(set(golden) - actual_names),
        "added": sorted(actual_names - set(golden)),
    })
    mismatches = {
        name: {"expected": expected, "actual": permission_modes.risk_of(name)}
        for name, expected in golden.items()
        if permission_modes.risk_of(name) != expected
    }
    assert not mismatches, "built-in risk drift: %s" % json.dumps(mismatches, sort_keys=True)


def test_builtin_speculation_allowlist_matches_origin_golden():
    golden = _load(BEHAVIOR_FIXTURE)
    expected = set(golden["speculatable"])
    assert SPECULATABLE_TOOLS == expected
    catalog = set(_load(RISK_FIXTURE))
    actual = {name for name in catalog if is_speculatable(name)}
    assert actual == expected, "speculation drift: %s" % json.dumps(
        {"missing": sorted(expected - actual), "added": sorted(actual - expected)},
        sort_keys=True,
    )


def test_native_traits_preserve_static_permission_grades(actual_catalog):
    """Typed aliases/static-only tools also pass descriptor traits to the gate."""
    from sonder_runtime.bootstrap.native_mcp import _GRADED_NAMES, native_tool_registry

    mismatches = {}
    for descriptor in native_tool_registry().list_all():
        name = _GRADED_NAMES.get(descriptor.name, descriptor.name)
        expected = permission_modes._catalog_risk_of(name)
        actual = permission_modes.risk_of(name, traits=descriptor.traits)
        if actual != expected:
            mismatches[descriptor.name] = {"expected": expected, "actual": actual}
    assert not mismatches, json.dumps(mismatches, sort_keys=True)


def test_builtin_retry_eligibility_matches_origin_call_sites():
    golden = _load(BEHAVIOR_FIXTURE)
    assert golden["retry"]["eligible"] == []
    # There is no automatic per-tool retry allowlist. Pin the actual generic
    # policy caller and the three keyed, journaled replay declarations instead
    # of claiming that a generic effect=NONE probe makes all tools retryable.
    for path, expected in golden["retry"]["calls"].items():
        tree = ast.parse((ROOT.parent / path).read_text(encoding="utf-8"))
        actual = sorted(ast.unparse(node) for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in {"journaled_effect", "retry_decision"})
        assert actual == expected, f"retry declaration drift in {path}: {actual}"

    # Search the whole bounded production tree for retry entrypoint callers,
    # not only the old files: wiring a built-in dispatcher to retries must
    # invalidate the empty per-tool allowlist rather than escape this test.
    actual_callers = []
    for path in chain(ROOT.parent.glob("*.py"), (ROOT.parent / "sonder_runtime").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in RETRY_ENTRYPOINTS):
            continue
        actual_callers.extend(_retry_entrypoint_calls(source, path.relative_to(ROOT.parent).as_posix()))
    assert sorted(actual_callers) == golden["retry"]["entrypoint_callers"]


RETRY_ENTRYPOINTS = {"retry_decision", "TransportRetryExecutor", "transport_executor", "execute_retry"}


def _retry_entrypoint_calls(source, path):
    calls = []
    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.owners = []

        def visit_FunctionDef(self, node):
            self.owners.append(node.name)
            self.generic_visit(node)
            self.owners.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Call(self, node):
            target = node.func
            if isinstance(target, ast.Subscript):
                target = target.value
            name = (target.id if isinstance(target, ast.Name) else
                    target.attr if isinstance(target, ast.Attribute) else "")
            if name in RETRY_ENTRYPOINTS:
                calls.append(f"{path}:{'.'.join(self.owners)}:{name}")
            self.generic_visit(node)
    Calls().visit(ast.parse(source))
    return calls


def test_legacy_retry_effect_decisions_match_origin_with_explicit_exception():
    for row in _load(BEHAVIOR_FIXTURE)["retry"]["effect_decisions"]:
        kwargs = row["kwargs"]
        # NON_IDEMPOTENT was incorrectly replayed after known transport failure
        # with a key. The generic safety fix has no built-in dispatch caller.
        expected = ("do_not_retry" if kwargs["effect"] == "non_idempotent"
                    and kwargs["outcome_known"] else row["action"])
        assert retry_decision("timeout", **kwargs).action.value == expected, row


def test_builtin_speculation_overlap_matches_documented_exceptions():
    golden = _load(BEHAVIOR_FIXTURE)
    baseline = set(golden["concurrency"]["speculation"])
    exceptions = set(golden["intentional_concurrency_changes"]["speculation"])
    predictor = SimpleNamespace(
        speculatable=lambda name, traits=None: is_speculatable(name, traits),
        note_speculation=lambda: None,
        note_squash=lambda: None,
    )
    actual = set()
    for name in sorted(baseline):
        started, release = Event(), Event()
        def dispatch(*args, _started=started, _release=release):
            _started.set()
            assert _release.wait(3)
            return "ok", True
        engine = SpeculationEngine(predictor, dispatch, enabled=True, slots=2)
        try:
            assert engine.begin(name, "first", {})
            assert started.wait(3)
            if engine.begin(name, "second", {}):
                actual.add(name)
        finally:
            release.set()
            engine.discard()
    assert baseline - actual == exceptions
    assert not actual - baseline


def test_typed_gateway_overlap_matches_documented_exceptions():
    golden = _load(BEHAVIOR_FIXTURE)
    baseline = set(golden["concurrency"]["typed_gateway"])
    exceptions = set(golden["intentional_concurrency_changes"]["typed_gateway"])
    registry = typed_tool_registry()
    assert {descriptor.name for descriptor in registry.list_all()} == baseline
    actual = set()
    for name in sorted(baseline):
        entered, entered_second, attempting, release = Event(), Event(), Event(), Event()
        errors = []
        def invoke(request, _entered=entered, _entered_second=entered_second,
                   _release=release):
            (_entered if request.request_id == "first" else _entered_second).set()
            assert _release.wait(3)
            return ToolInvocationOutput(success=True, output="ok")
        gateway = ToolGateway(
            SimpleNamespace(validate=lambda *args: None),
            SimpleNamespace(authorize=lambda *args: None),
            SimpleNamespace(approve=lambda request: True),
            SimpleNamespace(invoke=invoke),
            SimpleNamespace(redact=lambda name, output: output),
            SimpleNamespace(record=lambda receipt: None), registry=registry,
        )
        def check_control(request, _attempting=attempting):
            if request.request_id == "second":
                _attempting.set()
        gateway._check_control = check_control
        def execute(request_id, _name=name, _gateway=gateway, _errors=errors):
            try:
                _gateway.execute(ToolGatewayRequest(
                    request_id, _name, {}, ToolScope("test"), ToolPermission(),
                ))
            except BaseException as exc:
                _errors.append(exc)
        first = Thread(target=execute, args=("first",))
        second = Thread(target=execute, args=("second",))
        first.start()
        try:
            assert entered.wait(3), name
            second.start()
            assert attempting.wait(3), name
            if entered_second.wait(0.05 if name in exceptions else 3):
                actual.add(name)
        finally:
            release.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        assert not first.is_alive() and not second.is_alive(), name
        assert not errors, (name, errors)
        assert entered_second.is_set(), name
    assert baseline - actual == exceptions
    assert not actual - baseline


def _held_gateway(registry, *, concurrency_gate=None):
    """A gateway whose first call blocks until released, with inert ports."""
    entered = {"first": Event(), "second": Event()}
    release = Event()

    def invoke(request):
        entered[request.request_id].set()
        if request.request_id == "first":
            assert release.wait(3)
        return ToolInvocationOutput(success=True, output="ok")

    gateway = ToolGateway(
        SimpleNamespace(validate=lambda *args: None),
        SimpleNamespace(authorize=lambda *args: None),
        SimpleNamespace(approve=lambda request: True),
        SimpleNamespace(invoke=invoke),
        SimpleNamespace(redact=lambda name, output: output),
        SimpleNamespace(record=lambda receipt: None), registry=registry,
        concurrency_gate=concurrency_gate,
    )
    return gateway, entered, release


def _overlaps(gateway, entered, release, first_tool, second_tool):
    first = Thread(target=gateway.execute, args=(ToolGatewayRequest(
        "first", first_tool, {}, ToolScope("test"), ToolPermission()),))
    first.start()
    try:
        assert entered["first"].wait(3)
        second = Thread(target=gateway.execute, args=(ToolGatewayRequest(
            "second", second_tool, {}, ToolScope("test"), ToolPermission()),))
        second.start()
        return entered["second"].wait(1)
    finally:
        release.set()
        first.join(3)
        second.join(3)


@pytest.mark.parametrize("first_tool,second_tool", [
    ("test_run", "read_file"), ("build_job", "text_search"), ("write_file", "read_file"),
    ("test_run", "write_file"),
])
def test_default_gateway_never_holds_unrelated_calls_behind_a_long_call(first_tool, second_tool):
    """origin/main parity: one shared gateway serves every surface of a process.

    ``test_run``/``build_job`` wait up to 60 s by default, so a default
    exclusive admission gate would stall every agent, MCP and HTTP tool call
    for that long.  Trait-driven admission is opt-in.
    """
    gateway, entered, release = _held_gateway(typed_tool_registry())
    assert _overlaps(gateway, entered, release, first_tool, second_tool)


def test_opt_in_concurrency_gate_serializes_unknown_concurrency_calls():
    from sonder_runtime.application.tools.scheduling import ToolConcurrencyGate

    gateway, entered, release = _held_gateway(
        typed_tool_registry(), concurrency_gate=ToolConcurrencyGate())
    assert not _overlaps(gateway, entered, release, "test_run", "read_file")
    gateway, entered, release = _held_gateway(
        typed_tool_registry(), concurrency_gate=ToolConcurrencyGate())
    assert _overlaps(gateway, entered, release, "read_file", "text_search")
