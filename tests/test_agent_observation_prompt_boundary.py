"""Agent observation framing lives in the domain; the root names are aliases."""
import server
from sonder_runtime.domain.agents import observation_prompt as framing


def test_root_names_are_identity_preserving_aliases():
    assert server._AGENT_OBSERVATION_PROMPT_CHARS is framing.OBSERVATION_PROMPT_CHARS
    assert server._AGENT_UNTRUSTED_OBSERVATION_HEADER is framing.UNTRUSTED_OBSERVATION_HEADER
    assert server._AGENT_UNTRUSTED_OBSERVATION_FOOTER is framing.UNTRUSTED_OBSERVATION_FOOTER
    assert server._clip_agent_prompt_text is framing.clip_prompt_text
    assert server._frame_agent_observations is framing.frame_observations
    assert server._agent_observation_prompt is framing.observation_prompt
    assert framing.OBSERVATION_PROMPT_CHARS == 9000


def test_clipping_keeps_both_ends_within_the_limit():
    assert framing.clip_prompt_text("short", 100) == "short"
    assert framing.clip_prompt_text("abcdefghij", 4) == "abcd"
    text = "H" * 500 + "T" * 500
    clipped = framing.clip_prompt_text(text, 200)
    assert len(clipped) == 200
    assert clipped.startswith("HHH")
    assert clipped.endswith("TTT")
    assert "...[observation compacted by host]..." in clipped
    assert framing.clip_prompt_text(None, 10) == ""


def test_framing_wraps_a_body_clipped_to_the_envelope_budget():
    header = framing.UNTRUSTED_OBSERVATION_HEADER
    footer = framing.UNTRUSTED_OBSERVATION_FOOTER
    framed = framing.frame_observations("evidence", 4000)
    assert framed == header + "evidence" + footer
    tight = framing.frame_observations("x" * 4000, len(header) + len(footer) + 10)
    assert tight.startswith(header) and tight.endswith(footer)
    assert len(tight) == len(header) + len(footer) + 10


def test_observation_prompt_bounds_context_and_keeps_recent_evidence():
    assert framing.observation_prompt([]) == ""
    assert framing.observation_prompt(["", "   "]) == ""
    small = framing.observation_prompt(["file_read: ok", "grep: 3 hits"])
    assert small.startswith(framing.UNTRUSTED_OBSERVATION_HEADER)
    assert "Tool observations so far:\nfile_read: ok\n\ngrep: 3 hits" in small
    assert small.endswith(framing.UNTRUSTED_OBSERVATION_FOOTER)
    observations = ["step %d: %s" % (index, "y" * 300) for index in range(12)]
    prompt = framing.observation_prompt(observations, max_chars=1800)
    assert len(prompt) <= 1800
    assert "Earlier observation summaries (" in prompt
    assert "Recent tool observations (full host ledger retained):" in prompt
    assert "step 11:" in prompt


def test_observation_prompt_keeps_the_untrusted_envelope_when_clipped():
    prompt = framing.observation_prompt(["x" * 4000], max_chars=512)
    assert prompt.startswith(framing.UNTRUSTED_OBSERVATION_HEADER)
    assert prompt.endswith(framing.UNTRUSTED_OBSERVATION_FOOTER)
    assert len(prompt) <= 512
