"""Context compaction plan rendering lives in the domain; the root name is an alias."""
import server
from sonder_runtime.domain.context import compaction_plan_formatting as rendering


def test_root_helper_is_an_identity_preserving_alias():
    assert server.format_context_compaction_plan is rendering.format_context_compaction_plan


def test_a_plan_renders_its_context_lines_and_prioritized_actions():
    plan = {
        "context": {
            "session": "s1", "context_percent": 82, "estimated_tokens": 6560, "context_limit": 8000,
            "context_mode": "compacted", "live_turns": 9, "max_live_turns": 12, "summary_tokens": 400,
        },
        "actions": [
            {"priority": "high", "action": "compact now", "reason": "context is above 80%"},
            {"priority": "info", "action": "keep going"},
        ],
    }
    assert rendering.format_context_compaction_plan(plan).splitlines() == [
        "sonder context compaction plan",
        "  session: s1",
        "  context: 82%  ~6560/8000 tokens (compacted mode)",
        "  live turns: 9/12 | summary: ~400 tokens",
        "  recommended actions:",
        "    [high] compact now",
        "        -> context is above 80%",
        "    [info] keep going",
        "        -> ",
    ]


def test_an_empty_plan_uses_safe_defaults():
    assert rendering.format_context_compaction_plan({}).splitlines() == [
        "sonder context compaction plan",
        "  session: none",
        "  context: 0%  ~0/0 tokens (native mode)",
        "  live turns: 0/0 | summary: ~0 tokens",
        "  recommended actions:",
    ]
