"""<=45-second Linux fake-tree smoke; never imports Sonder or uses a network."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
CANDIDATE = ROOT
EXPECTED = {
    "tls_ci_diagnostic_plugin.py": "8bbc3eb0133f626e56a601fde35d1b3082dcde48caba9ba1fc5d5c9959753bd6",
    "tls_ci_diagnostic_runner.py": "6761721d08262c2614bc3a0d0f5e297a320bbebb978cdeb22f189cc25189d3f5",
}
for name, expected in EXPECTED.items():
    assert hashlib.sha256((CANDIDATE / name).read_bytes()).hexdigest() == expected
spec = importlib.util.spec_from_file_location("reviewed_supervisor", CANDIDATE / "tls_ci_diagnostic_runner.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)
supervisor.INTERRUPT_SECONDS = supervisor.TERM_SECONDS = supervisor.KILL_SECONDS = 1
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
EVIDENCE = Path(os.environ["TLS_CI_SMOKE_DIR"])
EVIDENCE.mkdir(parents=True, exist_ok=False)
deadline = time.monotonic() + 45
results = []


def run_owned(command, cwd):
    owned = {}
    with (cwd / "outer.txt").open("w", encoding="utf-8") as output:
        child = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        timeout = False
        try:
            while child.poll() is None:
                supervisor.capture_descendants(owned)
                if time.monotonic() >= deadline:
                    timeout = True
                    break
                time.sleep(0.05)
            code = child.returncode
        finally:
            try:
                leftovers = supervisor.cleanup(owned)
                child.wait(timeout=1)
            finally:
                supervisor.close_owned(owned)
    assert not timeout and not leftovers, (timeout, leftovers)
    return code


def copy_harness(destination, *, cap=5, launch_failure=False, watchdog=10):
    destination.mkdir()
    plugin = (CANDIDATE / "tls_ci_diagnostic_plugin.py").read_text()
    assert plugin.count("_CAP = 5000") == 1
    (destination / "tls_ci_diagnostic_plugin.py").write_text(plugin.replace("_CAP = 5000", f"_CAP = {cap}"))
    runner = (CANDIDATE / "tls_ci_diagnostic_runner.py").read_text()
    replacements = {
        "WATCHDOG_SECONDS = 18 * 60": f"WATCHDOG_SECONDS = {watchdog}",
        "INTERRUPT_SECONDS = 5": "INTERRUPT_SECONDS = 1",
        "TERM_SECONDS = 5": "TERM_SECONDS = 1",
        "KILL_SECONDS = 5": "KILL_SECONDS = 1",
        '"-v", "-n", "auto"': '"-v", "-n", "2"',
    }
    if launch_failure:
        replacements['sys.executable, "-u", "-m", "pytest"'] = '"/definitely-absent-tls-ci-python", "-u", "-m", "pytest"'
    for old, new in replacements.items():
        assert runner.count(old) == 1, old
        runner = runner.replace(old, new)
    (destination / "runner.py").write_text(runner)


with tempfile.TemporaryDirectory(prefix="tls-ci-fake-smoke-") as temporary:
    temp = Path(temporary)
    source = temp / "source"
    source.mkdir()
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_ollama_endpoint_tls.py").write_text("# Fake tracked blob only; no product source.\n")
    (source / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Codex smoke", "-c", "user.email=noreply@openai.com",
                    "commit", "-qm", "Disposable fake diagnostic smoke"], check=True)
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    cases = [
        ("cap", "import pytest\n@pytest.mark.parametrize('value', range(8))\ndef test_fake_pass(value):\n    assert value >= 0\n", 5, False, sha, 4),
        ("failures", "import pytest\n@pytest.mark.parametrize('value', range(8))\ndef test_fake_fail(value):\n    assert value < 0\n", 50, False, sha, 1),
        ("preflight", "def test_fake_unused():\n    assert True\n", 5, False, "0" * 40, 125),
        ("launch", "def test_fake_unused():\n    assert True\n", 5, True, sha, 125),
        ("tree", "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
         "def test_fake_escape():\n"
         "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
         "    Path(os.environ['TLS_CI_DIAGNOSTIC_DIR'], 'escaped.json').write_text(json.dumps({'pid': child.pid}))\n"
         "    time.sleep(30)\n", 50, False, sha, 124),
    ]
    for name, fake_test, cap, failed_launch, expected_sha, expected_exit in cases:
        case = temp / name
        copy_harness(case, cap=cap, launch_failure=failed_launch, watchdog=10)
        (tests / "test_fake.py").write_text(fake_test)
        evidence = case / "evidence"
        command = [sys.executable, str(case / "runner.py"), "--source", str(source),
                   "--expected-sha", expected_sha, "--evidence", str(evidence), "--label", "current-candidate"]
        try:
            code = run_owned(command, case)
        finally:
            shutil.copytree(case, EVIDENCE / "smoke-case-evidence" / name, dirs_exist_ok=True)
        receipt = json.loads((evidence / "supervisor-result.json").read_text())
        assert code == expected_exit, (name, code, receipt)
        assert receipt["leftover_owned_pids"] == []
        assert not receipt["cleanup_error"] and not receipt["output_reader_alive"]
        event_path = evidence / "events-controller.jsonl"
        events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.exists() else []
        failures = [item for item in events if item["kind"] == "failure"]
        caps = [item for item in events if item["kind"] == "cap"]
        if name == "cap":
            assert caps and caps[0]["completed"] == 5 and not failures
            assert (evidence / "pytest-report.xml").is_file()
        if name == "failures":
            assert len(failures) >= 5 and all(item["nodeid"].startswith("tests/test_fake.py::test_fake_fail") for item in failures)
            assert '"kind": "failure"' in (case / "outer.txt").read_text()
        if name in {"preflight", "launch"}:
            assert receipt["infrastructure_error"] in {"RuntimeError", "FileNotFoundError"}
        if name == "tree":
            escaped = json.loads((evidence / "escaped.json").read_text())
            assert not (Path("/proc") / str(escaped["pid"])).exists(), escaped
        results.append({"case": name, "exit": code, "named_failures": len(failures),
                        "cap": caps[0]["completed"] if caps else None,
                        "owned_processes_discovered": receipt["owned_processes_discovered"],
                        "leftover_owned_pids": receipt["leftover_owned_pids"]})
        print(json.dumps(results[-1]), flush=True)

for name, expected in EXPECTED.items():
    assert hashlib.sha256((CANDIDATE / name).read_bytes()).hexdigest() == expected
result = {"qualified": True, "smoke_only": True, "python": sys.version,
          "workers_in_copies": 2, "reduced_thresholds_in_copies": True,
          "original_sources": EXPECTED, "cases": results}
(EVIDENCE / "diagnostic-smoke-linux.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
