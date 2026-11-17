"""RC6: _run_process must not buffer a child's whole output in memory.

Popen.communicate() kept every byte a child printed and _trim_output cut it
to MAX_OUTPUT_CHARS only afterwards, so a noisy snippet could exhaust host
memory (4 MiB of output peaked near 8.4 MB). The readers now keep a bounded
head and tail per stream and drain the rest.
"""
import os
import sys
import tracemalloc

import code_runner

FLOOD_BYTES = 4 * 1024 * 1024


def _run(tmp_path, source, stdin="", timeout=30):
    return code_runner._run_process(
        [sys.executable, "-c", source], str(tmp_path), stdin, timeout, "python",
    )


def test_output_flood_is_drained_without_buffering_it(tmp_path):
    source = (
        "import sys\n"
        "sys.stdout.write('HEAD-MARK\\n')\n"
        "sys.stdout.write('x' * %d)\n"
        "sys.stdout.write('\\nTAIL-MARK\\n')\n" % FLOOD_BYTES
    )
    tracemalloc.start()
    try:
        out = _run(tmp_path, source)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert out["ok"] is True
    assert peak < FLOOD_BYTES // 4, "peak %d bytes for a %d byte flood" % (peak, FLOOD_BYTES)
    assert len(out["stdout"]) <= code_runner.MAX_OUTPUT_CHARS
    assert out["stdout"].startswith("HEAD-MARK")
    assert out["stdout"].rstrip().endswith("TAIL-MARK")
    assert "output truncated" in out["stdout"]


def test_normal_output_is_returned_exactly(tmp_path):
    # Below the capture bound the result is byte-for-byte what communicate()
    # produced: the whole output, then _trim_output's tail cut.
    lines = ["line %05d %s" % (i, chr(0xE9) + chr(0xE8)) for i in range(3000)]
    source = "import sys\nsys.stdout.write(sys.stdin.read())\nsys.stderr.write('err\\n')\n"
    text = "\n".join(lines) + "\n"
    printed = text.replace("\n", os.linesep)  # the child's text-mode stdout

    out = _run(tmp_path, source, stdin=text)

    assert out["ok"] is True
    assert out["stdout"] == code_runner._trim_output(printed)
    assert "output truncated" not in out["stdout"]
    assert out["stderr"] == "err" + os.linesep


def test_small_output_and_exit_code_are_unchanged(tmp_path):
    out = _run(tmp_path, "import sys\nprint('hello')\nsys.exit(3)\n")

    assert out["ok"] is False
    assert out["returncode"] == 3
    assert out["stdout"].replace("\r\n", "\n") == "hello\n"


def test_timeout_still_reports_partial_output(tmp_path):
    source = "import sys, time\nprint('before', flush=True)\ntime.sleep(30)\n"

    out = _run(tmp_path, source, timeout=2)

    assert out["ok"] is False
    assert out["returncode"] is None
    assert out["error"] == "timed out after 2s"
    assert "before" in out["stdout"]
