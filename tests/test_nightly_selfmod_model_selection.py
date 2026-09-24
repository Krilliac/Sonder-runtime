"""Focused contract tests for the bounded selfmod worker's model pin."""

import json
import os
import sys
import subprocess
import hashlib
from pathlib import Path

import pytest

from scripts import nightly_selfmod, selfmod_host_grader


def test_parent_grader_extracts_only_literal_assertions_from_trusted_suite(tmp_path):
    suite = tmp_path / "test_reflection.py"
    suite.write_text(
        "import reflection as target\n"
        "def test_simple():\n"
        "    assert target.answer(4) == 42\n"
        "def test_dynamic_input():\n"
        "    assert target.answer(value) == 42\n"
        "def test_other():\n"
        "    assert target.different(4) == 42\n",
        encoding="utf-8",
    )
    assert selfmod_host_grader.extract_cases([suite], "reflection", "answer") == (
        {"args": [4], "kwargs": {}, "expected": 42},
    )
    assert selfmod_host_grader.extract_cases([suite], "reflection", "different") == (
        {"args": [4], "kwargs": {}, "expected": 42},
    )
    assert not selfmod_host_grader.extract_cases([suite], "reflection", "missing")


def test_parent_grader_leaves_fixture_and_setup_dependent_tests_unevaluated(tmp_path):
    suite = tmp_path / "test_bootstrap_engine.py"
    suite.write_text(
        "import bootstrap_engine as target\n"
        "def test_main_under_different_patches(monkeypatch):\n"
        "    monkeypatch.setattr(target, 'result', 0)\n"
        "    assert target.main([]) == 0\n"
        "    monkeypatch.setattr(target, 'result', 4)\n"
        "    assert target.main([]) == 4\n"
        "def test_literal_assertion_before_local_setup():\n"
        "    assert target.main([]) == 0\n"
        "    setup = target.configure_for_test()\n"
        "    assert setup is not None\n"
        "def test_setup_before_assertion_is_not_projected():\n"
        "    target.configure_for_test()\n"
        "    assert target.main([]) == 4\n"
        "@pytest.mark.parametrize('result', [0, 4])\n"
        "def test_parametrized_case_is_not_projected():\n"
        "    assert target.main([]) == 0\n",
        encoding="utf-8",
    )

    assert selfmod_host_grader.extract_cases(
        [suite], "bootstrap_engine", "main"
    ) == ({"args": [[]], "kwargs": {}, "expected": 0},)


def test_parent_grader_extracts_multiple_direct_assertions_from_setup_free_test(tmp_path):
    suite = tmp_path / "test_reflection.py"
    suite.write_text(
        "import reflection as target\n"
        "def test_literal_cases():\n"
        "    assert target.answer(4) == 42\n"
        "    assert target.answer(value=5) == 43\n",
        encoding="utf-8",
    )

    assert selfmod_host_grader.extract_cases([suite], "reflection", "answer") == (
        {"args": [4], "kwargs": {}, "expected": 42},
        {"args": [], "kwargs": {"value": 5}, "expected": 43},
    )


@pytest.mark.parametrize(
    "module_setup",
    (
        "def setup_function():\n    configure()\n",
        "pytestmark = pytest.mark.usefixtures('configured')\n",
        "@pytest.fixture(autouse=True)\ndef configured():\n    configure()\n",
    ),
)
def test_parent_grader_leaves_modules_with_pytest_setup_unevaluated(
    tmp_path, module_setup
):
    suite = tmp_path / "test_reflection.py"
    suite.write_text(
        "import reflection as target\n"
        + module_setup
        + "def test_literal_case():\n"
        "    assert target.answer(4) == 42\n",
        encoding="utf-8",
    )

    assert not selfmod_host_grader.extract_cases([suite], "reflection", "answer")


def test_parent_grader_leaves_suites_with_inherited_autouse_fixture_unevaluated(
    tmp_path,
):
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def configure():\n"
        "    prepare_test_environment()\n",
        encoding="utf-8",
    )
    suite = tmp_path / "test_reflection.py"
    suite.write_text(
        "import reflection as target\n"
        "def test_literal_case():\n"
        "    assert target.answer(4) == 42\n",
        encoding="utf-8",
    )

    assert not selfmod_host_grader.extract_cases([suite], "reflection", "answer")


