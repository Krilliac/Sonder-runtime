"""format_run_result(digest=...): opt-in digest block, byte-identical otherwise."""
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


def test_digest_block_is_last_indented_and_bounded():
    base = format_run_result("test run (pytest)", DATA)
    rendered = format_run_result("test run (pytest)", DATA, digest=True)
    assert rendered.startswith(base + "\ndigest:\n")
    block = rendered[len(base) + 1:]
    assert block.splitlines()[1] == "  summary: 1 failed, 2 passed in 0.10s"
    assert "  failure lines:" in block and "    FAILED t.py::test_x - assert 0" in block
    assert all(line.startswith("  ") for line in block.splitlines()[1:])
    stdout = "".join("FAILED t.py::t%d - boom\n" % i for i in range(5000))
    huge_block = format_run_result("t", dict(DATA, stdout=stdout), digest=True).split(
        "\ndigest:\n", 1,
    )[1]
    lines = huge_block.splitlines()
    # 2000 rendered characters, each line re-indented by two spaces.
    assert len(huge_block) <= 2000 + 2 * len(lines) + 2


def test_no_output_means_no_digest_block():
    quiet = dict(DATA, stdout="", stderr="")
    assert format_run_result("build", quiet, digest=True) == format_run_result("build", quiet)
