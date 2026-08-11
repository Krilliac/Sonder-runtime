"""The agent is told the standing its own claims are being made under (#23).

The runtime measures how often delegated work was judged good. That number was
computed (``calibration.measure``) and shown to the *caller* (``calibration_status``)
and to nobody else. The agent -- the thing actually making the claims -- ran with
no idea whether its output had been landing.

The subtle half is *which* fact to hand over. ``calibration.should_verify``
returns ``(True, reason)`` for a measured-poor record and ``(True, reason)`` for
a record too thin to measure at all. Same boolean, two entirely different facts:
"we measured you and you are unreliable" versus "we have never measured you".
An agent given only the boolean collapses them, which is exactly the shape the
claim-review lane fixed on the reviewer path, where "the tool ran and found
nothing" was indistinguishable from "the tool was never allowed to run". So the
standing is surfaced as an explicit three-way state, never as a bare percentage
and never as a bare boolean.

The counts are read from the real store shape; nothing here is generated prose.
"""
from __future__ import annotations

import calibration
import pytest
import server
import sonder_runtime.adapters.memory_store as memory_store


MIN = calibration.MIN_SAMPLE

# Populations, shaped to land on each verdict. Signal names are the store's own.
_GOOD_RECORD = {"used": 40, "accepted": 50, "rejected": 4}          # 95.7%
_POOR_RECORD = {"used": 5, "accepted": 6, "rejected": 40}           # 21.6%
_THIN_RECORD = {"used": 2, "rejected": 1}                           # n=3, unmeasurable
# Self-graded execution rows, ~50x larger and far higher: the population that
# must never be blended into the figure the agent is shown.
_EXECUTION = {"tests_passed": 9049, "failed": 182, "compiled": 1}


def _with_counts(monkeypatch, counts):
    monkeypatch.setattr(
        memory_store, "outcome_signal_counts", lambda _conn: dict(counts),
    )


def _hosted_target(*_a, **_k):
    return ("stub-model", True, False, "stub-tier")


def _local_target(*_a, **_k):
    return ("stub-model", False, False, "stub-tier")


def _agent_transcript(monkeypatch, counts, *, cloud=False):
    """Exactly the text the agent model is shown, system prompt included.

    Captured at the generator seam rather than scanned out of the source, so a
    block that is built but never reaches the model cannot pass.
    """
    _with_counts(monkeypatch, counts)
    captured = {}

    def fake_make_generate(_model, system, *_a, **_k):
        captured["system"] = str(system)

        def generate(prompt, history=None):
            captured.setdefault("prompt", str(prompt))
            return '{"final":"done"}'

        return generate

    monkeypatch.setattr(server, "_serve_target",
                        _hosted_target if cloud else _local_target)
    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    server._agent_impl("summarise the repository", max_steps=1)
    assert "prompt" in captured, (
        "the agent model was never reached; this test measured nothing"
    )
    return captured.get("system", "") + "\n" + captured["prompt"]


def _standing_block(text):
    """Just the standing section, so surrounding prose cannot satisfy a test."""
    marker = "VERIFICATION STANDING"
    assert marker in text, "no %r block in the agent transcript" % marker
    tail = text.split(marker, 1)[1]
    out = []
    for line in tail.splitlines():
        if out and line and not line.startswith((" ", "\t")):
            break
        out.append(line)
    return marker + "\n".join(out)


# --------------------------------------------------------------------------
# Non-vacuity.
# --------------------------------------------------------------------------

def test_the_transcript_seam_actually_captures_the_agent_transcript(monkeypatch):
    text = _agent_transcript(monkeypatch, _POOR_RECORD)
    assert len(text) > 200, "captured transcript is implausibly short"
    assert "summarise the repository" in text, "the task never reached the model"


def test_the_fixtures_land_on_the_three_verdicts(monkeypatch):
    """If every fixture produced the same verdict the tests below prove nothing."""
    _with_counts(monkeypatch, _GOOD_RECORD)
    assert calibration.measure(None, "caller").verdict == calibration.GOOD
    _with_counts(monkeypatch, _POOR_RECORD)
    assert calibration.measure(None, "caller").verdict == calibration.POOR
    _with_counts(monkeypatch, _THIN_RECORD)
    thin = calibration.measure(None, "caller")
    assert thin.verdict == calibration.UNMEASURED
    assert thin.total < MIN and thin.rate is None


# --------------------------------------------------------------------------
# The agent is told at all.
# --------------------------------------------------------------------------

def test_the_agent_transcript_carries_the_verification_standing(monkeypatch):
    text = _agent_transcript(monkeypatch, _POOR_RECORD)
    assert "VERIFICATION STANDING" in text, (
        "the standing is computed and shown to the caller but never to the agent"
    )


