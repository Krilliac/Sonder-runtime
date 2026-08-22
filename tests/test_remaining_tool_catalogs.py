from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.generated_catalogs import (
    CatalogLimitError,
    CatalogLimits,
    GeneratedCatalogs,
)
from sonder_runtime.domain.common.events import EventKind


def _registry():
    return InMemoryToolRegistry([
        ToolDescriptor(
            name="zeta",
            description="Z tool",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        ),
        ToolDescriptor(name="alpha", description="A tool"),
    ])


def test_all_surfaces_are_derived_and_sorted_from_typed_tools():
    bundle = GeneratedCatalogs.generate(
        _registry(), commands=[{"name": "/z", "summary": "Z"}, {"name": "/a", "summary": "A"}],
        event_kinds=[EventKind.TOOL_COMPLETED],
    )
    assert [item["name"] for item in bundle.mcp["tools"]] == ["alpha", "zeta"]
    assert [item["function"]["name"] for item in bundle.openai["tools"]] == ["alpha", "zeta"]
    assert [item["name"] for item in bundle.cli["commands"]] == ["/a", "/z"]
    assert bundle.client["events"][0]["name"] == "tool.completed"
    assert bundle.digest == bundle.client["digest"]


def test_digest_is_deterministic_and_changes_when_contract_changes():
    first = GeneratedCatalogs.generate(_registry(), event_kinds=[])
    second = GeneratedCatalogs.generate(tuple(reversed(_registry().list_all())), event_kinds=[])
    changed = GeneratedCatalogs.generate(
        InMemoryToolRegistry([ToolDescriptor(name="alpha", description="changed")]), event_kinds=[]
    )
    assert first.digest == second.digest
    assert first.digest != changed.digest


def test_protocol_and_event_schema_shapes_are_bounded_contracts():
    bundle = GeneratedCatalogs.generate(_registry(), event_kinds=[EventKind.TOOL_COMPLETED])
    assert bundle.openai["tools"][1]["function"]["parameters"]["required"] == ["x"]
    event = bundle.client["events"][0]
    assert event["schema"]["properties"]["call_id"] == {"type": "string"}
    assert event["schema"]["additionalProperties"] is False


def test_explicit_bounds_fail_closed_instead_of_silent_truncation():
    try:
        GeneratedCatalogs.generate(_registry(), limits=CatalogLimits(max_tools=1))
    except CatalogLimitError as exc:
        assert "max_tools" in str(exc)
    else:
        raise AssertionError("expected bounded catalog failure")


def test_freshness_digest_is_present_in_serialized_client_catalog():
    bundle = GeneratedCatalogs.generate(_registry(), event_kinds=[])
    assert bundle.client["digest"]
    assert len(bundle.digest) == 64
