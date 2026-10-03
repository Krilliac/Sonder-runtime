"""Run the real per-test configuration boundary in a fresh serial child.

The ordered companion lives outside pytest's default test-file patterns;
this independently scheduled test selects it explicitly and checks its
exact JUnit identities and terminal result.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


_EXPECTED_CASES = (
    "test_01_typed_composition_owns_ca_and_pool",
    "test_02_next_test_verifies_local_tls_with_its_environment_bundle",
    "test_03_next_test_builds_a_pool_from_its_environment",
)
_EXPECTED_CLASS = "tests.fixtures.ca_pool_order_contract"
_CHILD_TIMEOUT_SECONDS = 45
_KILL_TIMEOUT_SECONDS = 5
_MAX_LOG_BYTES = 1_048_576
_MAX_XML_BYTES = 1_048_576
_TAIL_BYTES = 65_536


def _bounded_log_tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - _TAIL_BYTES))
        return stream.read(_TAIL_BYTES).decode("utf-8", errors="replace")


def test_typed_ollama_configuration_fixture_isolates_real_composition(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests" / "fixtures" / "ca_pool_order_contract.py"
    assert fixture.is_file(), "The explicit ordered fixture must be installed"
    junit = tmp_path / "ollama-configuration-child.xml"
    transcript = tmp_path / "ollama-configuration-child.log"
    environment = dict(os.environ)
    # Parent test scheduling and plugin options must not turn the child into
    # an xdist run, recursively select this driver, or share parent plugins.
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(root)
    command = [
        sys.executable,
        "-m", "pytest",
        "-q", "-ra", "--color=no",
        "-p", "no:cacheprovider",
        "--import-mode=importlib",
        "--rootdir", str(root),
        "-o", "addopts=",
        "-o", "junit_family=xunit2",
        "-o", "faulthandler_timeout=20",
        "--junitxml", str(junit),
        "tests/fixtures/ca_pool_order_contract.py",
    ]
    # Ordinary creation flags permit inheritance of an existing Windows Job.
    # The selected cases create one listener thread and no application child
    # process; this driver's own child deadline applies in every environment.
    with transcript.open("wb") as output:
        child = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            exit_code = child.wait(timeout=_CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=_KILL_TIMEOUT_SECONDS)
            pytest.fail("Serial Ollama configuration child exceeded 45 seconds")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=_KILL_TIMEOUT_SECONDS)

    assert transcript.stat().st_size <= _MAX_LOG_BYTES, "Child transcript exceeded its evidence bound"
    child_output = _bounded_log_tail(transcript)
    assert exit_code == 0, child_output
    assert junit.is_file(), "Successful child must retain JUnit evidence"
    assert 0 < junit.stat().st_size <= _MAX_XML_BYTES, "Child JUnit exceeded its evidence bound"
    report = ET.parse(junit).getroot()
    assert report.tag == "testsuites"
    suites = list(report)
    assert len(suites) == 1 and suites[0].tag == "testsuite"
    suite = suites[0]
    assert {
        name: int(suite.attrib[name])
        for name in ("tests", "failures", "errors", "skipped")
    } == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}, child_output
    cases = list(suite.iter("testcase"))
    assert tuple(case.attrib.get("name") for case in cases) == _EXPECTED_CASES, child_output
    assert all(case.attrib.get("classname") == _EXPECTED_CLASS for case in cases)
    assert all(
        case.find(tag) is None
        for case in cases
        for tag in ("failure", "error", "skipped")
    ), child_output
    summaries = re.findall(
        r"(?m)^\s*(?:=+\s*)?3 passed(?:, \d+ warnings?)? in "
        r"\d+(?:\.\d+)?s(?: \([^)]+\))?(?:\s*=+)?\s*$",
        child_output,
    )
    assert len(summaries) == 1, "Expected exactly one real 3-passed terminal summary\n" + child_output
