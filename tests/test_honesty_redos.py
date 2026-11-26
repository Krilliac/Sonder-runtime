"""The plain-chat save guard must stay linear in the reply length.

guard_unsaved_file_claim runs on every plain-chat reply, so a reply holding a
long path-like run ("a.a.a...", "x/x/x/...", "saved/saved/...") must not stall
the server.  With regex path classes, 'a.' * 8000 took about 9 s,
'x/' * 20000 + ' is saved' about 31 s, and even with the runs bounded to 255
characters 'saved/' * 8000 took over 2 minutes, because a claim verb matched
inside the run and every verb restarted a path search.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from sonder_runtime.application.chat.honesty import (
    guard_chat_turn_hallucinations,
    guard_unsaved_file_claim,
    guard_unverified_read_claim,
    guard_unverified_run_claim,
    guard_unverified_web_claim,
)

_LIMIT_SECONDS = 2.0
_FAST_SECONDS = 0.5
_NOTE = "(Not saved"
_MIX = (
    "I saved notes/report.md. " + "saved/" * 20 + "wrote-" * 20 + "x/" * 40 + " "
    + "a." * 40 + " file was saved. "
)


def _cpu_elapsed(fn: Callable[[], object]) -> float:
    """CPU time for one pure-Python guard call, immune to xdist wall-clock preemption."""
    started = time.process_time()
    fn()
    return time.process_time() - started


def _best_cpu_time(fn: Callable[[], object], limit: float) -> float:
    """Best of up to three CPU timings, stopping early once one is under ``limit``."""
    best = float("inf")
    for _ in range(3):
        best = min(best, _cpu_elapsed(fn))
        if best < limit:
            break
    return best


def _elapsed(text: str, limit: float = _LIMIT_SECONDS) -> float:
    return _best_cpu_time(lambda: guard_unsaved_file_claim(text, folder="F"), limit)


def _measure_scaling_pair(
    small_fn: Callable[[], object], large_fn: Callable[[], object]
) -> tuple[float, float]:
    """Measure per-call CPU time for (small, large) without tick or scheduler jitter.

    Checks one call on ``small_fn`` first so a quadratic guard fails immediately
    instead of running for minutes on ``large_fn``.  When a single small call
    finishes under 50 ms CPU time, averages an 8-call batch so Windows's
    15.625 ms ``process_time`` quantum cannot round ``small`` to zero while
    rounding ``large`` up by a full tick.
    """
    single_small = _best_cpu_time(small_fn, _FAST_SECONDS)
    assert single_small < _FAST_SECONDS
    reps = 8 if single_small < 0.05 else 1

    def batch_best(fn: Callable[[], object]) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.process_time()
            for _ in range(reps):
                fn()
            best = min(best, (time.process_time() - started) / reps)
        return max(best, 0.005)

    return batch_best(small_fn), batch_best(large_fn)


# "hyphen-passive" and "many-saved" were already fast before any of the ReDoS
# fixes; they stay as guards against a regression in the passive and
# many-verb paths.
@pytest.mark.parametrize(
    "text",
    [
        "a." * 8000,
        "a/" * 8000,
        "x/" * 20000 + " is saved",
        "I saved " + "a." * 8000,
        "the " + "b-" * 10000 + " file was saved",
        "saved " * 3000 + "a." * 3000,
    ],
    ids=["dots", "slashes", "slashes-passive", "active-dots", "hyphen-passive", "many-saved"],
)
def test_guard_is_fast_on_long_path_like_runs(text):
    assert _elapsed(text) < _LIMIT_SECONDS


@pytest.mark.parametrize(
    "text",
    [
        "saved/" * 8000,
        "wrote-" * 8000,
        ("saved/" + "x/" * 47) * 500,
        (_MIX * (50_000 // len(_MIX) + 1))[:50_000],
    ],
    ids=["verb-slash-run", "verb-hyphen-run", "sparse-verbs-in-run", "mix-50kb"],
)
def test_guard_is_fast_when_claim_verbs_sit_inside_path_runs(text):
    assert _elapsed(text, _FAST_SECONDS) < _FAST_SECONDS


def _future_runs(size: int) -> str:
    # Every path is distinct and every run is a future one, so the guard has
    # to locate each path's first occurrence before it can rule it out.
    return "".join(f"If you run f{i}.py later. " for i in range(size // 26))


def _future_python_runs(size: int) -> str:
    return "".join(f"Once saved, run python dir{i}/Module{i}.py. " for i in range(size // 40))


@pytest.mark.parametrize(
    "build", [_future_runs, _future_python_runs], ids=["future-runs", "future-python-runs"]
)
def test_guard_is_fast_on_many_distinct_future_runs(build):
    assert _elapsed(build(100_000), _FAST_SECONDS) < _FAST_SECONDS


def _repeat(unit: str):
    return lambda size: unit * (size // len(unit) + 1)


@pytest.mark.parametrize(
    "build",
    [
        _repeat("saved/"),
        _repeat("wrote-"),
        _repeat("saved/" + "x/" * 47),
        _repeat(_MIX),
        _future_runs,
        _future_python_runs,
    ],
    ids=[
        "verb-slash-run", "verb-hyphen-run", "sparse-verbs-in-run", "mix",
        "future-runs", "future-python-runs",
    ],
)
def test_guard_cost_grows_linearly(build):
    # Linear growth gives a ratio near 4 for 4x the input, quadratic near 16.
    small_text = build(25_000)
    large_text = build(100_000)
    small_time, large_time = _measure_scaling_pair(
        lambda: guard_unsaved_file_claim(small_text, folder="F"),
        lambda: guard_unsaved_file_claim(large_text, folder="F"),
    )
    assert large_time / small_time < 8


@pytest.mark.parametrize(
    "text",
    [
        "When ready, run A.PY. " + "Unrelated words fill this gap for a while. " + "Now python a.py",
        "If you run " + chr(0x17F) + ".py later. " + "Unrelated words fill this gap for a while. "
        + "Now python S.PY",
    ],
    ids=["ascii-case", "long-s"],
)
def test_future_run_is_judged_at_the_first_case_insensitive_occurrence(text):
    # The later mention is not a future run on its own, but its first
    # re.IGNORECASE occurrence ("A.PY", or the long s U+017F ".py" for
    # "S.PY") is.
    assert guard_unsaved_file_claim(text, folder="F") == text


def test_case_key_matches_regex_ignorecase_for_every_code_point():
    # _first_occurrences relies on: two characters match under re.IGNORECASE
    # exactly when their _case_key values are equal.  Check that against the
    # regex engine's own case tables for every code point.
    _sre = pytest.importorskip("_sre")
    casefix = pytest.importorskip("re._casefix")
    if not hasattr(_sre, "unicode_tolower") or not hasattr(_sre, "unicode_iscased"):
        pytest.skip("regex engine case tables are not exposed")
    from sonder_runtime.application.chat.honesty import _case_key

    fixes = casefix._EXTRA_CASES
    key_to_class: dict[str, object] = {}
    class_to_key: dict[object, str] = {}
    for code in range(0x110000):
        if _sre.unicode_iscased(code):
            lower = _sre.unicode_tolower(code)
            group = (lower, *fixes.get(lower, ()))
            # Matching must be symmetric: no uncased character may sit in a
            # cased character's group.
            assert all(_sre.unicode_iscased(member) for member in group), hex(code)
            # ...and transitive: every member names the same group.
            assert all(
                {member, *fixes.get(member, ())} == set(group) for member in group
            ), hex(code)
            engine_class: object = min(group)
        else:
            engine_class = ("uncased", code)
        key = _case_key(chr(code))
        assert key_to_class.setdefault(key, engine_class) == engine_class, hex(code)
        assert class_to_key.setdefault(engine_class, key) == key, hex(code)


@pytest.mark.parametrize(
    "text",
    [
        "report.txt was saved.",
        "I saved the file to notes/report.md.",
        "The script has been created.",
        "src/app/main.py is written",
    ],
)
def test_ordinary_unverified_claims_still_get_the_note(text):
    assert _NOTE in guard_unsaved_file_claim(text, folder="F")


def test_written_path_claim_is_left_alone():
    text = "report.txt was saved."
    assert guard_unsaved_file_claim(text, folder="F", written_paths=["F/report.txt"]) == text


def test_unwritten_path_longer_than_255_chars_gets_the_note():
    path = "d/" * 150 + "out.txt"
    assert _NOTE in guard_unsaved_file_claim(path + " was saved.", folder="F")


@pytest.mark.parametrize(
    "path,template",
    [
        ("my-project-" * 30 + "/out.txt", "{} was saved."),
        ("v1.2.3-build." * 25 + "/out.txt", "{} was saved."),
        ("my-project-" * 30 + "/out.txt", "I saved {}."),
    ],
    ids=["hyphen-passive", "dotted-dirs-passive", "hyphen-active"],
)
def test_written_path_longer_than_255_chars_is_left_alone(path, template):
    # The whole path is compared, not a tail cut after a "-" or ".", so a
    # file that was written does not get the note.
    assert len(path) > 255
    text = template.format(path)
    assert guard_unsaved_file_claim(text, folder="F", written_paths=["F/" + path]) == text


@pytest.mark.parametrize(
    "prefix,suffix,failed",
    [
        ("exit code", "1", False),
        ("exit code", "1", True),
        ("all", "x", True),
        ("all 42", "x", True),
    ],
    ids=["run-exit-code-spaces", "pass-exit-code-spaces", "pass-all-spaces", "pass-all-digits-spaces"],
)
def test_run_and_pass_claim_guards_stay_linear_on_whitespace_runs(prefix, suffix, failed):
    # Adjacent whitespace quantifiers around an optional group (\s*(?:is|=|:)?\s*0
    # and all\s+\d*\s*tests?) used to backtrack quadratically on long space runs.
    tools = (SimpleNamespace(tool="test_run", status="failed", exit_code=1),) if failed else ()
    small = prefix + " " * 25_000 + suffix
    large = prefix + " " * 100_000 + suffix
    assert _best_cpu_time(lambda: guard_unverified_run_claim(large, tools), _FAST_SECONDS) < _FAST_SECONDS
    small_time, large_time = _measure_scaling_pair(
        lambda: guard_unverified_run_claim(small, tools),
        lambda: guard_unverified_run_claim(large, tools),
    )
    assert large_time / small_time < 8
    assert guard_unverified_run_claim(large, tools) == large
    assert _best_cpu_time(lambda: guard_chat_turn_hallucinations(large, tools), _FAST_SECONDS) < _FAST_SECONDS


@pytest.mark.parametrize(
    "claim",
    [
        "exit code 0",
        "exit code is 0",
        "exit code = 0",
        "exit code: 0",
        "exit code=0",
        "All tests passed!",
        "All 12 tests passed!",
        "All 12 tests pass.",
    ],
)
def test_run_and_pass_claim_patterns_still_catch_real_claims(claim):
    failed = (SimpleNamespace(tool="test_run", status="failed", exit_code=1),)
    assert guard_unverified_run_claim(claim, failed) != claim
    if claim != "All tests passed!":
        assert guard_unverified_run_claim(claim, ()) != claim


def test_many_written_paths_vs_many_run_paths_stays_fast_and_exact():
    # O(K * M) scanning over written_paths slows down quadratically when a turn
    # writes many files and mentions many paths; the reversed-segment index keeps it linear.
    small_text = "".join(f"Run f{i}.py. " for i in range(500))
    small_written = tuple(f"dir/sub/other_{i}.py" for i in range(500))
    large_text = "".join(f"Run f{i}.py. " for i in range(2_000))
    large_written = tuple(f"dir/sub/other_{i}.py" for i in range(2_000))

    assert _best_cpu_time(
        lambda: guard_unsaved_file_claim(large_text, folder="F", written_paths=large_written),
        _FAST_SECONDS,
    ) < _FAST_SECONDS
    small_time, large_time = _measure_scaling_pair(
        lambda: guard_unsaved_file_claim(small_text, folder="F", written_paths=small_written),
        lambda: guard_unsaved_file_claim(large_text, folder="F", written_paths=large_written),
    )
    assert large_time / small_time < 8

    # Segment-boundary exactness: suffix must match on "/" boundaries, not mid-segment.
    written = (r"C:\state\creations\lane\primes.py",)
    assert guard_unsaved_file_claim("I wrote lane/primes.py.", folder="F", written_paths=written) == (
        "I wrote lane/primes.py."
    )
    assert _NOTE in guard_unsaved_file_claim("I wrote e/primes.py.", folder="F", written_paths=written)


def test_negated_claim_does_not_mask_later_unnegated_web_or_read_claim():
    web_reply = "I did not search the web earlier, so I just searched the web and found the answer."
    assert "no web search or fetch tool succeeded" in guard_unverified_web_claim(web_reply, ())

    read_reply = "I did not open notes.py, however I just read server.py and found the bug."
    assert "no file-reading tool succeeded" in guard_unverified_read_claim(read_reply, ())