def test_parent_grade_rejects_candidate_pytest_exit_or_report_spoof(tmp_path):
    cases = ({"args": [4], "kwargs": {}, "expected": 42},)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    target = candidate / "reflection.py"
    target.write_text("def answer(value): return 42\n", encoding="utf-8")
    command, nonce = selfmod_host_grader.challenge(candidate, "reflection", "answer", cases)

    honest = subprocess.run(command, cwd=candidate, text=True, capture_output=True,
                            timeout=10, check=False)
    assert honest.returncode == 0
    assert selfmod_host_grader.grade(honest.stdout, nonce, cases)[0]

    target.write_text("def answer(value): return 43\n", encoding="utf-8")
    wrong = subprocess.run(command, cwd=candidate, text=True, capture_output=True,
                           timeout=10, check=False)
    assert wrong.returncode == 0
    assert selfmod_host_grader.grade(wrong.stdout, nonce, cases) == (
        False, "candidate outputs differ from parent-held assertions",
    )

    target.write_text("import os\nprint('1 passed in 0.01s', flush=True)\nos._exit(0)\n",
                      encoding="utf-8")
    spoof = subprocess.run(command, cwd=candidate, text=True, capture_output=True,
                           timeout=10, check=False)
    assert spoof.returncode == 0 and "1 passed" in spoof.stdout
    assert selfmod_host_grader.grade(spoof.stdout, nonce, cases) == (
        False, "host challenge has no unique bounded result",
    )


@pytest.mark.parametrize("replay_ok", [True, False])
def test_nightly_parent_grader_requires_clean_replay(tmp_path, monkeypatch, replay_ok):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "reflection.py").write_text("def answer(): return 42\n", encoding="utf-8")
    held_out = {"host_cases": ({"args": [], "kwargs": {}, "expected": 42},),
                "protected_paths": ()}
    recorded = []

    # This unit seam exercises the production parent scorer, not Windows MIC.
    # The native supervisor proves its own boundary on the Windows CI runner.
    def low_probe(_run_id, _kind, command, **_kwargs):
        result = subprocess.run(command, cwd=candidate, capture_output=True,
                                text=True, timeout=10, check=False)
        return {"passed": result.returncode == 0, "output": result.stdout,
                "isolation": "low", "test_id": 7}

    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: sys.executable)
    monkeypatch.setattr(nightly_selfmod, "_record_candidate_test", low_probe)
    monkeypatch.setattr(nightly_selfmod.selfmod, "get_run", lambda _id: {"starting_commit": "base"})
    monkeypatch.setattr(nightly_selfmod.selfmod, "tested_digests", lambda _id: {
        "files": {"reflection.py": hashlib.sha256(
            (candidate / "reflection.py").read_bytes()).hexdigest()},
    })
    monkeypatch.setattr(nightly_selfmod.selfmod, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(selfmod_host_grader, "clean_replay", lambda *_a, **_kw: (
        replay_ok, "verified" if replay_ok else "untrusted extra file",
    ))

    def host_grade(_run_id, _probe_id, **kwargs):
        recorded.append(kwargs)
        return {"passed": kwargs["passed"], "detail": kwargs["detail"]}
    monkeypatch.setattr(nightly_selfmod.selfmod, "record_host_grade", host_grade)

    result = nightly_selfmod._parent_scored_gate(
        "test-run", candidate, "reflection.py", "answer", held_out, 10,
    )
    assert result["passed"] is replay_ok
    assert recorded and recorded[0]["passed"] is replay_ok
    assert "clean checkout" in recorded[0]["detail"]


def test_clean_host_replay_uses_only_bound_files_from_fresh_git_checkout(tmp_path, monkeypatch):
    from scripts import selfmod_low_integrity

    repository = tmp_path / "base"
    repository.mkdir()
    (repository / "reflection.py").write_text("def answer(): return -1\n", encoding="utf-8")
    for args in (["init", "--initial-branch=main"],
                 ["config", "user.email", "selfmod@test.invalid"],
                 ["config", "user.name", "Selfmod Test"], ["add", "."],
                 ["commit", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository,
                            capture_output=True, text=True, check=True).stdout.strip()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    source = candidate / "reflection.py"
    source.write_text("def answer(): return 42\n", encoding="utf-8")
    cases = ({"args": [], "kwargs": {}, "expected": 42},)

    def pretend_low_supervisor(command, *, cwd, **_kwargs):
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                   timeout=10, check=False)
        return {"exit_code": completed.returncode, "passed": completed.returncode == 0,
                "output": completed.stdout, "job": {"integrity": "low"}}

    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", pretend_low_supervisor)
    def replay():
        return selfmod_host_grader.clean_replay(
            repository, candidate, tmp_path / "state", commit,
            {"reflection.py": hashlib.sha256(source.read_bytes()).hexdigest()},
            "reflection", "answer", cases, 10, python=sys.executable,
        )

    assert replay()[0] is True
    (candidate / "helper.py").write_text("def value(): return 42\n", encoding="utf-8")
    source.write_text("def answer():\n    from helper import value\n    return value()\n",
                      encoding="utf-8")
    assert replay()[0] is False


class _FakeServer:
    def __init__(self):
        self.calls = []
        self.BASE = "http://127.0.0.1:11434"
        self.OLLAMA_POOL = type(
            "BrokenRemotePool", (),
            {"request": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("remote pool must not be used"))},
        )()

    class ollama_endpoint:
        @staticmethod
        def is_loopback(base):
            return True

        @staticmethod
        def open_url(request, timeout, allow_remote):
            import json
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def read(self, _limit):
                    return json.dumps({"models": [{"name": "qwen2.5-coder:14b"}]}).encode()
            return Response()

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

    @staticmethod
    def _is_cloud_model_name(model):
        return str(model).endswith(":cloud")

    def _make_generate(self, model, _system, _temperature, num_predict, num_ctx, **kwargs):
        self.calls.append({"gateway_model": model, "gateway_num_predict": num_predict,
                           "gateway_num_ctx": num_ctx, **kwargs})
        return lambda prompt: "def sample():\n    return 1\n"


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
        "gateway_model": "qwen2.5-coder:14b",
        "gateway_num_predict": 64,
        "gateway_num_ctx": 0,
        "cloud": False,
        "timeout": 60,
    }]


