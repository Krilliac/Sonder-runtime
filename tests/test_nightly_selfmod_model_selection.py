"""Focused contract tests for the bounded selfmod worker's model pin."""

import json
import sys
import subprocess
from pathlib import Path

import pytest

from scripts import nightly_selfmod


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


def test_regression_command_uses_bounded_four_worker_xdist(monkeypatch):
    monkeypatch.setattr(nightly_selfmod.subprocess, "run", lambda *args, **kwargs: type(
        "Result", (), {"returncode": 0}
    )())
    assert nightly_selfmod._regression_command("python") == [
        "python", "-m", "pytest", "-q", "--maxfail=1", "-n", "4", "--dist", "load",
        "--ignore", "tests/test_selfmod_low_integrity.py",
    ]


def test_regression_command_falls_back_to_serial_without_xdist(monkeypatch):
    monkeypatch.setattr(nightly_selfmod.subprocess, "run", lambda *args, **kwargs: type(
        "Result", (), {"returncode": 1}
    )())
    assert nightly_selfmod._regression_command("python") == [
        "python", "-m", "pytest", "-q", "--maxfail=1",
        "--ignore", "tests/test_selfmod_low_integrity.py",
    ]


def test_regression_excludes_the_separate_held_out_suite(monkeypatch):
    monkeypatch.setattr(nightly_selfmod.subprocess, "run", lambda *args, **kwargs: type(
        "Result", (), {"returncode": 1}
    )())
    assert nightly_selfmod._regression_command(
        "python", ignore_paths=("tests/test_reflection.py",)
    ) == [
        "python", "-m", "pytest", "-q", "--maxfail=1",
        "--ignore", "tests/test_selfmod_low_integrity.py",
        "--ignore", "tests/test_reflection.py",
    ]


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
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 60)
    assert prepared["source_paths"] == ("tests/test_reflection.py",)
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
