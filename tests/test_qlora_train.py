import json
import hashlib

import pytest

import qlora_train


def _launch(monkeypatch, tmp_path, *, created=100, token="secret"):
    run = tmp_path / "runs" / "run-1"
    output = run / "adapter"
    output.mkdir(parents=True)
    data = tmp_path / "training.jsonl"
    data.write_text("{}\n", encoding="utf-8")
    manifest = run / "training-plan.json"
    manifest.write_text(json.dumps({
        "schema": 2,
        "run_id": "run-1",
        "created_ts": created,
        "base_hf": qlora_train.BASE,
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
    data.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed"):
        qlora_train.authorize_launch(now=100)


def test_launch_rejects_expired_capability(monkeypatch, tmp_path):
    _launch(monkeypatch, tmp_path, created=100)
    with pytest.raises(RuntimeError, match="expired"):
        qlora_train.authorize_launch(now=401)


def test_default_adapter_output_uses_sonder_namespace():
    assert qlora_train.OUTPUT_DIR.endswith("sonder-personal-lora")


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


def test_load_examples_rejects_non_assistant_or_empty_targets(tmp_path):
    rows = [
        {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "good"}]},
        {"messages": [{"role": "user", "content": "x"}, {"role": "user", "content": "not a target"}]},
        {"messages": [{"role": "system", "content": "x"}, {"role": "assistant", "content": "no user"}]},
        {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "  "}]},
        {"messages": [{"role": "user", "content": ["not", "text"]}, {"role": "assistant", "content": "bad"}]},
    ]
    path = tmp_path / "training.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    loaded = qlora_train.load_examples(path)

    assert loaded == [rows[0]]


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