def test_selfmod_model_pin_accepts_a_bounded_request_timeout():
    server = _FakeServer()

    nightly_selfmod._ask(
        server, "inspect", num_predict=32,
        model="qwen2.5-coder:14b", timeout=7,
    )

    assert server.calls[-1]["timeout"] == 7


def test_objective_proposal_deadline_stops_before_another_model_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        nightly_selfmod, "_eligible_candidate_files",
        lambda: ["reflection.py", "memory_quality.py"],
    )
    monkeypatch.setattr(
        nightly_selfmod.time, "monotonic", lambda: 10.0,
    )
    monkeypatch.setattr(
        nightly_selfmod, "_ask",
        lambda *args, **kwargs: calls.append(kwargs) or "NONE",
    )

    result = nightly_selfmod.propose_objective(
        object(), lambda _message: None, deadline=9.0,
    )

    assert result is None
    assert calls == []


def test_objective_proposal_caps_each_request_to_remaining_window(monkeypatch):
    calls = []
    clock = iter((10.0, 19.0, 21.0))
    monkeypatch.setattr(
        nightly_selfmod, "_eligible_candidate_files",
        lambda: ["reflection.py", "memory_quality.py"],
    )
    monkeypatch.setattr(nightly_selfmod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        nightly_selfmod, "_ask",
        lambda *args, **kwargs: calls.append(kwargs) or "NONE",
    )

    result = nightly_selfmod.propose_objective(
        object(), lambda _message: None, deadline=20.0,
    )

    assert result is None
    assert [call["timeout"] for call in calls] == [10, 1]


def test_selfmod_refuses_unknown_or_cloud_explicit_models():
    server = _FakeServer()
    try:
        nightly_selfmod._ask(server, "inspect", model="missing:latest")
    except RuntimeError as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("unknown model must be refused")
    try:
        nightly_selfmod._ask(server, "inspect", model="qwen:cloud")
    except RuntimeError as exc:
        assert "cloud-backed" in str(exc)
    else:
        raise AssertionError("cloud model must be refused")


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


_LOW_MARKS = "not requires_medium_integrity and not heavy_memory"
_BASE = ["python", "-m", "pytest", "-vv", "--maxfail=1", "-p", "no:cacheprovider"]


def _xdist(monkeypatch, available):
    monkeypatch.setattr(nightly_selfmod.subprocess, "run", lambda *args, **kwargs: type(
        "Result", (), {"returncode": 0 if available else 1}
    )())


def test_regression_command_uses_bounded_xdist_workers(monkeypatch):
    _xdist(monkeypatch, True)
    assert nightly_selfmod._regression_command("python", workers=4) == [
        *_BASE, "-m", _LOW_MARKS, "-n", "4", "--dist", "load",
        "--ignore", "tests/test_selfmod_low_integrity.py",
    ]


def test_regression_command_falls_back_to_serial_without_xdist(monkeypatch):
    _xdist(monkeypatch, False)
    assert nightly_selfmod._regression_command("python", workers=4) == [
        *_BASE, "-m", _LOW_MARKS,
        "--ignore", "tests/test_selfmod_low_integrity.py",
    ]


