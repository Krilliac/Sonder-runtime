"""``digest_text``: the typed generalization of ``tail -1`` + ``grep FAILED|ERROR``."""
from __future__ import annotations

import json
import re

from sonder_runtime.domain.diagnostics.digest import (
    MAX_WIRE_BYTES,
    digest_text,
    render_digest,
)


# Captured verbatim from a real run (pytest 9, Python 3.12) of a two-file
# project, with only the absolute temp directory replaced by /work/pyproj:
#   python -m pytest -q -rfE -p no:cacheprovider --continue-on-collection-errors
REAL_PYTEST_OUTPUT = """\
..FsF                                                                    [100%]
==================================== ERRORS ====================================
_______________________ ERROR collecting test_broken.py ________________________
ImportError while importing test module '/work/pyproj/test_broken.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
test_broken.py:1: in <module>
    import not_a_real_module_xyz  # noqa: F401
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   ModuleNotFoundError: No module named 'not_a_real_module_xyz'
=================================== FAILURES ===================================
___________________________________ test_bad ___________________________________

    def test_bad():
>       assert 1 == 2
E       assert 1 == 2

test_mod.py:13: AssertionError
________________________________ test_other_bad ________________________________

    def test_other_bad():
>       raise ValueError("boom")
E       ValueError: boom

test_mod.py:22: ValueError
=========================== short test summary info ============================
FAILED test_mod.py::test_bad - assert 1 == 2
FAILED test_mod.py::test_other_bad - ValueError: boom
ERROR test_broken.py
2 failed, 2 passed, 1 skipped, 1 error in 0.03s
"""


def _shell_tail_1(text):
    return [line for line in text.splitlines() if line.strip()][-1]


def _shell_grep(text):
    return [line[:300] for line in text.splitlines()
            if re.match(r"^(FAILED|ERROR) ", line)]


def test_digest_of_a_real_pytest_run_matches_the_shell_idiom():
    digest = digest_text(REAL_PYTEST_OUTPUT, source_kind="job", source_label="job:x")
    assert digest.final_line == _shell_tail_1(REAL_PYTEST_OUTPUT)
    assert list(digest.failure_lines) == _shell_grep(REAL_PYTEST_OUTPUT)
    summary = digest.summary
    assert (summary.tool, summary.status, summary.passed, summary.failed,
            summary.skipped, summary.errors) == ("pytest", "failed", 2, 2, 1, 1)
    assert [d.message for d in digest.first_errors][:2] == [
        "assert 1 == 2", "ValueError: boom",
    ]
    assert digest.tail[-1] == "2 failed, 2 passed, 1 skipped, 1 error in 0.03s"
    assert len(digest.tail) == 20
    assert dict(digest.counts)["failure_lines"] == 3


def test_render_begins_with_summary_and_stays_bounded():
    digest = digest_text(REAL_PYTEST_OUTPUT)
    text = render_digest(digest, max_chars=4000)
    assert text.startswith("summary: 2 failed, 2 passed, 1 skipped, 1 error in 0.03s")
    assert "FAILED test_mod.py::test_bad - assert 1 == 2" in text
    assert len(render_digest(digest, max_chars=300)) <= 300
    assert len(render_digest(digest, max_chars=10**9)) <= 16_000


def test_without_a_summary_the_final_line_leads():
    digest = digest_text("building\nlinking\nall done\n\n")
    assert digest.summary is None
    assert render_digest(digest).startswith("final: all done")


def test_caps_on_tail_failure_lines_and_line_length():
    text = "\n".join("FAILED t.py::test_%d - %s" % (i, "x" * 500) for i in range(400))
    digest = digest_text(text, tail_lines=999, max_failure_lines=999)
    assert len(digest.failure_lines) == 200
    assert len(digest.tail) == 200
    assert all(len(line) <= 300 for line in digest.failure_lines + digest.tail)
    assert digest.truncated is True
    assert dict(digest.counts)["failure_lines"] == 400


def test_wire_fits_the_model_payload_on_a_huge_input():
    lines = []
    for i in range(50_000):
        lines.append("src/f%d.c:%d:1: error: problem %s number %d" % (i % 97, i, "q" * 80, i))
    text = "\n".join(lines) + "\n" + "FAILED " * 10
    digest = digest_text(text, tail_lines=200, max_failure_lines=200,
                         max_first_errors=50, max_groups=50)
    wire = digest.to_wire()
    encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= MAX_WIRE_BYTES
    assert wire["truncated"] is True
    assert wire["object"] == "output_digest"
    assert wire["counts"]["error"] >= 200


def test_oversized_input_keeps_head_and_tail_windows():
    body = ["line %d" % i for i in range(60_000)]
    body[-1] = "1 passed in 9.99s"
    digest = digest_text("\n".join(body))
    assert digest.scan_truncated is True
    assert digest.lines_scanned == 50_000
    assert digest.summary is not None and digest.summary.passed == 1


def test_ansi_and_controls_are_removed_from_lines():
    digest = digest_text("\x1b[31mFAILED\x1b[0m t.py::a - boom\x07\n")
    assert digest.failure_lines == ("FAILED t.py::a - boom",)
