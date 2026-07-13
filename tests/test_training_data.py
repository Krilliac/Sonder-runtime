import hashlib
import json

import pytest

import training_data


def _record(user="question", assistant="answer"):
    return {"messages": [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]}


def test_encode_and_inspect_round_trip_non_ascii_without_schema_drift(tmp_path):
    records = [_record("why ☃?", "because U0001f680")]
    payload = training_data.encode_jsonl(records)
    path = tmp_path / "training.jsonl"
    path.write_bytes(payload)

    inspected = training_data.inspect_jsonl(
        path, hashlib.sha256(payload).hexdigest()
    )

    assert inspected.examples == records
    assert inspected.file_bytes == len(payload)
    assert inspected.content_chars == len("why ☃?because U0001f680")


@pytest.mark.parametrize(
    "payload,error",
    [
        ('{"messages":[],"extra":1}\n', "only the messages"),
        (
            '{"messages":[{"role":"user","content":"x","extra":1},'
            '{"role":"assistant","content":"y"}]}\n',
            "only role and content",
        ),
        (
            '{"messages":[],"messages":[]}\n',
            "strict JSON",
        ),
        (
            '{"messages":[{"role":"user","content":NaN},'
            '{"role":"assistant","content":"y"}]}\n',
            "strict JSON",
        ),
        (
            '{"messages":[{"role":"user","content":"\\ud800"},'
            '{"role":"assistant","content":"y"}]}\n',
            "invalid Unicode",
        ),
        ("\n", "line 1 is empty"),
    ],
)
def test_inspect_rejects_noncanonical_or_unsafe_records(tmp_path, payload, error):
    path = tmp_path / "training.jsonl"
    path.write_bytes(payload.encode("utf-8"))

    with pytest.raises(training_data.TrainingDataError, match=error):
        training_data.inspect_jsonl(path)


def test_inspect_enforces_exact_boundaries_and_hashes_same_stream(tmp_path):
    payload = training_data.encode_jsonl([_record()])
    path = tmp_path / "training.jsonl"
    path.write_bytes(payload)
    exact = training_data.Limits(
        file_bytes=len(payload),
        record_bytes=len(payload),
        field_chars=len("question"),
        total_chars=len("questionanswer"),
        examples=1,
    )

    assert training_data.inspect_jsonl(path, limits=exact).examples == [_record()]
    with pytest.raises(training_data.TrainingDataError, match="changed while loading"):
        training_data.inspect_jsonl(path, "0" * 64, limits=exact)

    for field, error in (
        ("file_bytes", "file exceeds"),
        ("record_bytes", "record size"),
        ("field_chars", "field size"),
        ("total_chars", "aggregate content"),
        ("examples", "too many examples"),
    ):
        values = dict(exact.__dict__)
        values[field] -= 1
        with pytest.raises(training_data.TrainingDataError, match=error):
            training_data.inspect_jsonl(path, limits=training_data.Limits(**values))


def test_json_outer_non_json_whitespace_is_rejected(tmp_path):
    path = tmp_path / "training.jsonl"
    path.write_text("\u00a0" + json.dumps(_record()) + "\n", encoding="utf-8")

    with pytest.raises(training_data.TrainingDataError, match="strict JSON"):
        training_data.inspect_jsonl(path)


def test_escaped_surrogate_pair_is_normalized_but_unpaired_low_is_rejected(tmp_path):
    valid = tmp_path / "valid.jsonl"
    valid.write_text(
        '{"messages":[{"role":"user","content":"\\ud83d\\ude80"},'
        '{"role":"assistant","content":"ok"}]}\n',
        encoding="ascii",
    )
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(
        '{"messages":[{"role":"user","content":"\\ude80"},'
        '{"role":"assistant","content":"ok"}]}\n',
        encoding="ascii",
    )

    assert training_data.inspect_jsonl(valid).examples[0]["messages"][0][
        "content"
    ] == chr(0x1F680)
    with pytest.raises(training_data.TrainingDataError, match="invalid Unicode"):
        training_data.inspect_jsonl(invalid)
