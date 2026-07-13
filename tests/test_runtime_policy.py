import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import runtime_policy


@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "sonder-home"))
    return path


def test_default_policy_prefers_shared_sonder_alias():
    policy = runtime_policy.default_policy(env={})

    assert policy["local_models"] == {
        "fast": "qwen2.5:3b",
        "code": "sonder:latest",
        "general": "sonder:latest",
    }
    assert policy["routing"]["router"] == "fast"
    assert policy["routing"]["autopilot"] == "code"


def test_environment_seeds_first_policy_without_allowing_cloud(policy_file, monkeypatch):
    monkeypatch.setenv("SONDER_FAST", "qwen3:4b")
    monkeypatch.setenv("SONDER_CODE", "qwen3-coder:480b-cloud")
    monkeypatch.setenv("SONDER_CODE_LOCAL", "sonder-tuned:latest")
    monkeypatch.setenv("SONDER_GENERAL", "qwen2.5:7b-instruct")

    policy = runtime_policy.load(create=True)

    assert policy_file.exists()
    assert policy["local_models"] == {
        "fast": "qwen3:4b",
        "code": "sonder-tuned:latest",
        "general": "qwen2.5:7b-instruct",
    }


def test_environment_only_seeds_first_policy_creation(policy_file, monkeypatch):
    monkeypatch.setenv("SONDER_FAST", "seed-fast:latest")
    monkeypatch.setenv("SONDER_CODE", "seed-code:latest")
    created = runtime_policy.load(create=True)

    monkeypatch.setenv("SONDER_FAST", "later-fast:latest")
    monkeypatch.setenv("SONDER_CODE", "later-code:latest")
    loaded = runtime_policy.load(create=False)
    reset = runtime_policy.update(reset=True, source="test reset")

    assert created["local_models"]["fast"] == "seed-fast:latest"
    assert created["local_models"]["code"] == "seed-code:latest"
    assert loaded["local_models"] == created["local_models"]
    assert reset["local_models"] == runtime_policy.DEFAULT_MODELS


