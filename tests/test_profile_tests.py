"""Contracts for the bounded pytest profiling harness."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_profile_report_is_atomic_bounded_and_omits_test_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_sample.py").write_text(
        "import time\n\n"
        "def test_fast():\n    print('PRIVATE-SENTINEL')\n    assert True\n\n"
        "def test_slow():\n    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    output = project / ".pytest_cache" / "profile.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "profile_tests.py"),
            "--repo", str(project),
            "--top", "1",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema"] == "sonder.pytest-profile.v1"
    assert report["parallelism"] == "serial"
    assert report["workers"] == 0
    assert report["reported_tests"] == 2
    assert report["outcomes"] == {"failed": 0, "passed": 2, "skipped": 0}
    assert len(report["slowest"]) == 1
    assert report["slowest"][0]["nodeid"].endswith("test_slow")
    assert "PRIVATE-SENTINEL" not in output.read_text(encoding="utf-8")
    assert not list(output.parent.glob("*.tmp"))


def test_parallelism_and_report_bounds_fail_closed(tmp_path):
    script = _REPO_ROOT / "scripts" / "profile_tests.py"
    for option in (("--workers", "5"), ("--top", "0")):
        result = subprocess.run(
            [sys.executable, str(script), *option],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "must be between" in result.stderr


def test_two_worker_mode_uses_file_grouped_xdist(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (project / "tests").mkdir()
    for index in range(2):
        (project / "tests" / ("test_%d.py" % index)).write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
    output = project / "parallel.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "profile_tests.py"),
            "--repo", str(project),
            "--workers", "2",
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["parallelism"] == "xdist-loadfile"
    assert report["workers"] == 2
    assert report["reported_tests"] == 2
