from sonder_runtime.application.chat.honesty import (
    guard_unsaved_file_claim,
    no_tools_system,
)


def test_plain_chat_is_byte_identical_without_a_file_claim() -> None:
    text = "Python is useful for automation."
    assert guard_unsaved_file_claim(text, folder="creations/lane") == text


def test_unsaved_written_claim_gets_exact_note() -> None:
    text = "I created primes.py for you."
    result = guard_unsaved_file_claim(text, folder="creations/lane")
    assert result.endswith(
        "(Not saved — ask me to create it and I will write it to creations/lane.)"
    )


def test_existing_written_path_does_not_get_note() -> None:
    text = "I wrote primes.py to disk."
    assert (
        guard_unsaved_file_claim(
            text,
            folder="creations/lane",
            written_paths=("C:/state/creations/lane/primes.py",),
        )
        == text
    )


def test_claim_for_different_path_gets_note_even_when_another_file_was_written() -> None:
    text = "I created primes.py for you."
    result = guard_unsaved_file_claim(
        text, folder="creations/lane", written_paths=("notes.txt",)
    )
    assert "(Not saved — ask me to create it" in result


def test_run_instruction_for_unwritten_path_gets_note() -> None:
    text = "Run it with `python primes.py`."
    result = guard_unsaved_file_claim(text, folder="creations/lane")
    assert result.count("(Not saved — ask me to create it") == 1


def test_run_instruction_for_written_path_is_preserved() -> None:
    text = "Run it with python primes.py."
    assert (
        guard_unsaved_file_claim(
            text, folder="creations/lane", written_paths=("primes.py",)
        )
        == text
    )


def test_passive_file_claims_are_guarded() -> None:
    for text in ("primes.py was saved.", "`primes.py` was saved.", "The file was created."):
        assert "(Not saved — ask me to create it" in guard_unsaved_file_claim(
            text, folder="creations/lane"
        )


def test_direct_and_windows_run_paths_are_guarded() -> None:
    for text in ("Run `primes.py`.", r"Run python C:\temp\primes.py."):
        assert "(Not saved — ask me to create it" in guard_unsaved_file_claim(
            text, folder="creations/lane"
        )


def test_future_run_instruction_is_preserved() -> None:
    text = "Once saved, run `primes.py`."
    assert guard_unsaved_file_claim(text, folder="creations/lane") == text


def test_code_fence_does_not_trigger_guard() -> None:
    text = "Example:\n```python\n# write primes.py\n```"
    assert guard_unsaved_file_claim(text, folder="creations/lane") == text


def test_negated_claim_is_not_a_claim() -> None:
    for text in ("I did not create a file; this is only an example.", "No file was saved.", "I haven't saved the file."):
        assert guard_unsaved_file_claim(text, folder="creations/lane") == text


def test_note_is_idempotent() -> None:
    once = guard_unsaved_file_claim("Saved primes.py", folder="creations/lane")
    assert guard_unsaved_file_claim(once, folder="creations/lane") == once


def test_no_tools_guidance_preserves_ordinary_prompts() -> None:
    system = "Answer helpfully."
    assert no_tools_system(system, "How does a Python list work?") == system
    assert no_tools_system(system, "How do I read a file in Python?") == system
    assert no_tools_system(system, "Show me how to read foo.py") == system
    assert no_tools_system(system, "Show me how to write app.py") == system


def test_no_tools_guidance_names_tools_for_workspace_question() -> None:
    result = no_tools_system("Answer helpfully.", "How many files are in this folder?")
    assert "workspace_inventory" in result
    assert "directory_tree" in result
    assert "Ask directly in chat" in result
    assert "do not claim" in result
    assert "gated workbench/agent lane" in result


def test_no_tools_guidance_preserves_generic_show_me_prompt() -> None:
    system = "Answer helpfully."
    assert no_tools_system(system, "Show me a poem about folders.") == system


def test_no_tools_guidance_preserves_explicit_no_tools_file_requests() -> None:
    system = "Answer helpfully.\nKeep this prefix unchanged."
    for prompt in (
        "Read README.md without tools", "How many files are in this folder? No tools.",
        "Do not use tools to inspect the workspace", "Don't read README.md",
    ):
        assert no_tools_system(system, prompt) == system


def test_no_tools_guidance_for_file_fallback_is_idempotent() -> None:
    prompt = "Read README.md"
    once = no_tools_system("system", prompt)
    assert once != "system"
    assert no_tools_system(once, prompt) == once
