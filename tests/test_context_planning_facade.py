"""Focused evidence for the CTX-001..010 production facade."""
from __future__ import annotations

from sonder_runtime.adapters.context_planning import RuntimeContextPlanningAdapter
from sonder_runtime.application.context_integration import ContextPlanningFacade
from sonder_runtime.application.context_manifests import ContextRecord, PrefixManifestCache
from sonder_runtime.application.context_planner import CONTEXT_SECTIONS, ModelContext
from sonder_runtime.domain.context.hardware_sizing import MeasuredContextCapability
from sonder_runtime.domain.context.priority import ContextItem


def _budgets(value: int = 100) -> dict[str, int]:
    return {section: value for section in CONTEXT_SECTIONS}


def _item(item_id: str, section: str, cost: int, priority: int) -> ContextItem:
    return ContextItem(item_id, section, cost, priority, "fixture", ordinal=0)


def test_facade_composes_selection_explanation_hardware_prefix_and_replay():
    facade = ContextPlanningFacade()
    records = (
        ContextRecord("rules", "stable_instructions", "keep safe", "policy", stable=True),
        ContextRecord("history", "recent_history", "hello", "session"),
    )

    result = facade.assemble(
        ModelContext("model", 20_000, 2_000),
        {"policy": [_item("high", "policy", 4, 10), _item("low", "policy", 4, 1)]},
        {**_budgets(10), "policy": 4},
        capability=MeasuredContextCapability(16_000, 8.0, 4.0, "model", "q8"),
        records=records,
        request_id="req-1",
        replay_metadata={"mode": "test"},
    )

    assert result.plan.context_window_tokens == 11_520
    assert [item.item_id for item in result.selected] == ["high"]
    assert result.selections["policy"].explanations[1].reason == "omitted_budget"
    assert result.prefix is not None and result.prefix.cache_key
    assert result.replay is not None and result.replay.request_id == "req-1"
    assert facade.last_good().plan == result.plan  # type: ignore[union-attr]


def test_facade_exposes_bounded_overflow_recovery_and_last_good_fallback():
    facade = ContextPlanningFacade()
    result = facade.recover_overflow(
        list(range(10)),
        compact=lambda value: value[:-1],
        shrink=lambda value, factor: value[:max(1, int(len(value) * factor))],
        fits=lambda value: len(value) <= 3,
        max_attempts=2,
    )

    assert result.action == "adaptively_shrunk"
    assert result.attempts == 3
    assert result.candidate == [0, 1, 2]


def test_facade_prefix_identity_telemetry_and_last_good_restore_after_compaction():
    facade = ContextPlanningFacade()
    baseline = facade.assemble(
        ModelContext("model-a", 20_000, 2_000), {}, _budgets(20),
        records=(ContextRecord("rules", "stable_instructions", "safe", "policy", stable=True),),
        tokenizer="tok", template="chat", system_prefix="sys",
        visible_tool_schemas={"read": {"type": "object"}}, project_policy={"network": "deny"},
    )
    restored = facade.assemble(
        ModelContext("model-a", 20_000, 2_000), {}, _budgets(20),
        records=(ContextRecord("rules", "stable_instructions", "safe", "policy", stable=True),),
        tokenizer="tok", template="chat", system_prefix="sys",
        visible_tool_schemas={"read": {"type": "object"}}, project_policy={"network": "deny"},
        dynamic_memory=["post-compaction"], retrieval={"query": "new"},
    )
    assert baseline.prefix is not None and restored.prefix is not None
    assert baseline.prefix.cache_key == restored.prefix.cache_key
    assert facade.prefix_cache_telemetry.last_reason == "hit"
    assert facade.prefix_cache_telemetry.hits == 1
    assert facade.last_good() is not None


def test_facade_keeps_its_own_cache_observation_when_another_request_interleaves():
    class InterleavingCache(PrefixManifestCache):
        def resolve_observed(self, records, *, version="1", **identity):
            selected = super().resolve_observed(records, version=version, **identity)
            super().resolve_observed(
                (ContextRecord("other", "stable_instructions", "other", "policy", stable=True),),
                version=version, **identity,
            )
            return selected

    cache = InterleavingCache()
    result = ContextPlanningFacade(prefix_cache=cache).assemble(
        ModelContext("model", 20_000, 2_000), {}, _budgets(20),
        records=(ContextRecord("rules", "stable_instructions", "safe", "policy", stable=True),),
    )
    assert result.prefix is not None and result.prefix_observation is not None
    assert result.prefix_observation.cache_key == result.prefix.cache_key
    assert cache.last_observation.cache_key != result.prefix.cache_key


def test_facade_builds_identity_prefix_without_stable_records():
    result = ContextPlanningFacade().assemble(
        ModelContext("model-a", 20_000, 2_000), {}, _budgets(20),
        system_prefix="system", visible_tool_schemas={"read": {"type": "object"}},
        project_policy={"network": "deny"},
    )
    assert result.prefix is not None
    assert result.prefix.sections == ()


def test_runtime_adapter_exposes_the_same_facade_boundary():
    adapter = RuntimeContextPlanningAdapter()
    assert isinstance(adapter.facade, ContextPlanningFacade)
    result = adapter.assemble(
        model="runtime-model",
        context_tokens=32_000,
        reserved_output_tokens=2_000,
        items={},
        section_budgets=_budgets(20),
    )
    assert result.plan.input_budget_tokens == 30_000
