"""NPU policy states: off by default, backward compatible, and inert about
anything except accelerator behavior."""
import json

import pytest

import runtime_policy


@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    return path


def test_default_policy_has_npu_off():
    policy = runtime_policy.default_policy(env={})
    assert policy["npu"] == {"mode": "off", "routing": "", "embeddings": ""}


def test_legacy_policy_file_without_npu_normalizes_to_off(policy_file):
    policy_file.write_text(json.dumps({
        "version": 1,
        "revision": 3,
        "local_models": {
            "fast": "qwen2.5:3b", "code": "sonder:latest",
            "general": "sonder:latest",
        },
        "routing": {
            "router": "fast", "workbench": "code", "autopilot": "code",
            "fleet": "code", "review": "code",
        },
        "updated_ts": 0,
        "source": "legacy",
    }), encoding="utf-8")
    policy = runtime_policy.load()
    assert policy["error"] == ""
    assert policy["npu"]["mode"] == "off"
    assert runtime_policy.npu_mode("routing", policy) == "off"
    assert runtime_policy.npu_mode("embeddings", policy) == "off"


def test_update_npu_mode_round_trips_and_keeps_models(policy_file):
    before = runtime_policy.load()
    updated = runtime_policy.update(npu={"mode": "shadow"})
    assert updated["npu"]["mode"] == "shadow"
    assert updated["local_models"] == before["local_models"]
    assert updated["routing"] == before["routing"]
    reloaded = runtime_policy.load()
    assert reloaded["npu"]["mode"] == "shadow"


def test_per_capability_override_resolves_effective_mode(policy_file):
    runtime_policy.update(npu={"mode": "shadow", "routing": "prefer"})
    policy = runtime_policy.load()
    assert runtime_policy.npu_mode("routing", policy) == "prefer"
    assert runtime_policy.npu_mode("embeddings", policy) == "shadow"
    assert runtime_policy.npu_mode("unknown-capability", policy) == "off"


def test_invalid_npu_mode_is_rejected(policy_file):
    with pytest.raises(ValueError):
        runtime_policy.update(npu={"mode": "turbo"})
    with pytest.raises(ValueError):
        runtime_policy.update(npu={"routing": "cloud"})


def test_unknown_npu_keys_are_rejected(policy_file):
    with pytest.raises(ValueError):
        runtime_policy.update(npu={"mode": "shadow", "worker_path": "C:/evil.py"})
    with pytest.raises(ValueError):
        runtime_policy.update(npu={"allow_cloud": True})


def test_invalid_npu_section_in_file_falls_back_to_safe_defaults(policy_file):
    payload = runtime_policy.default_policy(env={})
    payload["npu"] = {"mode": "warp-speed"}
    policy_file.write_text(json.dumps(
        {key: payload[key] for key in (
            "version", "revision", "local_models", "routing", "updated_ts",
            "source", "npu",
        )}
    ), encoding="utf-8")
    policy = runtime_policy.load()
    assert policy["error"]
    assert policy["npu"]["mode"] == "off"


def test_format_policy_reports_npu_modes(policy_file):
    runtime_policy.update(npu={"mode": "prefer"})
    text = runtime_policy.format_policy(runtime_policy.load())
    assert "npu accelerator" in text
    assert "prefer" in text
