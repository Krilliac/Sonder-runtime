"""The tool schema gate enforces every keyword the catalog advertises (TL01).

``validate_tool_call`` used to check only scalar types, enum/const, required,
``additionalProperties: false``, item traversal and string lengths, so
``oneOf``, numeric bounds, ``pattern``, array/object size bounds,
schema-valued ``additionalProperties`` and non-finite numbers passed the gate
on every composed route.  These tests walk the real native and typed catalogs,
replay the confirmed probe inputs, drive the public routes with spy executors
and pin the declared dialect at descriptor construction.
"""
from __future__ import annotations

import io
import json
import math
from collections import Counter
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_ARGUMENT_DEPTH,
    MAX_TOOL_SCHEMA_DEPTH,
    SONDER_TOOL_SCHEMA_SUBSET_V1,
    InMemoryToolRegistry,
    ToolCall,
    ToolDescriptor,
    check_schema_supported,
    validate_tool_arguments,
    validate_tool_call,
)
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway,
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.bootstrap.native_mcp import _LEGACY_ALIASES, native_tool_registry, run_native_mcp
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from sonder_runtime.domain.common.errors import InvalidInput

_NO_SAMPLE = object()
_PATTERN_CANDIDATES = (
    "../../evil;rm", "", " ", "a b", "x\n", "-", "build-job-zz", "build-fix-zz",
)
_SAMPLE_STRINGS = (
    "a", "abc", "a1", "build-job-0123456789abcdef", "build-fix-0123456789abcdef", "1", "x" * 64,
)


def _accepts(schema: dict[str, Any], value: Any) -> bool:
    probe = ToolDescriptor("probe", input_schema={
        "type": "object", "properties": {"v": schema}, "required": ["v"],
    })
    try:
        validate_tool_call(probe, ToolCall("probe", {"v": value}))
    except InvalidInput:
        return False
    return True


