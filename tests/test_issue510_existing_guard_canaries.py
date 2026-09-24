"""Issue #510 section 7: deliberate-trigger canaries for existing guards.

The Issue #510 guard inventory found these enforcement paths already present
in production code but with no test that actually trips them.  Each canary
here drives the real code path until the guard fires and asserts the guarded
work did not happen; each has a normal-traffic partner proving ordinary use
still passes.
"""
from __future__ import annotations

import server


# -- identical successful web call refusal (server._agent_turn) -------------

def _agent_with(monkeypatch, responses, observation):
    prompts, dispatches = [], []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda tool, args, **kwargs: dispatches.append((tool, dict(args))) or observation,
    )
    return prompts, dispatches


def test_canary_identical_successful_web_call_is_not_dispatched_twice(monkeypatch):
    prompts, dispatches = _agent_with(
        monkeypatch,
        [
            '{"tool":"web_search","args":{"query":"sonder release notes"}}',
            '{"tool":"web_search","args":{"query":"sonder release notes"}}',
            '{"final":"done"}',
        ],
        "1. Release notes\n   https://example.com/notes",
    )

    server._agent_impl("find the release notes", max_steps=3)

    assert dispatches == [("web_search", {"query": "sonder release notes"})]
    assert "identical web tool call already succeeded" in prompts[2]


def test_normal_distinct_web_calls_are_all_dispatched(monkeypatch):
    prompts, dispatches = _agent_with(
        monkeypatch,
        [
            '{"tool":"web_search","args":{"query":"sonder release notes"}}',
            '{"tool":"web_search","args":{"query":"sonder changelog"}}',
            '{"final":"done"}',
        ],
        "1. Result\n   https://example.com/r",
    )

    server._agent_impl("find the release notes", max_steps=3)

    assert [args["query"] for _tool, args in dispatches] == [
        "sonder release notes", "sonder changelog",
    ]
    assert "identical web tool call already succeeded" not in "\n".join(prompts)


# -- selfmod candidate tool-call and runtime budgets ------------------------

def _selfmod_run(tmp_path, *, max_tool_calls=3, max_runtime_seconds=600):
    return {
        "workspace_path": str(tmp_path),
        "files": ["a.py"],
        "budgets": {
            "max_tool_calls": max_tool_calls,
            "max_runtime_seconds": max_runtime_seconds,
            "max_files_inspected": 50,
        },
    }


def test_canary_selfmod_tool_call_budget_refuses_the_next_call(tmp_path):
    policy = server._selfmod_agent_policy(_selfmod_run(tmp_path, max_tool_calls=2))
    run_args = {"cwd": str(tmp_path), "args_json": ["python", "-V"]}

    assert policy("workspace_run", dict(run_args)) == ""
    assert policy("workspace_run", dict(run_args)) == ""
    refused = policy("workspace_run", dict(run_args))

    assert refused == "ERROR: SELFMOD POLICY: tool-call budget exhausted."
    # The bound is sticky: a different tool is refused too.
    assert "budget exhausted" in policy("file_read", {"path": "a.py"})


def test_canary_selfmod_runtime_budget_refuses_after_deadline(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["now"])
    policy = server._selfmod_agent_policy(
        _selfmod_run(tmp_path, max_tool_calls=100, max_runtime_seconds=30),
    )
    assert policy("file_read", {"path": "a.py"}) == ""
    clock["now"] += 31

    refused = policy("file_read", {"path": "a.py"})

    assert refused == "ERROR: SELFMOD POLICY: total runtime budget exhausted."


def test_normal_selfmod_calls_within_budget_are_admitted(tmp_path):
    policy = server._selfmod_agent_policy(_selfmod_run(tmp_path, max_tool_calls=3))
    results = [
        policy("file_read", {"path": "a.py"}),
        policy("file_write", {"path": "a.py", "content": "x = 1\n"}),
        policy("workspace_run", {"cwd": str(tmp_path), "args_json": ["python", "a.py"]}),
    ]

    assert results == ["", "", ""]
