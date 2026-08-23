"""Focused contract tests for the bounded selfmod worker's model pin."""

import sys

from scripts import nightly_selfmod


class _FakeServer:
    def __init__(self):
        self.calls = []

    def ensemble_answer(self, prompt, *, tiers, num_predict, mode, **kwargs):
        call = {
            "prompt": prompt,
            "tiers": tiers,
            "num_predict": num_predict,
            "mode": mode,
        }
        call.update(kwargs)
        self.calls.append(call)
        return "def sample():\n    return 1\n"


def test_selfmod_model_pin_is_passed_as_an_explicit_catalog_selector():
    server = _FakeServer()

    reply = nightly_selfmod._ask(
        server,
        "rewrite one function",
        num_predict=64,
        model="qwen2.5-coder:14b",
    )

    assert reply.startswith("def sample")
    assert server.calls == [{
        "prompt": "rewrite one function",
        "tiers": "qwen2.5-coder:14b",
        "num_predict": 64,
        "mode": "code",
    }]


def test_selfmod_without_model_pin_keeps_using_code_tier():
    server = _FakeServer()

    nightly_selfmod._ask(server, "inspect", num_predict=32)

    assert server.calls[0]["tiers"] == "code"


def test_selfmod_forwards_explicit_context_without_changing_default_calls():
    server = _FakeServer()

    nightly_selfmod._ask(server, "inspect", num_predict=32, num_ctx=16384)

    assert server.calls[0]["tiers"] == "code"
    assert server.calls[0]["num_ctx"] == 16384


def test_selfmod_uses_worker_interpreter_when_worktree_has_no_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)

    assert nightly_selfmod._test_python() == sys.executable
