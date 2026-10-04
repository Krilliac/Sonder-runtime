"""Focused tests for the conservative delegated-task classifier."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.fleet_intent import classify_task


@pytest.mark.parametrize(
    "task",
    [
        "make me something useful",
        "Build a small game",
        "please create an app",
        "Could you implement this feature?",
        "write a Python script that converts CSV files",
        "I want you to develop a prototype",
        "scaffold a new project",
        "generate the component and tests",
        "fix the broken parser in parser.py",
        "refactor this module",
        "fleet: create a tool for me",
        "have the workers build an app",
        "make a game, then run its tests",
    ],
)
def test_concrete_creation_requests_are_build(task: str) -> None:
    assert classify_task(task) == "build"


@pytest.mark.parametrize(
    "task",
    [
        "What should I build for this problem?",
        "How do I implement a worker pool?",
        "Can you explain how to create an app?",
        "Explain how to build an app",
        "Tell me how to write a script",
        "We build games at work",
        "The team will create an app next year",
        "Suggest something I could make",
        "Should we build a service or a library?",
        "Review this implementation",
        "Compare two ways to build the tool",
        "Design an architecture for the game",
        "Give me a plan for building an app",
        "Just advise me about the script",
        "Write a design proposal",
        "No tools, just answer the question",
        "Do not modify files; tell me what to change",
        "What does `build an app` mean here?",
        'Discuss the phrase "create a game"',
        "build",
        "something about implementation",
        "I am unsure what to make",
        "Have the fleet review the proposed patch",
    ],
)
def test_questions_and_advice_remain_advice(task: str) -> None:
    assert classify_task(task) == "advise"


@pytest.mark.parametrize(
    "task",
    [
        "Please build the app, then explain the design",
        "Implement the plan and write the files",
        "Create a prototype; review it after",
        "fleet workers: make the script and test it",
        "Could you create a game for me?",
    ],
)
def test_explicit_implementation_wins_in_mixed_requests(task: str) -> None:
    assert classify_task(task) == "build"


@pytest.mark.parametrize("task", [None, "", "   ", 0, object()])
def test_invalid_or_empty_input_is_safe_advice(task: object) -> None:
    assert classify_task(task) == "advise"
