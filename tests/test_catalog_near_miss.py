"""A typed-and-submitted command miss must point somewhere, not dead-end.

``complete`` is deliberately a strict narrowing -- name prefix, then alias,
then substring, then summary text -- so the as-you-type slash menu converges
on what is being typed instead of reshuffling under the cursor. That is the
right rule while a line is still being typed and the wrong one the moment the
line is submitted: ``/mdoel`` narrows to nothing, and the console answered
"no command matches" with no way forward, for a one-character transposition of
a command the user had just been shown by ``/help``.

The same dead end swallowed category names. ``/help`` tells the reader to type
``/help <category>``; ``/help securty`` came back "no command 'securty'" --
the wrong noun, and no mention that ``security`` is one of the seventeen
categories it had just listed.

These tests pin the recovery to the *miss* surfaces only. ``complete`` itself
must stay strict, so the live menu is unchanged.
"""
from __future__ import annotations

import pytest

from command_catalog import (
    by_name,
    categories,
    complete,
    format_matches,
    help_text,
    near_misses,
)


pytestmark = pytest.mark.unit


# --- the dead ends --------------------------------------------------------


def test_submitted_command_typo_suggests_the_real_command():
    text = format_matches("/mdoel")

    assert "/model" in text, text
    assert "did you mean" in text.lower(), text


def test_help_for_a_command_typo_suggests_the_real_command():
    text = help_text("/dianostics")

    assert "/diagnostics" in text, text
    assert "did you mean" in text.lower(), text


def test_help_for_a_category_typo_names_the_category():
    text = help_text("securty")
    lowered = text.lower()

    assert "security" in lowered, text
    assert "category" in lowered, text


def test_a_typoed_alias_suggests_the_command_it_belongs_to():
    # "/cmds" is an alias of "/commands"; a typo of either must land on the
    # canonical name rather than on a second spelling of the same command.
    suggestions = near_misses("comands")

    assert "/commands" in suggestions, suggestions
    assert "/cmds" not in suggestions, suggestions


# --- the recovery must not become noise -----------------------------------


def test_nonsense_still_reports_no_match_and_offers_nothing():
    text = format_matches("/zzzzzzzzzz")

    assert "no command matches" in text.lower(), text
    assert "did you mean" not in text.lower(), text
    assert near_misses("zzzzzzzzzz") == []


def test_a_prefix_that_matches_is_never_replaced_by_a_near_miss():
    # "/stat" is a real prefix of several commands. The strict tiers answer it,
    # so the fuzzy tier must not run and must not reorder the result.
    text = format_matches("/stat")

    assert "/stats" in text
    assert "did you mean" not in text.lower(), text
    assert "commands matching" in text.lower(), text


def test_a_single_character_is_too_short_to_guess_from():
    assert near_misses("") == []
    assert near_misses("x") == []


def test_every_suggestion_is_something_the_user_can_actually_type():
    known = set(categories())
    for probe in ("mdoel", "securty", "memry", "systm", "comands", "helo"):
        for suggestion in near_misses(probe):
            if suggestion.startswith("/"):
                assert by_name(suggestion) is not None, (probe, suggestion)
            else:
                assert suggestion in known, (probe, suggestion)


def test_suggestions_are_bounded_and_deduplicated():
    got = near_misses("stauts", limit=3)

    assert len(got) <= 3
    assert len(set(got)) == len(got), got


# --- the strict path stays strict -----------------------------------------


def test_as_you_type_completion_is_unchanged_by_the_recovery():
    # The live slash menu calls complete() on every keystroke. A fuzzy tier
    # there would make the list grow as the query gets more specific.
    assert complete("/mdoel") == []
    assert complete("/zzzzzzzzzz") == []

    narrowed = [command.name for command in complete("/stat")]
    assert narrowed and narrowed[0] == "/stats", narrowed


def test_an_exact_category_still_renders_its_command_list():
    text = help_text("security")

    assert text.startswith("security")
    assert "did you mean" not in text.lower(), text


def test_an_exact_command_still_renders_its_usage():
    text = help_text("/model")

    assert text.startswith("/model")
    assert "risk:" in text
    assert "did you mean" not in text.lower(), text
