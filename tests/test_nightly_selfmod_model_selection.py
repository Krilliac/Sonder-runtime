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


def test_protected_and_missing_modules_are_not_eligible_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "reflection.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "safe_update.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    candidates = nightly_selfmod._eligible_candidate_files()

    assert "reflection.py" in candidates
    assert "safe_update.py" not in candidates
    assert all((tmp_path / name).is_file() for name in candidates)


def test_non_executable_objectives_are_filtered_before_a_run():
    assert not nightly_selfmod._objective_is_actionable("Add a docstring to explain this function")
    assert not nightly_selfmod._objective_is_actionable("Fix the comment describing the branch")
    assert nightly_selfmod._objective_is_actionable("Guard the empty input before indexing it")


def test_ast_splice_preserves_contract_and_sibling_code():
    original = (
        "@decorator\n"
        "def sample(value: int = 1) -> int:\n"
        "    return value\n\n\n"
        "def sibling():\n"
        "    return 2\n"
    )
    reply = (
        "@decorator\n"
        "def sample(value: int = 1) -> int:\n"
        "    if value < 0:\n"
        "        return 0\n"
        "    return value\n"
    )

    edited = nightly_selfmod._splice_function(original, reply)

    assert edited is not None
    compile(edited, "candidate.py", "exec")
    assert "def sibling():\n    return 2\n" in edited
    assert "if value < 0" in edited


def test_ast_splice_rejects_malformed_or_contract_changing_replies():
    original = "def sample(value):\n    return value\n"

    assert nightly_selfmod._splice_function(original, "def sample(value):\n    if:\n") is None
    assert nightly_selfmod._splice_function(original, "def sample(other):\n    return other\n") is None