def test_first_policy_creation_is_serialized_across_processes(
    policy_file, monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_CODE", "parent-seed:latest")
    signal = tmp_path / "child-holds-policy-lock"
    attempted = tmp_path / "parent-attempted-policy-lock"
    result = tmp_path / "child-policy.json"
    script = r'''
import json
import os
from pathlib import Path
import time
import runtime_policy

path = runtime_policy.policy_path()
with runtime_policy._policy_file_lock(path=path):
    Path(os.environ["TEST_SIGNAL"]).write_text("locked", encoding="utf-8")
    deadline = time.monotonic() + 5
    attempt = Path(os.environ["TEST_ATTEMPT"])
    while not attempt.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not attempt.exists():
        raise RuntimeError("competing creator never attempted the policy lock")
    created = runtime_policy._load_unlocked(path, create=True)
Path(os.environ["TEST_RESULT"]).write_text(json.dumps(created), encoding="utf-8")
'''
    env = os.environ.copy()
    env["SONDER_RUNTIME_POLICY"] = str(policy_file)
    env["SONDER_CODE"] = "child-seed:latest"
    env["TEST_SIGNAL"] = str(signal)
    env["TEST_ATTEMPT"] = str(attempted)
    env["TEST_RESULT"] = str(result)
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(runtime_policy.__file__).parent,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not signal.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert signal.exists()

        original_file_lock = runtime_policy._policy_file_lock

        @contextlib.contextmanager
        def signaled_file_lock(*args, **kwargs):
            attempted.write_text("attempted", encoding="utf-8")
            with original_file_lock(*args, **kwargs):
                yield

        monkeypatch.setattr(runtime_policy, "_policy_file_lock", signaled_file_lock)
        loaded = runtime_policy.load(create=True)

        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    child_policy = json.loads(result.read_text(encoding="utf-8"))
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert loaded["local_models"]["code"] == "child-seed:latest"
    assert child_policy["local_models"] == loaded["local_models"]
    assert on_disk["local_models"] == loaded["local_models"]


def test_transition_reservation_is_policy_local_atomic_and_exclusive(policy_file):
    current, journal = runtime_policy.reserve_transition({
        "schema": 1,
        "deployment_id": "deploy-one",
        "policy_path": "spoofed",
        "prior_models": {"code": "spoofed"},
        "prior_policy_revision": 999,
        "last_policy_revision": 999,
        "policy_token": "transition-token",
    })

    marker = runtime_policy.transition_path()
    saved = json.loads(marker.read_text(encoding="utf-8"))
    assert marker == policy_file.with_name(policy_file.name + ".transition.json")
    assert journal == saved
    assert journal["policy_path"] == str(policy_file.resolve())
    assert journal["prior_models"] == current["local_models"]
    assert journal["prior_policy_revision"] == current["revision"]
    assert journal["last_policy_revision"] == current["revision"]
    with pytest.raises(RuntimeError, match="active model deployment"):
        runtime_policy.reserve_transition({"deployment_id": "deploy-two"})


def test_finish_transition_requires_exact_id_token_and_policy_path(policy_file):
    _current, journal = runtime_policy.reserve_transition({
        "deployment_id": "deploy-one",
        "policy_token": "transition-token",
    })
    marker = runtime_policy.transition_path()

    with pytest.raises(RuntimeError, match="id does not match"):
        runtime_policy.finish_transition("deploy-two", "transition-token")
    with pytest.raises(RuntimeError, match="token does not match"):
        runtime_policy.finish_transition("deploy-one", "wrong-token")
    assert marker.exists()

    tampered = {**journal, "policy_path": str(policy_file.with_name("other.json"))}
    runtime_policy._write_json_atomic(marker, tampered)
    with pytest.raises(RuntimeError, match="another policy"):
        runtime_policy.finish_transition("deploy-one", "transition-token")
    assert marker.exists()

    runtime_policy._write_json_atomic(marker, journal)
    assert runtime_policy.finish_transition("deploy-one", "transition-token") is True
    assert not marker.exists()
    with pytest.raises(RuntimeError, match="no active model deployment"):
        runtime_policy.finish_transition("deploy-one", "transition-token")


def test_update_is_atomic_revisioned_and_hot_read(policy_file):
    initial = runtime_policy.load(create=True)
    updated = runtime_policy.update(
        local_models={"code": "sonder-tuned:latest"},
        routing={"review": "general"},
        source="test",
    )

    assert updated["revision"] == initial["revision"] + 1
    assert updated["local_models"]["code"] == "sonder-tuned:latest"
    assert updated["routing"]["review"] == "general"
    assert updated["source"] == "test"
    assert list(policy_file.parent.glob("runtime_policy.json.tmp-*")) == []

    raw = json.loads(policy_file.read_text(encoding="utf-8"))
    raw["routing"]["workbench"] = "fast"
    policy_file.write_text(json.dumps(raw), encoding="utf-8")
    assert runtime_policy.load()["routing"]["workbench"] == "fast"


def test_personal_alias_routing_requires_active_transition(policy_file):
    initial = runtime_policy.load(create=True)
    aliases = (
        "sonder-personal:latest",
        "SONDER-PERSONAL:latest",
        "Sonder-Personal:Latest",
        "sonder-personal",
        "library/sonder-personal",
        "library/sonder-personal:latest",
        "registry.ollama.ai/library/SONDER-PERSONAL",
        "registry.ollama.ai/library/sonder-personal:latest",
    )
    for alias in aliases:
        with pytest.raises(ValueError, match="reserved for an active validated deployment"):
            runtime_policy.update(local_models={"code": alias})

    _current, journal = runtime_policy.reserve_transition({
        "deployment_id": "validated-deployment",
        "policy_token": "validated-token",
    })
    updated = runtime_policy.update(
        local_models={"code": "sonder-personal:latest"},
        expected_revision=initial["revision"],
        transition_token=journal["policy_token"],
    )

    assert updated["local_models"]["code"] == "sonder-personal:latest"
    assert runtime_policy.finish_transition(
        journal["deployment_id"], journal["policy_token"],
    )


def test_personal_alias_case_is_canonicalized_and_never_environment_seeded(
    policy_file, monkeypatch,
):
    monkeypatch.setenv("SONDER_CODE", "SONDER-PERSONAL:latest")
    created = runtime_policy.load(create=True)
    assert created["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]

    raw = json.loads(policy_file.read_text(encoding="utf-8"))
    raw["local_models"]["code"] = "registry.ollama.ai/library/SoNdEr-PeRsOnAl"
    policy_file.write_text(json.dumps(raw), encoding="utf-8")

    assert runtime_policy.load(create=False)["local_models"]["code"] == (
        runtime_policy.RESERVED_PERSONAL_MODEL
    )


def test_update_expected_revision_rejects_concurrent_writer(policy_file):
    initial = runtime_policy.load(create=True)
    newer = runtime_policy.update(local_models={"code": "new-code:latest"})

    with pytest.raises(RuntimeError, match="changed concurrently"):
        runtime_policy.update(
            local_models={"general": "new-general:latest"},
            expected_revision=initial["revision"],
        )

    current = runtime_policy.load(create=False)
    assert current["revision"] == newer["revision"]
    assert current["local_models"]["general"] == "sonder:latest"


def test_expected_revision_is_serialized_across_processes(policy_file, tmp_path):
    initial = runtime_policy.load(create=True)
    signal = tmp_path / "locked"
    script = r'''
import os
from pathlib import Path
import time
import runtime_policy

path = runtime_policy.policy_path()
with runtime_policy._policy_file_lock(path=path):
    current = runtime_policy._load_unlocked(path, create=True)
    Path(os.environ["TEST_SIGNAL"]).write_text("locked", encoding="utf-8")
    time.sleep(0.35)
    candidate = {
        **current,
        "local_models": {**current["local_models"], "code": "child-model:latest"},
        "revision": current["revision"] + 1,
        "updated_ts": int(time.time()),
        "source": "child",
    }
    runtime_policy._write(runtime_policy.normalize(candidate), path)
'''
    env = os.environ.copy()
    env["SONDER_RUNTIME_POLICY"] = str(policy_file)
    env["TEST_SIGNAL"] = str(signal)
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(runtime_policy.__file__).parent,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not signal.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert signal.exists()
        with pytest.raises(RuntimeError, match="changed concurrently"):
            runtime_policy.update(
                local_models={"general": "parent-model:latest"},
                expected_revision=initial["revision"],
            )
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    current = runtime_policy.load(create=False)
    assert current["local_models"]["code"] == "child-model:latest"
    assert current["local_models"]["general"] == "sonder:latest"


def test_active_deployment_journal_blocks_ordinary_policy_updates(policy_file):
    initial = runtime_policy.load(create=True)
    journal = runtime_policy.transition_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({
        "policy_path": str(policy_file.resolve()),
        "policy_token": "transition-token",
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="active model deployment"):
        runtime_policy.update(local_models={"code": "blocked:latest"})

    updated = runtime_policy.update(
        local_models={"code": "allowed:latest"},
        expected_revision=initial["revision"],
        transition_token="transition-token",
    )
    assert updated["local_models"]["code"] == "allowed:latest"


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
    assert broken["local_models"]["code"] == "sonder:latest"
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