def test_prefix_identity_separates_provider_bindings():
    from sonder_runtime.application.context_manifests import build_prefix_manifest

    first = build_prefix_manifest(
        (), model="shared-name", provider_id="ollama",
        tokenizer="tokenizer-v1", template="chat-v1",
    )
    second = build_prefix_manifest(
        (), model="shared-name", provider_id="openai_compatible",
        tokenizer="tokenizer-v1", template="chat-v1",
    )
    assert first.cache_key != second.cache_key


ALWAYS_RULE = "Always run the full test suite before you push changes to the main branch."
NEVER_RULE = "Never run the full test suite before you push changes to the main branch."


def _rules(first: str = ALWAYS_RULE, second: str = NEVER_RULE) -> tuple[ContextRecord, ContextRecord]:
    return (
        ContextRecord("rule:first", "project_rules", first, "project-rule:global", ordinal=1, stable=True),
        ContextRecord("rule:second", "project_rules", second, "project-rule:project", ordinal=2, stable=True),
    )


def _assemble_records(records, **kwargs):
    return ContextPlanningFacade().assemble(
        ModelContext("model-a", 20_000, 2_000), {}, _budgets(20),
        records=records, tokenizer="tok", template="chat", request_id="r", **kwargs,
    )


def test_facade_keeps_contradictory_rules_from_different_sources_in_both_manifests():
    result = _assemble_records(_rules())

    assert result.replay is not None and result.prefix is not None
    assert [section.item_id for section in result.replay.sections] == ["rule:first", "rule:second"]
    assert [section.source for section in result.replay.sections] == [
        "project-rule:global", "project-rule:project",
    ]
    assert {record.item_id for record in result.prefix.sections} == {"rule:first", "rule:second"}
    assert result.dedup is not None and result.dedup.provenance == ()
    assert "context_dedup" not in result.replay.metadata


def test_facade_evidence_changes_when_a_near_duplicate_rule_changes():
    baseline = _assemble_records(_rules())
    # Still one word away from the first rule, so similarity alone would
    # treat both versions of the second rule as the same duplicate.
    changed = _assemble_records(_rules(second=ALWAYS_RULE.replace("Always", "Sometimes")))

    assert baseline.prefix.cache_key != changed.prefix.cache_key  # type: ignore[union-attr]
    assert baseline.replay.manifest_digest != changed.replay.manifest_digest  # type: ignore[union-attr]


def test_facade_exact_duplicates_keep_every_source_and_count_once():
    result = _assemble_records(_rules(second=ALWAYS_RULE))

    assert result.replay is not None and result.prefix is not None and result.dedup is not None
    # Both rendered copies stay in the evidence with their own source.
    assert [(section.item_id, section.source) for section in result.replay.sections] == [
        ("rule:first", "project-rule:global"), ("rule:second", "project-rule:project"),
    ]
    assert len(result.prefix.sections) == 2
    # They are one piece of evidence, and the mapping is part of the digest.
    assert [record.item_id for record in result.dedup.retained] == ["rule:first"]
    assert result.replay.metadata["context_dedup"] == [{
        "removed_item_id": "rule:second", "retained_item_id": "rule:first",
        "reason": "exact", "similarity": 1.0,
        "removed_source": "project-rule:project", "retained_source": "project-rule:global",
        "removed_section": "project_rules", "retained_section": "project_rules",
    }]
    independent = {section.item_id for section in result.replay.sections} - {
        entry["removed_item_id"] for entry in result.replay.metadata["context_dedup"]
    }
    assert independent == {"rule:first"}
    distinct = _assemble_records(_rules())
    assert distinct.replay.manifest_digest != result.replay.manifest_digest  # type: ignore[union-attr]


def test_facade_prefix_changes_when_an_exact_duplicate_rule_is_removed():
    both = _assemble_records(_rules(second=ALWAYS_RULE))
    one = _assemble_records(_rules(second=ALWAYS_RULE)[:1])

    assert both.prefix.cache_key != one.prefix.cache_key  # type: ignore[union-attr]


def test_facade_manifests_do_not_depend_on_record_input_order():
    forward = _assemble_records(_rules())
    backward = _assemble_records(tuple(reversed(_rules())))

    assert forward.replay == backward.replay
    assert forward.prefix == backward.prefix
    assert forward.dedup == backward.dedup
    assert len(forward.replay.sections) == 2  # type: ignore[union-attr]


def test_facade_records_semantic_grouping_of_prose_without_dropping_it():
    records = (
        ContextRecord("mem:a", "memories", ALWAYS_RULE, "recall-a", ordinal=1),
        ContextRecord("mem:b", "memories", NEVER_RULE, "recall-b", ordinal=2),
    )
    result = _assemble_records(records)

    assert result.replay is not None
    assert [section.item_id for section in result.replay.sections] == ["mem:a", "mem:b"]
    (entry,) = result.replay.metadata["context_dedup"]
    assert (entry["removed_item_id"], entry["retained_item_id"], entry["reason"]) == (
        "mem:b", "mem:a", "semantic",
    )


def test_facade_rejects_caller_metadata_under_the_reserved_dedup_key():
    try:
        _assemble_records(_rules(), replay_metadata={"context_dedup": []})
    except ValueError:
        pass
    else:
        raise AssertionError("the dedup mapping key is reserved for the facade")