def test_regression_excludes_the_separate_held_out_suite(monkeypatch):
    _xdist(monkeypatch, False)
    assert nightly_selfmod._regression_command(
        "python", ignore_paths=("tests/test_reflection.py",)
    ) == [
        *_BASE, "-m", _LOW_MARKS,
        "--ignore", "tests/test_selfmod_low_integrity.py",
        "--ignore", "tests/test_reflection.py",
    ]


def test_candidate_gates_never_select_medium_integrity_tests():
    kinds = dict(nightly_selfmod._REGRESSION_PARTITIONS)
    assert set(kinds) == {"regression", "regression_heavy"}
    assert nightly_selfmod.UNEVALUATED_PARTITION[0] == "regression_medium"
    for medium in (False, True):
        for heavy in (False, True):
            env = {"requires_medium_integrity": medium, "heavy_memory": heavy}
            selected = [k for k, expr in kinds.items() if eval(expr, {}, env)]
            # Medium-integrity tests are never run against the candidate;
            # every other test lands in exactly one gated partition.
            assert len(selected) == (0 if medium else 1), (env, selected)


def test_only_the_low_partition_runs_xdist(monkeypatch):
    _xdist(monkeypatch, True)
    command = nightly_selfmod._regression_command("python", kind="regression_heavy", workers=8)
    assert "-n" not in command
    assert command[command.index("-m", 3) + 1] == dict(
        nightly_selfmod._REGRESSION_PARTITIONS)["regression_heavy"]


def test_regression_isolation_never_leaves_low_integrity():
    low = nightly_selfmod._regression_isolation("regression", 6)
    assert "integrity" not in low and low["job_memory_mb"] >= 2048 * 6
    heavy = nightly_selfmod._regression_isolation("regression_heavy", 6)
    assert "integrity" not in heavy and heavy["process_memory_mb"] > 2048
    assert nightly_selfmod._regression_isolation("regression_medium", 6) == {}


def test_regression_workers_are_bounded(monkeypatch):
    monkeypatch.setenv("SONDER_SELFMOD_REGRESSION_WORKERS", "64")
    assert nightly_selfmod._regression_workers() == 12
    monkeypatch.setenv("SONDER_SELFMOD_REGRESSION_WORKERS", "3")
    assert nightly_selfmod._regression_workers() == 3


def test_default_workers_follow_commit_headroom(monkeypatch):
    monkeypatch.delenv("SONDER_SELFMOD_REGRESSION_WORKERS", raising=False)
    monkeypatch.setattr(nightly_selfmod.os, "cpu_count", lambda: 24)
    monkeypatch.setattr(nightly_selfmod, "_commit_headroom_mb", lambda: 6 * 1024)
    assert nightly_selfmod._regression_workers() == 1
    monkeypatch.setattr(nightly_selfmod, "_commit_headroom_mb", lambda: 24 * 1024)
    assert nightly_selfmod._regression_workers() == 4
    monkeypatch.setattr(nightly_selfmod, "_commit_headroom_mb", lambda: 400 * 1024)
    assert nightly_selfmod._regression_workers() == 8
    monkeypatch.setattr(nightly_selfmod, "_commit_headroom_mb", lambda: None)
    assert nightly_selfmod._regression_workers() == 2


