import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import persona_pack


EXPECTED_NAMES = {"debugger", "security-reviewer", "perf-optimizer", "teacher"}


def test_get_pack_returns_dict():
    assert isinstance(persona_pack.get_pack(), dict)


def test_get_pack_has_exactly_four_personas():
    pack = persona_pack.get_pack()
    assert set(pack) == EXPECTED_NAMES
    assert len(pack) == 4


def test_names_matches_pack_keys():
    assert persona_pack.names() == sorted(EXPECTED_NAMES)


def test_all_prompts_are_non_empty_strings():
    for name, prompt in persona_pack.get_pack().items():
        assert isinstance(prompt, str)
        assert prompt.strip() != ""


def test_all_prompts_are_reasonably_substantive():
    # Guard against placeholder/one-line stubs slipping in.
    for prompt in persona_pack.get_pack().values():
        assert len(prompt) >= 80


def test_each_prompt_mentions_sonder_and_its_own_mode():
    for name, prompt in persona_pack.get_pack().items():
        assert "sonder" in prompt.lower()
        assert name.split("-")[0] in prompt.lower()


def test_get_pack_returns_a_copy_not_the_live_dict():
    pack = persona_pack.get_pack()
    pack["debugger"] = "tampered"
    assert persona_pack.get_pack()["debugger"] != "tampered"
    assert persona_pack.PACK["debugger"] != "tampered"


def test_get_known_name_returns_matching_prompt():
    assert persona_pack.get("security-reviewer") == persona_pack.PACK["security-reviewer"]


def test_get_unknown_falls_back_to_default():
    assert persona_pack.get("nonexistent") == persona_pack.PACK[persona_pack.DEFAULT]


def test_get_none_falls_back_to_default():
    assert persona_pack.get(None) == persona_pack.PACK[persona_pack.DEFAULT]


def test_get_empty_string_falls_back_to_default():
    assert persona_pack.get("") == persona_pack.PACK[persona_pack.DEFAULT]


def test_get_is_case_and_whitespace_insensitive():
    assert persona_pack.get("  Perf-Optimizer  ") == persona_pack.PACK["perf-optimizer"]


def test_default_is_a_valid_pack_member():
    assert persona_pack.DEFAULT in persona_pack.PACK


def test_merges_cleanly_alongside_base_personas_without_editing_it():
    import personas

    merged = {**personas.PERSONAS, **persona_pack.get_pack()}
    # base personas keeps its own entries...
    assert merged["coder"] == personas.PERSONAS["coder"]
    # ...and the pack's entries are present and distinct from the base
    # "teacher" persona (same key, different specialization/wording).
    assert merged["teacher"] == persona_pack.PACK["teacher"]
    assert persona_pack.PACK["teacher"] != personas.PERSONAS["teacher"]
