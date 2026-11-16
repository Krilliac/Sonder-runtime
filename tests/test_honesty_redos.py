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

import pytest

from sonder_runtime.application.chat.honesty import guard_unsaved_file_claim

_LIMIT_SECONDS = 2.0
_FAST_SECONDS = 0.5
_NOTE = "(Not saved"
_MIX = (
    "I saved notes/report.md. " + "saved/" * 20 + "wrote-" * 20 + "x/" * 40 + " "
    + "a." * 40 + " file was saved. "
)


def _elapsed(text: str) -> float:
    started = time.perf_counter()
    guard_unsaved_file_claim(text, folder="F")
    return time.perf_counter() - started


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
    assert _elapsed(text) < _FAST_SECONDS


@pytest.mark.parametrize(
    "unit",
    ["saved/", "wrote-", "saved/" + "x/" * 47, _MIX],
    ids=["verb-slash-run", "verb-hyphen-run", "sparse-verbs-in-run", "mix"],
)
def test_guard_cost_grows_linearly(unit):
    # Linear growth gives a ratio near 4 for 4x the input, quadratic near 16.
    # Timings are floored at 5 ms so scheduler noise on tiny runs cannot fail
    # the ratio, and the small size must itself be fast so a quadratic guard
    # fails here instead of running for minutes on the large size.
    def best_of_three(text: str) -> float:
        return max(min(_elapsed(text) for _ in range(3)), 0.005)

    small = unit * (25_000 // len(unit) + 1)
    small_time = best_of_three(small)
    assert small_time < _FAST_SECONDS
    large_time = best_of_three(small * 4)
    assert large_time / small_time < 8


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