def test_a_hosted_agent_is_told_the_standing_too(monkeypatch):
    text = _agent_transcript(monkeypatch, _POOR_RECORD, cloud=True)
    assert "VERIFICATION STANDING" in text


def test_the_agent_is_told_how_many_rows_back_the_figure(monkeypatch):
    block = _standing_block(_agent_transcript(monkeypatch, _POOR_RECORD))
    total = sum(_POOR_RECORD.values())
    assert str(total) in block, (
        "a percentage with no sample size behind it is exactly the reassuring "
        "number this defect is about; block was:\n%s" % block
    )


# --------------------------------------------------------------------------
# The three states, kept apart.
# --------------------------------------------------------------------------

def test_unmeasurable_and_poor_do_not_read_the_same_to_the_agent(monkeypatch):
    poor = _standing_block(_agent_transcript(monkeypatch, _POOR_RECORD))
    thin = _standing_block(_agent_transcript(monkeypatch, _THIN_RECORD))
    assert poor != thin, (
        "'measured and unreliable' and 'never measured' render identically; "
        "collapsing them is the defect"
    )


def test_all_three_states_render_differently(monkeypatch):
    blocks = [
        _standing_block(_agent_transcript(monkeypatch, rec))
        for rec in (_GOOD_RECORD, _POOR_RECORD, _THIN_RECORD)
    ]
    assert len(set(blocks)) == 3, blocks


def test_an_unmeasurable_standing_quotes_no_percentage(monkeypatch):
    block = _standing_block(_agent_transcript(monkeypatch, _THIN_RECORD))
    assert "%" not in block, (
        "there is no rate to quote below n=%d; printing one invents evidence:\n%s"
        % (MIN, block)
    )
    assert calibration.UNVERIFIABLE in block


def test_a_measured_standing_does_quote_its_rate(monkeypatch):
    """The mirror of the test above, so 'never print a rate' cannot pass both."""
    block = _standing_block(_agent_transcript(monkeypatch, _POOR_RECORD))
    assert "%" in block
    assert calibration.UNVERIFIED in block


def test_a_good_standing_says_so(monkeypatch):
    block = _standing_block(_agent_transcript(monkeypatch, _GOOD_RECORD))
    assert calibration.VERIFIED_GOOD in block


def test_the_three_state_names_are_distinct():
    assert len(set(calibration.STATES)) == 3
    for state in calibration.STATES:
        assert state and isinstance(state, str)


def test_standing_never_collapses_unmeasured_into_poor(monkeypatch):
    """At the API, not just in the rendering."""
    _with_counts(monkeypatch, _THIN_RECORD)
    thin_state, _ = calibration.standing(None, "caller")
    _with_counts(monkeypatch, _POOR_RECORD)
    poor_state, _ = calibration.standing(None, "caller")
    _with_counts(monkeypatch, _GOOD_RECORD)
    good_state, _ = calibration.standing(None, "caller")
    assert (thin_state, poor_state, good_state) == (
        calibration.UNVERIFIABLE, calibration.UNVERIFIED, calibration.VERIFIED_GOOD,
    )
    # should_verify is True for the first two: the boolean is what collapses.
    _with_counts(monkeypatch, _THIN_RECORD)
    assert calibration.should_verify(None, "caller")[0] is True
    _with_counts(monkeypatch, _POOR_RECORD)
    assert calibration.should_verify(None, "caller")[0] is True


# --------------------------------------------------------------------------
# The populations stay apart.
# --------------------------------------------------------------------------

def test_the_agent_is_never_shown_the_two_populations_averaged(monkeypatch):
    mixed = dict(_POOR_RECORD)
    mixed.update(_EXECUTION)
    block = _standing_block(_agent_transcript(monkeypatch, mixed))
    _with_counts(monkeypatch, mixed)
    caller = calibration.measure(None, "caller")
    execution = calibration.measure(None, "execution")
    blended = (caller.good + execution.good) / (caller.total + execution.total)
    assert abs(caller.rate - execution.rate) > 0.3, "fixtures too close to detect a blend"
    assert "%.0f%%" % (blended * 100) not in block, (
        "the agent is being shown the two populations averaged:\n%s" % block
    )
    assert str(caller.total) in block
    assert "caller" in block


def test_calibration_still_refuses_to_offer_a_combined_figure():
    assert not hasattr(calibration, "overall")


def test_an_unknown_population_is_still_rejected():
    with pytest.raises(ValueError):
        calibration.standing(None, "everything")


def test_the_standing_helper_is_reachable_and_renders_the_three_states():
    """Name _agent_verification_standing/agent_notice so a rename selects."""
    assert callable(server._agent_verification_standing)
    assert callable(calibration.agent_notice)
    assert set(calibration.STATES) == {
        calibration.VERIFIED_GOOD, calibration.UNVERIFIED, calibration.UNVERIFIABLE,
    }