def _drive_to_gates(tmp_path, monkeypatch, *, mode="propose", on_gate=None, on_review=None):
    """Run nightly_selfmod.run() to its gates with every external effect faked."""
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    original = "def selected(value):\n    return value\n"
    edited = "def selected(value):\n    return value or 0\n"
    (workspace / "reflection.py").write_text(original, encoding="utf-8")
    calls = {"gates": [], "review": [], "approve": [], "deploy": [], "reject": [], "git": []}
    s = nightly_selfmod.selfmod
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    monkeypatch.setattr(s, "settings", lambda: {"enabled": True, "mode": mode})
    monkeypatch.setattr(s, "_git_info", lambda _root: (True, "base", ""))
    monkeypatch.setattr(nightly_selfmod, "propose_objective",
                        lambda *_a, **_k: ("reflection.py", "Guard input.", "selected"))
    monkeypatch.setattr(s, "create_plan", lambda *_a, **_k: {"id": "run-bind"})
    for name in ("create_backup", "verify_backup", "prepare_workspace", "begin_testing"):
        monkeypatch.setattr(s, name, lambda _run_id: None)
    monkeypatch.setattr(s, "candidate_path", lambda _run_id: workspace)
    monkeypatch.setattr(nightly_selfmod, "_ask", lambda *_a, **_k: edited)
    monkeypatch.setattr(nightly_selfmod, "_rewrite_reply_objection", lambda _reply: None)
    monkeypatch.setattr(nightly_selfmod, "_splice_function", lambda _o, reply, **_k: reply)
    monkeypatch.setattr(nightly_selfmod, "_diff_objection", lambda _o, _e: None)
    monkeypatch.setattr(s, "apply_candidate_changes", lambda _run_id, files: [
        (workspace / rel).write_text(text, encoding="utf-8") for rel, text in files.items()])

    def inspect(_run_id):
        text = (workspace / "reflection.py").read_text(encoding="utf-8")
        return {"diff": "diff:" + text, "changed_files": ["reflection.py"]}
    monkeypatch.setattr(s, "inspect_diff", inspect)
    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: "python")
    monkeypatch.setattr(nightly_selfmod, "_ruff_command", lambda _py: None)
    monkeypatch.setattr(nightly_selfmod, "_regression_workers", lambda: 1)
    monkeypatch.setattr(nightly_selfmod.subprocess, "run", lambda *a, **k: type(
        "Result", (), {"returncode": 1, "stdout": b""})())
    monkeypatch.setattr(nightly_selfmod, "_prepare_held_out", lambda *_a: {
        "source_paths": (), "command": ["python", "-c", "pass"], "cleanup": None,
        "protected_paths": ()})

    def gate(run_id, kind, command, **kwargs):
        calls["gates"].append((kind, kwargs.get("isolation")))
        if on_gate:
            on_gate(kind, workspace)
        return {"passed": True, "exit_code": 0}
    monkeypatch.setattr(nightly_selfmod, "_record_candidate_test", gate)

    def review(run_id, **kwargs):
        calls["review"].append(kwargs)
        if on_review:
            on_review(workspace)
        return {"phase": "reviewing"}
    monkeypatch.setattr(s, "review", review)
    monkeypatch.setattr(s, "approve", lambda run_id, **k: calls["approve"].append(run_id))
    monkeypatch.setattr(s, "deploy", lambda run_id, **k: calls["deploy"].append(k))
    monkeypatch.setattr(s, "get_run", lambda run_id: {"deployed_commit": ""})
    monkeypatch.setattr(s, "reject", lambda run_id, reason="": calls["reject"].append(reason))
    monkeypatch.setattr(s, "_git", lambda _ws, *args: (calls["git"].append(args), (0, "abc"))[1])
    monkeypatch.setattr(nightly_selfmod, "_discard_workspace", lambda _run_id: None)
    return calls


def test_candidate_mutating_its_file_during_a_gate_is_rejected(tmp_path, monkeypatch):
    def mutate(kind, workspace):
        if kind == "regression":
            (workspace / "reflection.py").write_text("def selected(value):\n    return 1\n")

    calls = _drive_to_gates(tmp_path, monkeypatch, on_gate=mutate)
    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60, branch=True)

    assert result.startswith("candidate rejected: tested bytes changed for reflection.py")
    assert calls["review"] == [] and calls["git"] == []
    assert calls["reject"] and "binding mismatch before review" in calls["reject"][0]


def test_candidate_mutated_after_review_is_not_committed(tmp_path, monkeypatch):
    def mutate(workspace):
        (workspace / "reflection.py").write_text("def selected(value):\n    return 2\n")

    calls = _drive_to_gates(tmp_path, monkeypatch, on_review=mutate)
    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60, branch=True)

    assert "before commit" in result and calls["git"] == []


def test_medium_tests_never_run_and_block_unattended_promotion(tmp_path, monkeypatch):
    calls = _drive_to_gates(tmp_path, monkeypatch, mode="auto-low-risk")
    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60, branch=False)

    kinds = [kind for kind, _ in calls["gates"]]
    assert "regression_medium" not in kinds
    assert all(not (iso or {}).get("integrity") for _, iso in calls["gates"])
    assert calls["review"][0]["unevaluated"]
    assert "regression_medium" in calls["review"][0]["unevaluated"][0]
    assert calls["approve"] == [] and calls["deploy"] == []
    assert "READY for review" in result and "NOT EVALUATED" in result


def test_nightly_rejects_a_candidate_that_fails_parent_scored_grade(tmp_path, monkeypatch):
    calls = _drive_to_gates(tmp_path, monkeypatch)
    monkeypatch.setattr(nightly_selfmod, "_prepare_held_out", lambda *_args: {
        "source_paths": (), "command": ["python", "-c", "pass"],
        "cleanup": None, "protected_paths": (),
        "host_cases": ({"args": [], "kwargs": {}, "expected": 42},),
    })
    monkeypatch.setattr(nightly_selfmod, "_parent_scored_gate", lambda *_args: {
        "passed": False, "detail": "candidate printed a fake pytest report",
    })

    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60, branch=True)
    assert result == "candidate rejected: parent-scored host grade failed"
    assert calls["review"] == [] and calls["git"] == []
    assert "parent-scored host grade failed" in calls["reject"]


