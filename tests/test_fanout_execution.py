"""Execution helpers shared by plain fanout and sealed batch child runs."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters import fanout_execution as ex
from sonder_runtime.domain.fanout_admission import fanout_limits


def _limits(**raw):
    import json
    return fanout_limits({"limits_json": json.dumps(raw)})


def test_plain_run_payload_is_the_raw_prompt_with_no_system():
    assert ex.fanout_payload(_limits(), "just a prompt") == ("just a prompt", "")


def test_batch_run_payload_is_the_sealed_envelope():
    limits = _limits(batch_job_id="job-1")
    assert ex.fanout_payload(limits, ex.seal_envelope("be terse", "question?")) == ("question?", "be terse")


@pytest.mark.parametrize("plaintext", ["not json", "[]", '{"v":2,"system":"","prompt":"x"}', '{"v":1,"prompt":"x"}'])
def test_malformed_envelope_is_rejected(plaintext):
    with pytest.raises(ValueError):
        ex.fanout_payload(_limits(batch_job_id="job-1"), plaintext)


def test_call_metadata_keeps_unreported_tokens_unknown():
    class Gen:
        last_response_meta = {"served_model": "m:2"}
        last_usage = {}

    assert ex.call_metadata(Gen) == {"served_model": "m:2"}
    assert ex.call_metadata(None) == {}
    Gen.last_usage = {"tokens_in": 4, "tokens_out": 2, "token_source": "ollama"}
    assert ex.call_metadata(Gen)["tokens_in"] == 4


def test_plain_answers_are_redacted_and_sealed_answers_are_exact():
    prompt = "p" * 10 + "SPAN-QUOTED-FROM-THE-INPUT-0123456789" + "q" * 10
    raw = "see " + "SPAN-QUOTED-FROM-THE-INPUT-0123456789" + " done"
    plain_meta, sealed_meta = {}, {}

    plain = ex.stored_answer(_limits(), raw, prompt, plain_meta, seal=lambda text: pytest.fail("no seal"))
    sealed = ex.stored_answer(
        _limits(batch_job_id="j", sealed_answers=True), raw, prompt, sealed_meta,
        seal=lambda text: "TOKEN:" + text,
    )

    assert "<redacted prompt>" in plain and "sealed_answer" not in plain_meta
    assert sealed == "" and sealed_meta["sealed_answer"] == "TOKEN:" + raw
    assert sealed_meta["sealed_truncated"] is False


def test_sealed_answer_is_bounded_and_flags_truncation():
    meta = {}
    ex.stored_answer(_limits(sealed_answers=True, answer_max_chars=1024), "x" * 2000, "q", meta, seal=lambda t: t)
    assert len(meta["sealed_answer"]) == 1024 and meta["sealed_truncated"] is True
    assert ex.result_fields(meta)["sealed_truncated"] is True
