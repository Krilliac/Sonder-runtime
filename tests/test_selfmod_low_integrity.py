import os
from contextlib import nullcontext
from pathlib import Path
import sys

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
        "  for _ in range(32):\n"
        "    children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']))\n"
        "except OSError:\n"
        "  raise SystemExit(0)\n"
        "raise SystemExit(3)",
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10)
    assert result["passed"] is True


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