def test_protected_and_missing_modules_are_not_eligible_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "reflection.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "safe_update.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    candidates = nightly_selfmod._eligible_candidate_files()

    assert "reflection.py" in candidates
    assert "safe_update.py" not in candidates
    assert all((tmp_path / name).is_file() for name in candidates)


def test_held_out_suite_is_selected_from_the_base_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "def test_holdout():\n    assert True\n", encoding="utf-8"
    )
    assert nightly_selfmod._held_out_suite_paths("reflection.py") == (
        "tests/test_reflection.py",
    )


def test_missing_held_out_suite_is_an_explicit_failure_command(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    prepared = nightly_selfmod._prepare_held_out("unmapped.py", tmp_path, 60)
    assert prepared["source_paths"] == ()
    assert "held-out evaluator unavailable" in prepared["command"][2]
    assert prepared["cleanup"] is None


def test_held_out_runner_executes_snapshot_against_candidate_root(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from reflection import answer\n\n"
        "import time\n\n"
        "def test_answer_is_stable():\n"
        "    assert answer() == 42\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "reflection.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 60, "answer")
    assert prepared["source_paths"] == ("tests/test_reflection.py",)
    assert prepared["host_cases"] == ({"args": [], "kwargs": {}, "expected": 42},)
    assert nightly_selfmod._regression_command(
        "python", ignore_paths=prepared["source_paths"]
    )[-2:] == ["--ignore", "tests/test_reflection.py"]
    try:
        result = subprocess.run(
            prepared["command"], cwd=candidate, text=True,
            capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CANARY PASSED" in result.stdout
    finally:
        if prepared["cleanup"] is not None:
            prepared["cleanup"].cleanup()


def test_held_out_snapshot_keeps_resources_without_ignoring_them(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.txt").write_text("held-out resource", encoding="utf-8")
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from pathlib import Path\n"
        "from reflection import answer\n\n"
        "def test_answer_uses_resource():\n"
        "    assert Path(__file__).with_name('fixture.txt').read_text() == 'held-out resource'\n"
        "    assert answer() == 42\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "reflection.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 60)
    assert prepared["source_paths"] == ("tests/test_reflection.py",)
    payload = json.loads(prepared["command"][-1])
    assert any(path["path"].endswith("fixture.txt") for path in payload["files"])
    try:
        result = subprocess.run(
            prepared["command"], cwd=candidate, text=True,
            capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        if prepared["cleanup"] is not None:
            prepared["cleanup"].cleanup()


def test_held_out_snapshot_rejects_bounded_file_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    monkeypatch.setattr(nightly_selfmod, "_HELD_OUT_MAX_FILES", 1)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "def test_answer():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "fixture.txt").write_text("overflow", encoding="utf-8")
    prepared = nightly_selfmod._prepare_held_out("reflection.py", tmp_path / "candidate", 60)
    assert "snapshot exceeds file limit" in prepared["command"][2]
    assert prepared["cleanup"] is None


def test_held_out_snapshot_rejects_symlink_outside_test_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_reflection.py").write_text(
        "def test_answer():\n    assert True\n", encoding="utf-8"
    )
    secret = tmp_path / "outside.txt"
    secret.write_text("outside", encoding="utf-8")
    link = tests / "outside-link.txt"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    prepared = nightly_selfmod._prepare_held_out("reflection.py", tmp_path / "candidate", 60)
    assert "snapshot contains a symlink" in prepared["command"][2]
    assert prepared["cleanup"] is None


def test_held_out_snapshot_rejects_candidate_mutation(tmp_path, monkeypatch):
    # The snapshot's protection is read-only file modes; root bypasses them,
    # so the candidate's write succeeds and the integrity check (correctly)
    # fails the run. That is the tamper-evident fallback working, not the
    # write-prevention this test asserts, which only a non-root user can see.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses the snapshot's read-only permissions")
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from reflection import answer\n\n"
        "def test_answer_is_stable():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 60)
    snapshot_test = json.loads(prepared["command"][-1])["files"][0]["path"]
    (candidate / "reflection.py").write_text(
        "from pathlib import Path\n"
        "def answer():\n"
        "    try:\n"
        "        Path(%r).write_text('tampered', encoding='utf-8')\n"
        "    except OSError:\n"
        "        pass\n"
        "    return 42\n" % snapshot_test,
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            prepared["command"], cwd=candidate, text=True,
            capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert Path(snapshot_test).read_text(encoding="utf-8").startswith("from reflection")
    finally:
        if prepared["cleanup"] is not None:
            prepared["cleanup"].cleanup()


def test_held_out_runner_bounds_hostile_output(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from reflection import answer\n\n"
        "def test_answer_is_stable():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "reflection.py").write_text(
        "print('x' * 200000)\n"
        "def answer():\n    return 42\n",
        encoding="utf-8",
    )
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 60)
    try:
        result = subprocess.run(
            prepared["command"], cwd=candidate, text=True,
            capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OUTPUT TRUNCATED" in result.stdout
        assert len(result.stdout) < 20000
    finally:
        if prepared["cleanup"] is not None:
            prepared["cleanup"].cleanup()


def test_held_out_timeout_terminates_windows_descendants(tmp_path, monkeypatch):
    if sys.platform != "win32":
        return
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from reflection import answer\n\n"
        "import time\n\n"
        "def test_answer_is_stable():\n"
        "    assert answer() == 42\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    marker = tmp_path / "child.pid"
    (candidate / "reflection.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path(%r).write_text(str(child.pid), encoding='ascii')\n"
        "def answer():\n    return 42\n" % str(marker),
        encoding="utf-8",
    )
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 1)
    try:
        result = subprocess.run(
            prepared["command"], cwd=candidate, text=True,
            capture_output=True, timeout=20,
        )
        assert result.returncode == 124, result.stdout + result.stderr
        if marker.is_file():
            child_pid = marker.read_text(encoding="ascii")
            tasklist = subprocess.run(
                ["tasklist", "/FI", "PID eq %s" % child_pid],
                capture_output=True, text=True, check=False,
            )
            assert child_pid not in tasklist.stdout
    finally:
        if prepared["cleanup"] is not None:
            prepared["cleanup"].cleanup()


def test_non_executable_objectives_are_filtered_before_a_run():
    assert not nightly_selfmod._objective_is_actionable("Add a docstring to explain this function")
    assert not nightly_selfmod._objective_is_actionable("Fix the comment describing the branch")
    assert nightly_selfmod._objective_is_actionable("Guard the empty input before indexing it")


def test_proposal_function_inventory_is_compact_and_top_level_only():
    source = (
        "def first(value):\n"
        "    def nested():\n"
        "        return value\n"
        "    return nested()\n\n"
        "async def second():\n"
        "    return 2\n"
    )
    assert nightly_selfmod._proposal_function_inventory(source) == "first, second"


def test_proposal_inventory_can_be_bounded_to_the_visible_source_slice():
    source = "def visible():\n    return 1\n" + ("# filler\n" * 20_000) + (
        "\ndef hidden():\n    return 2\n"
    )
    visible = source[:60_000]
    assert "visible" in nightly_selfmod._proposal_function_inventory(visible)
    assert "hidden" not in nightly_selfmod._proposal_function_inventory(visible)


def test_grounding_accepts_concrete_duplicate_claim_with_one_rewrite_target():
    source = (
        "COMMANDS = ['/foo', '/foo']\n\n"
        "def remove_duplicate_commands():\n"
        "    return [item for item in COMMANDS if item != '/foo']\n"
    )
    assert nightly_selfmod._objective_is_grounded(
        "Remove duplicate '/foo' command entries.",
        "The '/foo' entry appears twice.",
        source,
    )
    assert nightly_selfmod._objective_target_function(
        "Remove duplicate '/foo' command entries.",
        "The '/foo' entry appears twice.",
        source,
    ) == "remove_duplicate_commands"


def test_grounding_rejects_false_duplicate_and_declarative_targets():
    one_entry = (
        "COMMANDS = ['/foo']\n\n"
        "def remove_duplicate_commands():\n"
        "    return COMMANDS\n"
    )
    declarative = "COMMANDS = ['/foo', '/foo']\n"
    assert not nightly_selfmod._objective_is_grounded(
        "Remove duplicate '/foo' command entries.",
        "The '/foo' entry appears twice.",
        one_entry,
    )
    assert not nightly_selfmod._objective_is_grounded(
        "Remove duplicate '/foo' command entries.",
        "The '/foo' entry appears twice.",
        declarative,
    )


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


def test_ast_splice_rejects_a_different_existing_function_than_selected():
    original = (
        "def selected(value):\n"
        "    return value\n\n"
        "def other(value):\n"
        "    return value + 1\n"
    )
    reply = "def other(value):\n    return value + 2\n"

    assert nightly_selfmod._splice_function(
        original, reply, expected_name="selected",
    ) is None


def test_ast_splice_accepts_the_selected_existing_function():
    original = (
        "def selected(value):\n"
        "    return value\n\n"
        "def other(value):\n"
        "    return value + 1\n"
    )
    reply = "def selected(value):\n    return value + 1\n"

    edited = nightly_selfmod._splice_function(
        original, reply, expected_name="selected",
    )

    assert edited is not None
    assert "def selected(value):\n    return value + 1\n" in edited


def test_rewrite_prompt_binds_selected_function_objective_target_and_source():
    prompt = nightly_selfmod._rewrite_prompt(
        "Guard the empty input.",
        "pull_community.py",
        "load_source",
        "def load_source(src):\n    return src\n",
    )

    assert "the objective in the selected function `load_source`" in prompt
    assert "Guard the empty input." in prompt
    assert "=== pull_community.py (selected function: load_source) ===" in prompt
    assert prompt.endswith("def load_source(src):\n    return src\n")


def test_rewrite_reply_classifies_comment_only_and_none_as_no_change():
    assert nightly_selfmod._rewrite_reply_objection("# add a guard\n# done") == (
        "comment-only rewrite reply"
    )
    assert nightly_selfmod._rewrite_reply_objection("NONE") == (
        "model reported no executable change"
    )
    assert nightly_selfmod._rewrite_reply_objection(
        "def selected(value):\n    return value + 1\n"
    ) is None


def test_run_cleans_plan_when_rewrite_request_raises(tmp_path, monkeypatch):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "reflection.py").write_text(
        "def selected(value):\n    return value\n", encoding="utf-8",
    )
    cancelled, discarded = [], []
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    monkeypatch.setattr(
        nightly_selfmod.selfmod, "settings",
        lambda: {"enabled": True, "mode": "propose"},
    )
    monkeypatch.setattr(
        nightly_selfmod.selfmod, "_git_info",
        lambda _root: (True, "base", ""),
    )
    monkeypatch.setattr(
        nightly_selfmod, "propose_objective",
        lambda *_args, **_kwargs: ("reflection.py", "Guard input.", "selected"),
    )
    monkeypatch.setattr(
        nightly_selfmod.selfmod, "create_plan",
        lambda *_args, **_kwargs: {"id": "run-rewrite-error"},
    )
    monkeypatch.setattr(nightly_selfmod.selfmod, "create_backup", lambda _run_id: None)
    monkeypatch.setattr(nightly_selfmod.selfmod, "verify_backup", lambda _run_id: None)
    monkeypatch.setattr(nightly_selfmod.selfmod, "prepare_workspace", lambda _run_id: None)
    monkeypatch.setattr(
        nightly_selfmod.selfmod, "candidate_path", lambda _run_id: workspace,
    )
    monkeypatch.setattr(
        nightly_selfmod.selfmod, "cancel",
        lambda run_id: cancelled.append(run_id),
    )
    def discard(run_id):
        discarded.append(run_id)
        (workspace / "reflection.py").unlink()
        workspace.rmdir()
    monkeypatch.setattr(nightly_selfmod, "_discard_workspace", discard)
    monkeypatch.setattr(
        nightly_selfmod, "_ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private model detail")),
    )

    result = nightly_selfmod.run(object(), lambda _message: None, test_timeout=60)

    assert result == "candidate rejected: rewrite request failed (RuntimeError)"
    assert cancelled == discarded == ["run-rewrite-error"]
    assert not workspace.exists()


