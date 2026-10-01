"""format_run_result(digest=...): opt-in digest before process output."""
from __future__ import annotations

from sonder_runtime.adapters.observability.run_result_formatting import format_run_result

DATA = {
    "command": ["python", "-m", "pytest", "-q"], "cwd": "/w", "ok": False,
    "returncode": 1, "timed_out": False, "elapsed_ms": 812,
    "stdout": "..F\nFAILED t.py::test_x - assert 0\n1 failed, 2 passed in 0.10s\n",
    "stderr": "", "stdout_truncated": True,
}


def test_digest_off_is_byte_identical_to_the_default():
    assert format_run_result("test run (pytest)", DATA) == format_run_result(
        "test run (pytest)", DATA, digest=False,
    )
    assert "digest:" not in format_run_result("test run (pytest)", DATA)


def test_digest_block_precedes_output_and_is_bounded():
    rendered = format_run_result("test run (pytest)", DATA, digest=True)
    block = rendered.split("digest:\n", 1)[1].split("stdout:\n", 1)[0]
    assert block.splitlines()[0] == "  summary: 1 failed, 2 passed in 0.10s"
    assert "  failure lines:" in block and "    FAILED t.py::test_x - assert 0" in block
    assert all(line.startswith("  ") for line in block.splitlines())
    assert len(block) <= 1200
    stdout = "".join("FAILED t.py::t%d - boom\n" % i for i in range(5000))
    huge = format_run_result("t", dict(DATA, stdout=stdout), digest=True)
    assert len(huge) <= 6000
    assert huge.index("digest:") < huge.index("stdout:")


def test_no_output_means_no_digest_block():
    quiet = dict(DATA, stdout="", stderr="")
    assert format_run_result("build", quiet, digest=True) == format_run_result("build", quiet)
