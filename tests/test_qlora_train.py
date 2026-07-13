import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import qlora_train


def _launch(monkeypatch, tmp_path, *, created=100, token="secret"):
    run = tmp_path / "runs" / "run-1"
    output = run / "adapter"
    output.mkdir(parents=True)
    data = tmp_path / "training.jsonl"
    data.write_text(json.dumps({"messages": [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]}) + "\n", encoding="utf-8")
    manifest = run / "training-plan.json"
    manifest.write_text(json.dumps({
        "schema": 2,
        "run_id": "run-1",
        "created_ts": created,
        "base_hf": qlora_train.BASE,
        "hf_revision": qlora_train.HF_REVISION,
        "data_path": str(data.resolve()),
        "data_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "adapter_dir": str(output.resolve()),
        "gpu_index": 0,
        "launch_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }), encoding="utf-8")
    monkeypatch.setenv("SONDER_TRAINING_MANIFEST", str(manifest))
    monkeypatch.setenv("SONDER_TRAINING_LAUNCH_TOKEN", token)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(qlora_train, "DATA_PATH", str(data))
    monkeypatch.setattr(qlora_train, "OUTPUT_DIR", str(output))
    return manifest, data


def test_launch_authorization_is_consumed_once(monkeypatch, tmp_path):
    manifest, _ = _launch(monkeypatch, tmp_path)
    approved = qlora_train.authorize_launch(now=100)
    assert approved["run_id"] == "run-1"
    assert json.loads(manifest.read_text())["launch_consumed_ts"] == 100
    with pytest.raises(RuntimeError, match="already claimed"):
        qlora_train.authorize_launch(now=100)


def test_launch_rejects_changed_training_data(monkeypatch, tmp_path):
    _, data = _launch(monkeypatch, tmp_path)
    data.write_text(json.dumps({"messages": [
        {"role": "user", "content": "changed question"},
        {"role": "assistant", "content": "changed answer"},
    ]}) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed"):
        qlora_train.authorize_launch(now=100)


def test_launch_rejects_expired_capability(monkeypatch, tmp_path):
    _launch(monkeypatch, tmp_path, created=100)
    with pytest.raises(RuntimeError, match="expired"):
        qlora_train.authorize_launch(now=401)


def test_launch_rejects_unreviewed_revision_even_when_manifest_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(qlora_train, "HF_REVISION", "0" * 40)
    _launch(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="reviewed Hugging Face commit"):
        qlora_train.authorize_launch(now=101)


def test_default_adapter_output_uses_sonder_namespace():
    assert qlora_train.OUTPUT_DIR.endswith("sonder-personal-lora")
    assert qlora_train.HF_REVISION == qlora_train.HF_REVISIONS[qlora_train.BASE]


@pytest.mark.skipif(
    importlib.util.find_spec("transformers") is None
    or importlib.util.find_spec("accelerate") is None
    or importlib.util.find_spec("peft") is None,
    reason="training-only dependencies are not installed",
)
def test_installed_trainer_stack_builds_real_peft_trainer(tmp_path):
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        Qwen2Config,
        Qwen2ForCausalLM,
        Trainer,
        TrainingArguments,
    )

    arguments = qlora_train.build_training_arguments(
        TrainingArguments,
        output_dir=tmp_path / "checkpoints",
    )

    assert Path(arguments.output_dir) == tmp_path / "checkpoints"
    assert arguments.report_to == []
    model = Qwen2ForCausalLM(Qwen2Config(
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=1,
        num_attention_heads=4,
        num_key_value_heads=2,
    ))
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(
        r=2,
        lora_alpha=4,
        target_modules=["q_proj", "v_proj"],
        revision=qlora_train.HF_REVISION,
        task_type="CAUSAL_LM",
    ))
    trainer = Trainer(model=model, args=arguments)
    assert trainer.model is model


class FakeTokenizer:
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize
        tokens = [1]
        for message in messages:
            tokens.extend([10] * len(str(message.get("content") or "")))
        if add_generation_prompt:
            tokens.append(99)
        elif messages and messages[-1].get("role") == "assistant":
            # The full template shares the exact generation marker prefix,
            # followed by assistant content and an end token.
            prompt = self.apply_chat_template(
                messages[:-1], tokenize=True, add_generation_prompt=True
            )
            return prompt + [20] * len(messages[-1]["content"]) + [2]
        return tokens


def test_load_examples_accepts_exact_user_assistant_pairs(tmp_path):
    row = {"messages": [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]}
    path = tmp_path / "training.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded = qlora_train.load_examples(path)

    assert loaded == [row]


def test_load_examples_rejects_blank_lines(tmp_path):
    path = tmp_path / "training.jsonl"
    path.write_text("\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="line 1 is empty"):
        qlora_train.load_examples(path)


@pytest.mark.parametrize("record, error", [
    ([], "must be a JSON object"),
    ({}, "only the messages field"),
    ({"messages": "not a list"}, "exactly two messages"),
    ({"messages": [{"role": "user", "content": "x"}]}, "exactly two messages"),
    ({"messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "assistant", "content": "z"},
    ]}, "exactly two messages"),
    ({"messages": ["not an object", {"role": "assistant", "content": "y"}]},
     "message 1 must be an object"),
    ({"messages": [
        {"role": "system", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]}, "ordered user then assistant"),
    ({"messages": [
        {"role": "user", "content": "x"},
        {"role": "user", "content": "y"},
    ]}, "ordered user then assistant"),
    ({"messages": [
        {"role": "user", "content": "  "},
        {"role": "assistant", "content": "y"},
    ]}, "content must be non-empty text"),
    ({"messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": ["not", "text"]},
    ]}, "content must be non-empty text"),
    ({"messages": [
        {"role": "user", "content": "x", "extra": "not allowed"},
        {"role": "assistant", "content": "y"},
    ]}, "only role and content"),
    ({"messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ], "extra": "not allowed"}, "only the messages field"),
])
def test_load_examples_rejects_entire_snapshot_for_invalid_records(
    tmp_path, record, error
):
    good = {"messages": [
        {"role": "user", "content": "valid question"},
        {"role": "assistant", "content": "valid answer"},
    ]}
    path = tmp_path / "training.jsonl"
    path.write_text(
        json.dumps(good) + "\n" + json.dumps(record) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=error):
        qlora_train.load_examples(path)


def test_load_examples_rejects_malformed_json_and_utf8_without_echoing_data(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"messages": [secret-value\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="line 1 is not valid strict JSON") as json_error:
        qlora_train.load_examples(malformed)
    assert "secret-value" not in str(json_error.value)

    invalid_utf8 = tmp_path / "invalid-utf8.jsonl"
    invalid_utf8.write_bytes(b'\xffprivate-value\n')
    with pytest.raises(RuntimeError, match="line 1 is not valid UTF-8") as utf8_error:
        qlora_train.load_examples(invalid_utf8)
    assert "private-value" not in str(utf8_error.value)


def test_load_examples_enforces_file_record_field_and_example_limits(
    monkeypatch, tmp_path
):
    valid = {"messages": [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "answer"},
    ]}
    encoded = json.dumps(valid) + "\n"
    path = tmp_path / "training.jsonl"
    path.write_text(encoded, encoding="utf-8")

    monkeypatch.setattr(qlora_train, "MAX_TRAINING_FILE_BYTES", len(encoded) - 1)
    with pytest.raises(RuntimeError, match="file exceeds"):
        qlora_train.load_examples(path)

    monkeypatch.setattr(qlora_train, "MAX_TRAINING_FILE_BYTES", 1024)
    monkeypatch.setattr(qlora_train, "MAX_TRAINING_RECORD_BYTES", len(encoded) - 1)
    with pytest.raises(RuntimeError, match="record size"):
        qlora_train.load_examples(path)

    monkeypatch.setattr(qlora_train, "MAX_TRAINING_RECORD_BYTES", 1024)
    monkeypatch.setattr(qlora_train, "MAX_TRAINING_FIELD_CHARS", 3)
    with pytest.raises(RuntimeError, match="field size"):
        qlora_train.load_examples(path)

    monkeypatch.setattr(qlora_train, "MAX_TRAINING_FIELD_CHARS", 1024)
    monkeypatch.setattr(qlora_train, "MAX_TRAINING_EXAMPLES", 1)
    path.write_text(encoded + encoded, encoding="utf-8")
    with pytest.raises(RuntimeError, match="too many examples"):
        qlora_train.load_examples(path)


def test_load_examples_hashes_the_same_bytes_it_parses(tmp_path):
    path = tmp_path / "training.jsonl"
    path.write_text(
        json.dumps({"messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]}) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(qlora_train.load_examples(path, digest)) == 1
    with pytest.raises(RuntimeError, match="changed while loading"):
        qlora_train.load_examples(path, "0" * 64)


def test_long_prompt_truncation_preserves_assistant_loss_tokens():
    result = qlora_train.build_supervised_example(
        FakeTokenizer(),
        [
            {"role": "user", "content": "x" * 200},
            {"role": "assistant", "content": "answer"},
        ],
        max_len=32,
    )

    assert len(result["input_ids"]) == 32
    assert len(result["labels"]) == len(result["input_ids"])
    assert result["labels"][:25] == [-100] * 25
    assert all(label != -100 for label in result["labels"][25:])
    assert result["input_ids"][24] == 99


def test_format_training_examples_fails_before_token_lists_exceed_budget(monkeypatch):
    examples = [
        {"messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]},
    ]
    monkeypatch.setattr(qlora_train, "formatted_token_limit", lambda _value=None: 5)

    with pytest.raises(RuntimeError, match="safe in-memory token budget"):
        qlora_train.format_training_examples(
            FakeTokenizer(), examples, max_len=64, ram_budget_gb=1,
        )


def test_template_without_prefix_match_fails_closed():
    class MismatchedTokenizer(FakeTokenizer):
        def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
            value = super().apply_chat_template(
                messages, tokenize=tokenize,
                add_generation_prompt=add_generation_prompt,
            )
            if not add_generation_prompt:
                value[0] = 77
            return value

    with pytest.raises(ValueError, match="not a prefix"):
        qlora_train.build_supervised_example(
            MismatchedTokenizer(),
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
            max_len=64,
        )