def test_ast_splice_rejects_malformed_or_contract_changing_replies():
    original = "def sample(value):\n    return value\n"

    assert nightly_selfmod._splice_function(original, "def sample(value):\n    if:\n") is None
    assert nightly_selfmod._splice_function(original, "def sample(other):\n    return other\n") is None


def test_committed_digests_match_binding_for_real_git_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "reflection.py").write_bytes(b"def selected(value):\n    return value\n")
    subprocess.run(["git", "add", "reflection.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=repo, check=True, capture_output=True)
    binding = nightly_selfmod._candidate_binding(repo, ["reflection.py"], "d")
    assert nightly_selfmod._committed_digests(repo, binding["files"]) == binding["files"]
    (repo / "reflection.py").write_bytes(b"changed\n")
    changed = nightly_selfmod._candidate_binding(repo, ["reflection.py"], "d")
    assert nightly_selfmod._committed_digests(repo, changed["files"]) != changed["files"]


def test_committed_bytes_mismatch_rejects_and_deletes_branch(tmp_path, monkeypatch):
    # The faked `git show` returns empty bytes, so the committed blob differs
    # from the tested bytes.
    calls = _drive_to_gates(tmp_path, monkeypatch)
    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60, branch=True)

    assert result.startswith("candidate rejected: committed bytes differ")
    assert ("branch", "-D", "selfmod/run-bind") in calls["git"]
    assert calls["reject"] and "after commit" in calls["reject"][0]
