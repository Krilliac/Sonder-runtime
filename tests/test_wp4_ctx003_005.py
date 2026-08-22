from sonder_runtime.application.context_priority import ContextItem, select_context
from sonder_runtime.domain.context.priority import eviction_order


def item(name, *, cost, priority, protected=False, ordinal=0, confidence=None):
    return ContextItem(
        name, "working", cost, priority, "fixture", confidence,
        protected, ordinal,
    )


def test_selection_is_deterministic_and_explains_cost_and_provenance():
    candidates = [
        item("low", cost=3, priority=1, ordinal=2, confidence=0.4),
        item("high", cost=4, priority=9, ordinal=1, confidence=0.9),
        item("later", cost=2, priority=2, ordinal=3, confidence=0.7),
    ]

    result = select_context(candidates, budget=6)

    assert [candidate.item_id for candidate in result.selected] == ["high", "later"]
    assert [candidate.item_id for candidate in result.omitted] == ["low"]
    assert result.used == 6
    assert result.emergency_overflow is False
    assert result.explanations[0].__dict__ == {
        "item_id": "high", "selected": True, "reason": "selected_priority_fit",
        "cost": 4, "source": "fixture", "confidence": 0.9, "priority": 9,
        "protected": False,
    }
    assert result.explanations[2].reason == "omitted_budget"


def test_protected_items_win_ties_and_report_emergency_overflow():
    result = select_context(
        [
            item("optional", cost=1, priority=100, ordinal=1),
            item("guard", cost=5, priority=-1, protected=True, ordinal=2),
        ],
        budget=3,
    )

    assert [candidate.item_id for candidate in result.selected] == ["guard"]
    assert result.used == 5
    assert result.emergency_overflow is True
    assert result.explanations[0].reason == "selected_protected"
    assert result.explanations[1].reason == (
        "emergency_overflow_protected_items_exceed_budget"
    )


def test_eviction_order_is_stable_and_protected_items_are_last():
    candidates = [
        item("new-low", cost=1, priority=1, ordinal=5),
        item("old-low", cost=1, priority=1, ordinal=1),
        item("protected", cost=1, priority=-20, protected=True, ordinal=0),
        item("high", cost=1, priority=8, ordinal=2),
    ]

    assert [candidate.item_id for candidate in eviction_order(candidates)] == [
        "old-low", "new-low", "high", "protected",
    ]
