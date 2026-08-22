from sonder_runtime.adapters.goal_formatting import format_goal


def test_format_goal_handles_missing_active_goal():
    assert format_goal(None) == "no active goal"


def test_format_goal_renders_criteria_and_only_latest_five_notes():
    goal = {
        "id": "goal-1",
        "status": "active",
        "objective": "finish migration",
        "criteria": ["tests pass", "evidence recorded"],
        "notes": [{"text": "old-%d" % index} for index in range(6)],
    }

    assert format_goal(goal) == (
        "goal-1 [active] finish migration\n"
        "  criterion: tests pass\n"
        "  criterion: evidence recorded\n"
        "  note: old-1\n"
        "  note: old-2\n"
        "  note: old-3\n"
        "  note: old-4\n"
        "  note: old-5"
    )