def _types(schema: dict[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    return (declared,) if isinstance(declared, str) else tuple(declared)


def _sample(schema: Any) -> Any:
    """Return a value the schema accepts, or ``_NO_SAMPLE``."""
    if not isinstance(schema, dict):
        return _NO_SAMPLE
    if "const" in schema:
        candidates: list[Any] = [schema["const"]]
    elif "enum" in schema:
        candidates = list(schema["enum"])
    else:
        candidates = []
        for name in _types(schema) or ("string",):
            if name == "string":
                low = schema.get("minLength", 0)
                candidates.extend(_SAMPLE_STRINGS)
                candidates.append("a" * max(low, 1))
            elif name in {"integer", "number"}:
                low = schema.get("minimum", 1)
                candidates.append(int(low) if name == "integer" else float(low))
            elif name == "boolean":
                candidates.append(True)
            elif name == "null":
                candidates.append(None)
            elif name == "array":
                item = _sample(schema.get("items", {"type": "string"}))
                if item is not _NO_SAMPLE:
                    candidates.append([item] * schema.get("minItems", 0))
            elif name == "object":
                value: dict[str, Any] = {}
                for key in schema.get("required", ()):
                    child = _sample(schema.get("properties", {}).get(key, {"type": "string"}))
                    if child is _NO_SAMPLE:
                        return _NO_SAMPLE
                    value[key] = child
                candidates.append(value)
    for candidate in candidates:
        if _accepts(schema, candidate):
            return candidate
    return _NO_SAMPLE


def _wrong_type(schema: dict[str, Any]) -> Any:
    allowed = set(_types(schema))
    for name, value in (("string", 7), ("integer", "seven"), ("array", {"k": "v"}),
                        ("object", ["x"]), ("boolean", "yes")):
        if name in allowed:
            return value
    return _NO_SAMPLE


def _subschemas(schema: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if not isinstance(schema, dict):
        return
    yield path, schema
    for name, child in schema.get("properties", {}).items():
        yield from _subschemas(child, f"{path}.{name}")
    if isinstance(schema.get("items"), dict):
        yield from _subschemas(schema["items"], f"{path}[]")
    if isinstance(schema.get("additionalProperties"), dict):
        yield from _subschemas(schema["additionalProperties"], f"{path}.<additional>")
    for keyword in ("allOf", "anyOf", "oneOf"):
        for index, child in enumerate(schema.get(keyword, ())):
            yield from _subschemas(child, f"{path}<{keyword}{index}>")


def _violations(schema: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield ``(keyword, value)`` pairs that satisfy the declared type but break one keyword."""
    types = set(_types(schema))
    numeric = types & {"integer", "number"}
    if numeric:
        as_int = "integer" in types and "number" not in types
        if "minimum" in schema:
            low = schema["minimum"]
            yield "minimum", (int(low) - 1 if as_int else low - 0.5)
        if "maximum" in schema:
            high = schema["maximum"]
            yield "maximum", (int(high) + 1 if as_int else high + 0.5)
        if "number" in types:
            for bad in (math.nan, math.inf, -math.inf):
                yield "non-finite", bad
    if "pattern" in schema and "string" in types:
        low, high = schema.get("minLength", 0), schema.get("maxLength", 1 << 30)
        for candidate in _PATTERN_CANDIDATES:
            if low <= len(candidate) <= high and not _accepts({"type": "string", "pattern": schema["pattern"]}, candidate):
                yield "pattern", candidate
                break
    if "array" in types:
        if schema.get("minItems", 0) > 0:
            yield "minItems", []
        if "maxItems" in schema:
            item = _sample(schema.get("items", {"type": "string"}))
            if item is not _NO_SAMPLE:
                yield "maxItems", [item] * (schema["maxItems"] + 1)
    if "object" in types:
        extra = schema.get("additionalProperties", True)
        if isinstance(extra, dict):
            wrong = _wrong_type(extra)
            if wrong is not _NO_SAMPLE:
                yield "additionalProperties", {"undeclared_probe": wrong}
            if "maxProperties" in schema:
                value = _sample(extra)
                if value is not _NO_SAMPLE:
                    yield "maxProperties", {f"k{i}": value for i in range(schema["maxProperties"] + 1)}
        if "oneOf" in schema:
            properties = schema.get("properties", {})
            yield "oneOf", {}
            every = {name: _sample(child) for name, child in properties.items()}
            if all(value is not _NO_SAMPLE for value in every.values()):
                yield "oneOf", every


def _catalog() -> list[tuple[str, ToolDescriptor]]:
    return ([("native", item) for item in native_tool_registry().list_all()]
            + [("typed", item) for item in typed_tool_registry().list_all()])


def test_real_catalog_constraints_are_enforced_by_the_public_gate():
    executed: Counter[str] = Counter()
    accepted: list[str] = []
    for family, descriptor in _catalog():
        for path, schema in _subschemas(descriptor.input_schema, descriptor.name):
            for keyword, value in _violations(schema):
                executed[keyword] += 1
                if path == descriptor.name:
                    # A top-level keyword (``oneOf``) must be checked on the
                    # real descriptor itself.
                    try:
                        validate_tool_call(descriptor, ToolCall(descriptor.name, value))
                    except InvalidInput:
                        continue
                elif not _accepts(schema, value):
                    continue
                accepted.append(f"{family}:{path} {keyword}={value!r}")
    assert accepted == []
    # A walker that matched nothing must not pass by default: every keyword
    # the catalog is known to use produced at least one executed case.
    for keyword in ("minimum", "maximum", "pattern", "maxItems", "maxProperties",
                    "additionalProperties", "oneOf", "non-finite"):
        assert executed[keyword] > 0, (keyword, dict(executed))


def test_every_catalog_descriptor_uses_only_the_declared_dialect():
    names = [descriptor.name for _family, descriptor in _catalog()]
    assert len(names) > 50
    for _family, descriptor in _catalog():
        check_schema_supported(descriptor.input_schema)


def _native(name: str) -> ToolDescriptor:
    descriptor = native_tool_registry().get(name)
    assert descriptor is not None
    return descriptor


_COMPUTE_BASE = {
    "request_id": "r1", "workload": "build", "catalog_entry_id": "c1",
    "workspace_mapping": "w1", "allow_remote": False,
}


@pytest.mark.parametrize(("tool", "arguments"), [
    ("tool_help", {}),
    ("tool_help", {"name": "file_read", "query": "read"}),
    ("file_check", {"path": "a.py", "max_items": 100_000}),
    ("file_check", {"path": "a.py", "max_items": -5}),
    ("build_model", {"preset": "../../evil;rm"}),
    ("compute_submit", {**_COMPUTE_BASE, "environment": {"A": 1}}),
    ("compute_submit", {**_COMPUTE_BASE, "environment": {"B": [1]}}),
    ("compute_submit", {**_COMPUTE_BASE, "environment": {f"K{i}": "v" for i in range(33)}}),
    ("compute_submit", {**_COMPUTE_BASE, "arguments": ["x"] * 65}),
    ("archive_extract", {"source": "a.zip", "destination": "out", "max_seconds": math.inf}),
    ("archive_extract", {"source": "a.zip", "destination": "out", "max_seconds": math.nan}),
    ("archive_extract", {"source": "a.zip", "destination": "out", "max_seconds": 61}),
])
def test_confirmed_probe_inputs_are_refused(tool, arguments):
    with pytest.raises(InvalidInput):
        validate_tool_call(_native(tool), ToolCall(tool, arguments))


@pytest.mark.parametrize(("tool", "arguments"), [
    ("tool_help", {"name": "file_read"}),
    ("tool_help", {"query": "read"}),
    ("file_check", {"path": "a.py", "max_items": 100}),
    ("build_model", {"preset": "release-x64"}),
    ("compute_submit", {**_COMPUTE_BASE, "environment": {"A": "1"}, "arguments": ["x"]}),
    ("archive_extract", {"source": "a.zip", "destination": "out", "max_seconds": 60.0}),
])
def test_valid_catalog_calls_still_pass(tool, arguments):
    validate_tool_call(_native(tool), ToolCall(tool, arguments))


def _descriptor(schema: dict[str, Any]) -> ToolDescriptor:
    return ToolDescriptor("probe", input_schema={
        "type": "object", "properties": {"v": schema}, "required": ["v"],
    })


@pytest.mark.parametrize(("schema", "bad", "good"), [
    ({"type": ["string", "null"]}, 3, None),
    ({"type": "number", "exclusiveMinimum": 0}, 0, 0.5),
    ({"type": "number", "exclusiveMaximum": 1}, 1, 0.5),
    ({"type": "array", "minItems": 2}, ["a"], ["a", "b"]),
    ({"type": "object", "minProperties": 1}, {}, {"a": 1}),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 1.5, 1),
    ({"allOf": [{"type": "integer"}, {"minimum": 3}]}, 2, 3),
    ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1, 1.5),
    ({"type": "string", "pattern": "^a+$"}, "aab", "aaa"),
    # ``$`` alone would accept a trailing newline under ``re.search``.
    ({"type": "string", "pattern": "^a+$"}, "aa\n", "a"),
    ({"type": "integer", "minimum": 0}, True, 0),
])
def test_subset_keywords_are_enforced(schema, bad, good):
    descriptor = _descriptor(schema)
    validate_tool_call(descriptor, ToolCall("probe", {"v": good}))
    with pytest.raises(InvalidInput):
        validate_tool_call(descriptor, ToolCall("probe", {"v": bad}))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_refused_even_without_a_declared_type(bad):
    descriptor = ToolDescriptor("probe", input_schema={"type": "object"})
    with pytest.raises(InvalidInput):
        validate_tool_call(descriptor, ToolCall("probe", {"anything": [{"deep": bad}]}))


@pytest.mark.parametrize("schema", [
    {"type": "string", "format": "uri"},
    {"type": "object", "patternProperties": {"^a$": {"type": "string"}}},
    {"$ref": "#/definitions/x"},
    {"if": {"type": "string"}, "then": {"minLength": 1}},
    {"not": {"type": "string"}},
    {"type": "array", "uniqueItems": True},
    {"type": "integer", "multipleOf": 2},
    {"type": "string", "pattern": "a+"},  # unanchored
    {"type": "string", "pattern": "^(a$"},  # does not compile
    {"type": "integer", "minimum": "1"},
    {"type": "integer", "maximum": math.inf},
    {"type": "number", "exclusiveMinimum": True},
    {"type": "array", "maxItems": -1},
    {"type": "array", "items": [{"type": "string"}]},
    {"type": "sometimes"},
    {"oneOf": []},
])
def test_unsupported_or_malformed_assertions_are_refused_at_construction(schema):
    with pytest.raises(InvalidInput):
        _descriptor(schema)
    with pytest.raises(InvalidInput):
        InMemoryToolRegistry().register(_descriptor(schema))


def test_annotations_are_allowed_and_the_subset_is_declared():
    _descriptor({"type": "string", "description": "d", "title": "t", "default": "x",
                 "examples": ["x"]})
    assert {"oneOf", "minimum", "maximum", "pattern", "maxItems", "maxProperties",
            "additionalProperties"} <= SONDER_TOOL_SCHEMA_SUBSET_V1
    assert "format" not in SONDER_TOOL_SCHEMA_SUBSET_V1


def test_deep_schema_is_refused_at_construction():
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_TOOL_SCHEMA_DEPTH + 1):
        schema = {"type": "array", "items": schema}
    with pytest.raises(InvalidInput):
        _descriptor(schema)


def test_deep_or_oversized_arguments_are_refused_before_recursion():
    descriptor = ToolDescriptor("probe", input_schema={"type": "object"})
    nested: Any = "leaf"
    for _ in range(50_000):
        nested = [nested]
    with pytest.raises(InvalidInput, match="nested deeper"):
        validate_tool_call(descriptor, ToolCall("probe", {"v": nested}))
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(InvalidInput, match="nested deeper"):
        validate_tool_call(descriptor, ToolCall("probe", {"v": cyclic}))
    shallow: Any = "leaf"
    for _ in range(MAX_TOOL_ARGUMENT_DEPTH - 2):
        shallow = [shallow]
    validate_tool_call(descriptor, ToolCall("probe", {"v": shallow}))
    with pytest.raises(InvalidInput, match="size limit"):
        validate_tool_call(descriptor, ToolCall("probe", {"v": "x" * (17 * 1024 * 1024)}))


def _compact_ascii_json_size(arguments: Any) -> int:
    # The encoding permission_modes.call_digest and the audit use.
    return len(json.dumps(arguments, separators=(",", ":"), ensure_ascii=True))


@pytest.mark.parametrize(
    ("char", "escaped"),
    [("\x00", 6), ("\x1f", 6), ("\x7f", 6), ("\ud800", 6), ("\u00e9", 6),
     ("\U0001f600", 12)],
)
def test_argument_size_is_charged_at_the_escaped_json_size(char: str, escaped: int):
    # Each of these is one code point and at most 4 UTF-8 bytes, but escapes
    # to ``escaped`` bytes; a UTF-8 count let the JSON form exceed the limit.
    count = MAX_TOOL_ARGUMENT_BYTES // escaped + 1
    over = {"s": char * count}
    assert _compact_ascii_json_size(over) > MAX_TOOL_ARGUMENT_BYTES
    with pytest.raises(InvalidInput, match="size limit"):
        validate_tool_arguments({"type": "object"}, over)
    with pytest.raises(InvalidInput, match="size limit"):
        validate_tool_arguments({"type": "object"}, {char * count: 0})
    under = {"s": char * (MAX_TOOL_ARGUMENT_BYTES // escaped - 4)}
    validate_tool_arguments({"type": "object"}, under)
    assert _compact_ascii_json_size(under) <= MAX_TOOL_ARGUMENT_BYTES


def test_argument_size_counts_integer_keys_at_their_digits():
    # 10**4000 has 4001 digits, so each key is 4003 bytes in JSON; it used to
    # be charged a flat 8 bytes.
    keys = {10**4000 + i: 0 for i in range(MAX_TOOL_ARGUMENT_BYTES // 4003 + 1)}
    assert _compact_ascii_json_size({"v": keys}) > MAX_TOOL_ARGUMENT_BYTES
    with pytest.raises(InvalidInput, match="size limit"):
        validate_tool_arguments({"type": "object"}, {"v": keys})


def test_argument_size_counts_a_colon_and_a_comma_per_member():
    # Each member costs a colon and a comma; one byte per member used to be
    # charged for both, so this object, one byte over the limit, was accepted.
    members = {f"k{i}": "" for i in range(100_000)}
    arguments: dict[str, Any] = {"m": members, "s": ""}
    arguments["s"] = "x" * (MAX_TOOL_ARGUMENT_BYTES + 1 - _compact_ascii_json_size(arguments))
    assert _compact_ascii_json_size(arguments) == MAX_TOOL_ARGUMENT_BYTES + 1
    with pytest.raises(InvalidInput, match="size limit"):
        validate_tool_arguments({"type": "object"}, arguments)


# --- composed routes: the invoker never runs on a refused call -------------


class _SpyExecutor:
    def __init__(self):
        self.calls: list[Any] = []

    def execute(self, *args):
        self.calls.append(args)
        if len(args) == 2:
            from sonder_runtime.application.ports.tool_executor import ToolResult
            return ToolResult(ok=True, output=args[0].tool + ":ok")
        descriptor, call = args[0], args[1]
        return ToolExecutionResult(descriptor.name, True, output={"tool": call.tool_name})


class _Allow:
    def authorize(self, *args):
        return None

    def select_execution_class(self, descriptor):
        return descriptor.execution_class

    def approve(self, request):
        return True

    def redact(self, name, output):
        return output

    def record(self, receipt):
        return None


def _typed_gateway(executor: _SpyExecutor) -> ToolGateway:
    allow = _Allow()
    return ToolGateway.from_typed_ports(
        typed_tool_registry(), allow, executor, allow, allow, allow, allow,
    )


@pytest.mark.parametrize("arguments", [
    {"job_id": "build-job-../../evil"},
    {"job_id": "build-job-0123456789abcdef", "wait_seconds": 1_000},
    {"job_id": "build-job-0123456789abcdef", "wait_seconds": -1},
])
def test_typed_gateway_refuses_before_the_executor(arguments):
    executor = _SpyExecutor()
    request = ToolGatewayRequest(
        request_id="tl01", tool_name="build_job_result", arguments=arguments,
        scope=ToolScope("owner", ("workspace",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"})),
    )
    with pytest.raises(InvalidInput):
        _typed_gateway(executor).execute(request)
    assert executor.calls == []


def _run_native(calls: list[tuple[str, dict[str, Any]]], app: Any, **kwargs: Any) -> list[dict[str, Any]]:
    messages = [("initialize", {"protocolVersion": "2.0", "capabilities": {"tools": {}}})]
    messages += [("tools/call", {"name": name, "arguments": arguments}) for name, arguments in calls]
    stream = io.StringIO("".join(
        json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params}) + "\n"
        for index, (method, params) in enumerate(messages)
    ))
    output = io.StringIO()
    run_native_mcp(app, input_stream=stream, output_stream=output, **kwargs)
    return [json.loads(line) for line in output.getvalue().splitlines()][1:]


@pytest.fixture
def allow_native_tools(monkeypatch):
    from sonder_runtime.adapters.security.permission_policy import permission_policy

    monkeypatch.setattr(
        permission_policy, "decide_for_caller",
        lambda *_args, **_kwargs: SimpleNamespace(action="allow"),
    )


def test_native_mcp_refuses_catalog_violations_and_aliases_before_the_executor(allow_native_tools):
    # Every legacy alias resolves to a descriptor whose schema is the
    # canonical one, so validating the alias name validates the effective
    # contract the canonical executor receives.
    registry = native_tool_registry()
    for alias, canonical in _LEGACY_ALIASES.items():
        assert registry.require(alias).input_schema == registry.require(canonical).input_schema

    executor = _SpyExecutor()
    app = SimpleNamespace(config=None, tool_executor=executor)
    refused = [
        ("archive_extract", {"source": "a.zip", "destination": "out", "max_entries": 100_000}),
        ("archive_extract", {"source": "a.zip", "destination": "out", "max_seconds": math.inf}),
        ("build_model", {"preset": "../../evil;rm"}),
        ("compute_submit", {**_COMPUTE_BASE, "environment": {"A": 1}}),
        # Legacy alias rows (``script_search`` is listed in _LEGACY_ALIASES).
        ("script_search", {"query": "x", "timeout_seconds": math.inf}),
        ("file_read", {"path": "x.txt", "max_bytes": math.nan}),
    ]
    rows = _run_native(refused + [("archive_extract", {"source": "a.zip", "destination": "out"})], app)
    for row in rows[:len(refused)]:
        assert row["error"]["code"] == -32602, row
    assert rows[-1]["result"]["output"] == "archive_extract:ok"
    assert len(executor.calls) == 1


def test_native_discovery_tools_refuse_bounds_before_discovery_runs(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
    from sonder_runtime.application.tools.discovery import ToolDiscovery

    seen: list[str] = []
    original_search, original_load = ToolDiscovery.search, ToolDiscovery.load

    def search(self, *args, **kwargs):
        seen.append("search")
        return original_search(self, *args, **kwargs)

    def load(self, *args, **kwargs):
        seen.append("load")
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(ToolDiscovery, "search", search)
    monkeypatch.setattr(ToolDiscovery, "load", load)
    digest = ToolDiscovery(native_tool_registry()).digest
    app = SimpleNamespace(config=None, tool_executor=_SpyExecutor(),
                          tool_audit=DurableToolAuditRepository(tmp_path / "audit.jsonl"))
    rows = _run_native([
        ("tool_search", {"query": "file", "limit": 100}),
        ("tool_search", {"query": "file", "limit": 0}),
        ("tool_schema", {"names": [], "inventory_digest": digest}),
        ("tool_schema", {"names": [f"n{i}" for i in range(9)], "inventory_digest": digest}),
        ("tool_search", {"query": "file", "limit": 2}),
    ], app, progressive_tools=True)
    for row in rows[:4]:
        assert "error" in row and "result" not in row, row
    assert rows[4]["result"]["isError"] is False
    assert seen == ["search"]


def test_legacy_tool_service_validates_before_policy_and_executor():
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.execution.tool_service import ToolService
    from sonder_runtime.domain.tools.descriptors import ToolCall as DomainCall
    from sonder_runtime.domain.tools.descriptors import ToolDescriptor as DomainDescriptor
    from sonder_runtime.domain.tools.descriptors import ToolResult as DomainResult

    events: list[str] = []

    class Policy:
        def authorize(self, descriptor, call):
            events.append("policy")

        def select_execution_class(self, descriptor):
            return descriptor.execution_class

    class Executor:
        def execute(self, descriptor, call, context, execution_class):
            events.append("executor")
            return DomainResult(tool_name=call.tool_name, output="ok")

    descriptor = DomainDescriptor("bounded", input_schema={
        "type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}},
        "additionalProperties": False,
    })
    registry = SimpleNamespace(get=lambda name: descriptor if name == "bounded" else None,
                               list_all=lambda: [descriptor])
    service = ToolService(registry, Policy(), Executor())
    context = local_owner_context(correlation_id="tl01", source="repl")
    for bad in ({"n": 6}, {"n": 0}, {"n": math.inf}, {"m": 1}):
        with pytest.raises(InvalidInput):
            service.execute(DomainCall("bounded", bad), context)
    assert events == []
    assert service.execute(DomainCall("bounded", {"n": 3}), context).success is True
    assert events == ["policy", "executor"]
