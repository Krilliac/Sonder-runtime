"""The plain-chat save guard must stay linear on long dotted or slashed tokens.

guard_unsaved_file_claim runs on every plain-chat reply, so a reply holding a
long path-like run ("a.a.a..." or "x/x/x/...") must not stall the server.
Before the path classes were bounded, 'a.' * 8000 took about 9 s and
'x/' * 20000 + ' is saved' about 31 s.
"""
from __future__ import annotations

import time

import pytest

from sonder_runtime.application.chat.honesty import guard_unsaved_file_claim

_LIMIT_SECONDS = 2.0
_NOTE = "(Not saved"


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
    started = time.perf_counter()
    guard_unsaved_file_claim(text, folder="F")
    assert time.perf_counter() - started < _LIMIT_SECONDS


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


def test_path_longer_than_bound_is_still_seen_by_its_tail():
    # A 300-char path exceeds the 255 bound; the claim is still found from a
    # later offset, so the note is not lost for long real paths.
    path = "d/" * 150 + "out.txt"
    assert _NOTE in guard_unsaved_file_claim(path + " was saved.", folder="F")
