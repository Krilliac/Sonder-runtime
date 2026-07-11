import intents


def test_trace_and_strict_and_reasoning():
    assert intents.classify("strict on, debug on, show reasoning") == {
        "trace": True, "strict": True,
    }


def test_trace_off():
    assert intents.classify("trace off") == {"trace": False}


def test_run_it():
    assert intents.classify("run it") == {"run": True}


def test_train_yourself():
    assert intents.classify("train yourself") == {"train": 3}


def test_train_on_n_tasks():
    assert intents.classify("train on 5 tasks") == {"train": 5}


def test_practice():
    assert intents.classify("practice") == {"train": 3}


def test_negative_long_real_task():
    assert intents.classify(
        "write a python function to run a subprocess and show its output"
    ) == {}


def test_negative_question_execute():
    assert intents.classify("how do I execute shell commands in python") == {}


def test_negative_explain_strict_mode():
    assert intents.classify("explain strict mode in javascript") == {}


def test_negative_what_is_strict_mode():
    assert intents.classify("what is strict mode") == {}


def test_show_me_your_reasoning_still_fires():
    assert intents.classify("show me your reasoning") == {"trace": True}


def test_empty_and_none():
    assert intents.classify("") == {}
    assert intents.classify(None) == {}
    assert intents.classify("   ") == {}


def test_work_intent_requires_action_and_workspace_target():
    assert intents.classify_work("search the repo for TODO markers") is True
    assert intents.classify_work("please edit C:\\work\\app.py and run the tests") is True
    assert intents.classify_work("could you build the Flutter app?") is True
    assert intents.classify_work("fix it and validate it") is True
    assert intents.classify_work("make a logo and matching icon") is True
    assert intents.classify_work("generate a dashboard report") is True


def test_work_intent_does_not_hijack_questions_or_chat():
    assert intents.classify_work("how do I search folders in Python?") is False
    assert intents.classify_work("explain why this test failed") is False
    assert intents.classify_work("write me a short poem") is False
    assert intents.classify_work("hello trilobite") is False


def test_execution_intent_routes_explicit_autonomy_fleet_and_foreground():
    autonomous = intents.classify_execution(
        "Inspect the repo and keep working autonomously until the app tests pass."
    )
    fleet = intents.classify_execution(
        "Spawn as many parallel agents as the hardware allows to audit this repo."
    )
    foreground = intents.classify_execution(
        "Inspect and fix the app in the foreground only."
    )

    assert autonomous["mode"] == "autopilot"
    assert autonomous["plan_only"] is False
    assert fleet["mode"] == "fleet"
    assert foreground["mode"] == "workbench"
    assert intents.classify_execution(
        "Spawn as much subagents as possible to inspect this repo."
    )["mode"] == "fleet"
    assert intents.classify_execution(
        "Continue working on Trilobite autonomously."
    )["mode"] == "autopilot"


def test_execution_intent_routes_plan_only_and_ambiguous_compound_work():
    planned = intents.classify_execution(
        "Plan only: inspect the repo, fix the API, and validate the app tests."
    )
    compound = intents.classify_execution(
        "Inspect the repository, diagnose the failing API, and then fix the app "
        "before you run and validate all tests."
    )

    assert planned["mode"] == "autopilot"
    assert planned["plan_only"] is True
    assert compound["mode"] == "decide"
    assert {"inspect", "diagnose", "fix", "run", "validate"}.issubset(
        compound["actions"]
    )


def test_execution_intent_keeps_questions_and_no_tool_requests_in_chat():
    assert intents.classify_execution("How do I build a Flutter app?") is None
    assert intents.classify_execution("Explain only how to fix this app") is None
    assert intents.classify_execution("Write me a short poem") is None
