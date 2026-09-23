import os
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows MIC boundary")


def test_low_candidate_cannot_write_protected_truth(tmp_path):
    from scripts.selfmod_low_integrity import run_isolated

    truth = tmp_path / "truth.txt"
    truth.write_text("original", encoding="utf-8")
    command = [
        sys.executable, "-c",
        "from pathlib import Path; p=Path(%r); "
        "\ntry: p.write_text('tampered'); raise SystemExit(3)\n"
        "except PermissionError: pass" % str(truth),
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10, protected_paths=[truth])
    assert result["passed"] is True
    assert truth.read_text(encoding="utf-8") == "original"


def test_low_candidate_can_run_and_write_low_temp(tmp_path):
    from scripts.selfmod_low_integrity import run_isolated

    command = [
        sys.executable, "-c",
        "import os; from pathlib import Path; "
        "home=Path.home(); assert str(home).startswith(os.environ['TEMP']); "
        "home.joinpath('low-home-marker.txt').write_text('ok'); "
        "Path(os.environ['TEMP'], 'low-marker.txt').write_text('ok')",
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10)
    assert result["passed"] is True
    # The candidate cwd remains the medium checkout; its low temp is supplied
    # through TEMP/TMP. This assertion is intentionally on process success,
    # while the protected-file test above carries the security guarantee.
    assert not (tmp_path / "marker.txt").exists()


def test_low_candidate_cannot_read_medium_evaluator_manifest(tmp_path):
    from scripts.selfmod_low_integrity import run_isolated

    command = [
        sys.executable, "-c",
        "import os; from pathlib import Path; "
        "p=Path(os.environ['TEMP'], 'truth-manifest.json'); "
        "\ntry: p.read_text(); raise SystemExit(3)\n"
        "except PermissionError: pass",
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10)
    assert result["passed"] is True


def test_candidate_module_cannot_shadow_the_low_supervisor(tmp_path):
    from scripts.selfmod_low_integrity import run_isolated

    (tmp_path / "selfmod_low_integrity.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8",
    )
    result = run_isolated(
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=tmp_path, timeout=10,
    )
    assert result["exit_code"] == 7
    assert result["passed"] is False


def test_low_job_limits_descendant_process_count(tmp_path):
    from scripts.selfmod_low_integrity import run_isolated

    command = [
        sys.executable, "-c",
        "import subprocess, sys; children=[]\n"
        "try:\n"
        "  for _ in range(40):\n"
        "    children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']))\n"
        "except OSError:\n"
        "  print('launched', len(children))\n"
        "  raise SystemExit(0 if len(children) >= 10 else 4)\n"
        "raise SystemExit(3)",
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10)
    assert result["passed"] is True, result


def _child_spec(tmp_path, command, timeout=10):
    output = tmp_path / "child-output.txt"
    result = tmp_path / "child-result.json"
    spec = tmp_path / "child-spec.json"
    spec.write_text(json.dumps({
        "command": command,
        "cwd": str(tmp_path),
        "timeout": timeout,
        "env": {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\\Windows"),
            "PATH": os.environ.get("PATH", ""),
            "TEMP": str(tmp_path), "TMP": str(tmp_path),
            "USERPROFILE": str(tmp_path / "home"),
            "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(tmp_path / "home" / "AppData" / "Local"),
        },
        "output": str(output), "result": str(result),
    }), encoding="utf-8")
    return spec, output, result


def test_low_child_publishes_interim_output_and_bounds_noisy_tail(tmp_path):
    from scripts.selfmod_low_integrity import _child

    command = [sys.executable, "-c", (
        "import sys,time; print('early', flush=True); time.sleep(1); "
        "sys.stdout.write('x'*250000); sys.stdout.flush()"
    )]
    spec, output, result = _child_spec(tmp_path, command)
    worker = threading.Thread(target=_child, args=(spec,))
    worker.start()
    deadline = time.monotonic() + 5
    saw_interim = False
    while time.monotonic() < deadline and worker.is_alive():
        if output.exists() and b"early" in output.read_bytes():
            saw_interim = True
            break
        time.sleep(0.05)
    assert saw_interim
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert output.stat().st_size <= 120_000
    assert json.loads(result.read_text(encoding="utf-8"))["returncode"] == 0


def test_low_child_timeout_keeps_interim_diagnostic(tmp_path):
    from scripts.selfmod_low_integrity import _child

    command = [sys.executable, "-c", (
        "import time; print('started', flush=True); time.sleep(30)"
    )]
    spec, output, result = _child_spec(tmp_path, command, timeout=1)
    started = time.monotonic()
    assert _child(spec) == 124
    assert time.monotonic() - started < 10
    assert b"started" in output.read_bytes()
    details = json.loads(result.read_text(encoding="utf-8"))
    assert details["timed_out"] is True


def test_low_child_refuses_success_when_output_drain_fails(tmp_path, monkeypatch):
    from scripts import selfmod_low_integrity

    spec, _output, result = _child_spec(
        tmp_path, [sys.executable, "-c", "print('candidate passed')"],
    )

    def refuse_output(*_args):
        raise OSError("output unavailable")

    monkeypatch.setattr(selfmod_low_integrity, "_write_output_tail", refuse_output)
    assert selfmod_low_integrity._child(spec) == 125
    assert "output drain failed" in json.loads(
        result.read_text(encoding="utf-8")
    )["error"]


def test_record_test_routes_regression_and_held_out_through_low_runner(tmp_path, monkeypatch):
    import selfmod
    from scripts import selfmod_low_integrity

    calls = []

    def isolated(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return {"exit_code": 0, "output": "ok", "passed": True}

    class Connection:
        def execute(self, *_args):
            return None

    monkeypatch.setenv("SELFMOD_LOW_INTEGRITY", "1")
    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", isolated)
    monkeypatch.setattr(selfmod, "get_run", lambda _run_id: {
        "id": "run-1", "phase": "testing", "budgets": {"max_test_seconds": 30},
    })
    monkeypatch.setattr(selfmod, "candidate_path", lambda _run_id: tmp_path)
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)

    assert selfmod.record_test("run-1", "regression", ["python", "-V"])["passed"]
    assert selfmod.record_test(
        "run-1", "held_out", ["python", "-V"], protected_paths=[tmp_path / "truth"],
    )["passed"]
    assert len(calls) == 2
    assert calls[0][1]["protected_paths"] == ()
    assert calls[1][1]["protected_paths"] == [tmp_path / "truth"]


def test_isolation_setup_failure_is_not_a_successful_reproducer(tmp_path, monkeypatch):
    import selfmod
    from scripts import selfmod_low_integrity

    class Connection:
        def execute(self, *_args):
            return None

    monkeypatch.setenv("SELFMOD_LOW_INTEGRITY", "1")
    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)
    result = selfmod._record_command(
        {"id": "run-1"}, "reproducer_before", ["python", "-V"],
        tmp_path, 10, expect_failure=True,
    )
    assert result["exit_code"] == 125
    assert result["passed"] is False
