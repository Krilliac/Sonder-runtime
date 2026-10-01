import pytest
import intents

from sonder_runtime.application.chat.tool_intent import (
    classify_file_intent,
    suppresses_file_tools,
)


@pytest.mark.parametrize(
    "prompt",
    [
        "how many python files are in the sonder_runtime folder, and which one is the biggest?",
        "Please count the files in the workspace.",
        "what's in the sonder_runtime directory?",
        "what is in sonder_runtime folder?",
        "what is in `sonder_runtime`?",
        "Can you find TODO in sonder_runtime/application/chat?",
        "which file is largest in ./src?",
        "show me README.md",
        "please read sonder_runtime/application/chat/lanes.py",
        "list the directory tree for the project",
        "Could you inspect the contents of the repo?",
        "Summarize README.md in one sentence.",
        "Read app.dart",
        "Read README",
        "Search the Workspace for TODO",
        "What is the biggest file here?",
        "What is the size of README.md?",
        "Find files in src/utils",
        "How many test files are in this folder?",
        "Read run.py",
    ],
)
def test_local_read_questions_use_inspection(prompt):
    result = classify_file_intent(prompt)
    assert result["mode"] == "inspection"
    assert result["plan_only"] is False
    assert result["actions"] == ["read"]


@pytest.mark.parametrize(
    "prompt",
    [
        "write a python script and save it as primes.py",
        "Please create a file named notes.md in the workspace.",
        "edit sonder_runtime/application/chat/lanes.py",
        "can you save this to output.json?",
        "/delegate write a python script that prints the first 20 primes and save it as primes.py",
        "Could you modify the config file in the project?",
        "Write a Python script that prints the first 20 primes",
        "How many files are here, and save the count to count.txt",
    ],
)
def test_explicit_file_mutations_use_workbench(prompt):
    result = classify_file_intent(prompt)
    assert result["mode"] == "workbench"
    assert result["plan_only"] is False
    assert result["actions"] == ["write"]


@pytest.mark.parametrize(
    "prompt",
    [
        "how do I read a file in Python?",
        "what is a workspace?",
        "show me how to write a parser",
        "show me how to read foo.py",
        "I read the file yesterday",
        "write a short poem about files",
        "What does `find files` mean?",
        "don't read the file, just explain the API",
        "no tools, just answer how many files are typical in a package",
        "/workspace_inventory sonder_runtime",
        "find the bug and explain how to fix it",
        "read the docs, then edit app.py",
        "find README.md and delete it",
    ],
)
def test_plain_or_ambiguous_turns_stay_out_of_file_classifier(prompt):
    result = classify_file_intent(prompt)
    if prompt in {"read the docs, then edit app.py", "find README.md and delete it"}:
        assert result["mode"] == "workbench"
    else:
        assert result is None


def test_non_text_is_ordinary_chat():
    assert classify_file_intent(None) is None
    assert classify_file_intent(42) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "I saved a file yesterday.",
        "How do I write foo.py?",
        "I deleted README.md last week.",
        "What does the word file mean?",
    ],
)
def test_search_anywhere_mutations_and_concepts_stay_plain(prompt):
    assert classify_file_intent(prompt) is None


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("How do I write foo.py?", True),
        ("no file tools, just explain README.md", True),
        ("don't read the file; explain the API", True),
        ("what is in `sonder_runtime`?", False),
        ("How do I build a file parser?", True),
        ("how do I build a game?", False),
    ],
)
def test_file_tool_suppression_is_narrow(prompt, expected):
    assert suppresses_file_tools(prompt) is expected


def test_execution_classifier_prefers_read_only_file_inspection_over_legacy_work():
    result = intents.classify_execution("Read README.md")
    assert result["mode"] == "inspection"
    assert result["actions"] == ["read"]


def test_execution_classifier_keeps_compound_work_on_bounded_mode_decision():
    result = intents.classify_execution(
        "Inspect the repository, diagnose the API, and then fix the app "
        "before you run and validate all tests."
    )
    assert result["mode"] == "decide"
    assert {"inspect", "diagnose", "fix", "run", "validate"}.issubset(
        result["actions"]
    )
