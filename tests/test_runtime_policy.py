import json

import pytest

import runtime_policy


@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("TRILOBITE_RUNTIME_POLICY", str(path))
    return path


def test_default_policy_prefers_shared_trilobite_alias():
    policy = runtime_policy.default_policy(env={})

    assert policy["local_models"] == {
        "fast": "qwen2.5:3b",
        "code": "trilobite:latest",
        "general": "trilobite:latest",
    }
    assert policy["routing"]["router"] == "fast"
    assert policy["routing"]["autopilot"] == "code"


def test_environment_seeds_first_policy_without_allowing_cloud(policy_file, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_FAST", "qwen3:4b")
    monkeypatch.setenv("LOCAL_LLM_CODE", "qwen3-coder:480b-cloud")
    monkeypatch.setenv("LOCAL_LLM_CODE_LOCAL", "trilobite-tuned:latest")
    monkeypatch.setenv("LOCAL_LLM_GENERAL", "qwen2.5:7b-instruct")

    policy = runtime_policy.load(create=True)

    assert policy_file.exists()
    assert policy["local_models"] == {
        "fast": "qwen3:4b",
        "code": "trilobite-tuned:latest",
        "general": "qwen2.5:7b-instruct",
    }


def test_environment_only_seeds_first_policy_creation(policy_file, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_FAST", "seed-fast:latest")
    monkeypatch.setenv("LOCAL_LLM_CODE", "seed-code:latest")
    created = runtime_policy.load(create=True)

    monkeypatch.setenv("LOCAL_LLM_FAST", "later-fast:latest")
    monkeypatch.setenv("LOCAL_LLM_CODE", "later-code:latest")
    loaded = runtime_policy.load(create=False)
    reset = runtime_policy.update(reset=True, source="test reset")

    assert created["local_models"]["fast"] == "seed-fast:latest"
    assert created["local_models"]["code"] == "seed-code:latest"
    assert loaded["local_models"] == created["local_models"]
    assert reset["local_models"] == runtime_policy.DEFAULT_MODELS


def test_update_is_atomic_revisioned_and_hot_read(policy_file):
    initial = runtime_policy.load(create=True)
    updated = runtime_policy.update(
        local_models={"code": "trilobite-personal:latest"},
        routing={"review": "general"},
        source="test",
    )

    assert updated["revision"] == initial["revision"] + 1
    assert updated["local_models"]["code"] == "trilobite-personal:latest"
    assert updated["routing"]["review"] == "general"
    assert updated["source"] == "test"
    assert list(policy_file.parent.glob("runtime_policy.json.tmp-*")) == []

    raw = json.loads(policy_file.read_text(encoding="utf-8"))
    raw["routing"]["workbench"] = "fast"
    policy_file.write_text(json.dumps(raw), encoding="utf-8")
    assert runtime_policy.load()["routing"]["workbench"] == "fast"


def test_cloud_and_unknown_policy_values_are_rejected(policy_file):
    runtime_policy.load(create=True)

    with pytest.raises(ValueError, match="cannot reference cloud"):
        runtime_policy.update(local_models={"code": "qwen3-coder:480b-cloud"})
    with pytest.raises(ValueError, match="unknown local tier"):
        runtime_policy.update(local_models={"cloud-code": "anything"})
    with pytest.raises(ValueError, match="must use"):
        runtime_policy.update(routing={"workbench": "cloud-code"})


def test_invalid_file_fails_visibly_until_explicit_reset(policy_file):
    policy_file.write_text("{broken", encoding="utf-8")

    broken = runtime_policy.load()
    assert broken["error"]
    assert broken["local_models"]["code"] == "trilobite:latest"
    with pytest.raises(ValueError, match="use reset"):
        runtime_policy.update(local_models={"code": "qwen2.5-coder:7b"})

    repaired = runtime_policy.update(reset=True, source="test reset")
    assert repaired["error"] == ""
    assert repaired["revision"] == 1
    assert repaired["source"] == "test reset"


def test_route_tier_is_bounded_to_local_tiers(policy_file):
    policy = runtime_policy.load(create=True)
    assert runtime_policy.route_tier("fleet", policy) == "code"
    assert runtime_policy.route_tier("unknown", policy, fallback="fast") == "fast"
