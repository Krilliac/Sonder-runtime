"""Reference lookup: which models get automatic lookup, the policy section and the status line."""
from __future__ import annotations

import json
import threading

import pytest

from sonder_runtime.adapters import reference_lookup_service as service
from sonder_runtime.adapters import reference_lookup_state as state
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.domain.reference_lookup.applies import (
    AppliesReason,
    AppliesRule,
    decide,
)
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import SMALL_ROWS, create_index

SOURCE_TAGS = (
    "hf.co/mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF:Q3_K_M",
    "hf.co/DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF:Q8_0",
)
DEFAULT_RULE = AppliesRule.from_policy(None)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(config.ENV_INDEX, str(tmp_path / "missing" / "refs.sqlite"))
    monkeypatch.delenv(config.ENV_ENABLED, raising=False)
    monkeypatch.setattr(state, "_INDEX", None)
    monkeypatch.setattr(state, "_LAST_ERROR", "")
    return tmp_path


def _write_policy(path, section):
    base = runtime_policy.default_policy(env={})
    payload = {k: base[k] for k in ("local_models", "embedding_model", "routing")}
    payload["reference_lookup"] = section
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("name", [
    "astra-cyber-27b", "astra-cyber-9b:latest", "ASTRA-CYBER-9B", "  astra-cyber-9b  ",
    "library/astra-cyber-9b", "registry.example/ns/astra-cyber-27b:q4",
    *SOURCE_TAGS, SOURCE_TAGS[0].upper(), SOURCE_TAGS[1] + ":latest",
])
def test_defaults_match_both_spellings_prefixes_and_tags(name):
    decision = decide(name, DEFAULT_RULE)
    assert decision.applies is True
    assert decision.reason in (AppliesReason.MATCHED_NAME, AppliesReason.MATCHED_PREFIX)


@pytest.mark.parametrize("name", [
    "sonder:latest", "qwen3:4b", "phi4:latest", "astra-cyberx", "my-astra-cyber-9b", "", None,
    "hf.co/other/model:Q4", "cloud-model:480b-cloud",
])
def test_ordinary_model_names_do_not_match(name):
    assert decide(name, DEFAULT_RULE).applies is False


def test_policy_names_and_prefixes_extend_the_defaults():
    rule = AppliesRule.from_policy({"enabled": True, "names": ["Custom:7B"], "prefixes": ["lab-"]})
    assert decide("custom:7b", rule).applies is True
    assert decide("registry/custom:7b:latest", rule).applies is True
    assert decide("lab-model-3", rule).applies is True
    assert decide("astra-cyber-9b", rule).applies is True      # defaults stay
    assert decide("sonder:latest", rule).applies is False
    assert rule.extended and not DEFAULT_RULE.extended


def test_disabled_policy_and_kill_switch_turn_it_off_for_everyone():
    off = AppliesRule.from_policy({"enabled": False, "names": ["custom:7b"]})
    for name in ("astra-cyber-9b", SOURCE_TAGS[0], "custom:7b"):
        decision = decide(name, off)
        assert (decision.applies, decision.reason) == (False, AppliesReason.DISABLED_BY_POLICY)
        killed = decide(name, DEFAULT_RULE, kill_switch_off=True)
        assert (killed.applies, killed.reason) == (False, AppliesReason.DISABLED_BY_KILL_SWITCH)


def test_applies_agrees_with_the_block_list_on_what_a_name_means():
    from sonder_runtime.domain import tool_lane_models as lanes

    deny = lanes.ToolLaneDenyList.from_policy(None)
    for name in ("astra-cyber-9b:latest", "x/astra-cyber-27b", SOURCE_TAGS[0], "sonder:latest", "qwen3:4b"):
        assert decide(name, DEFAULT_RULE).applies == lanes.decide(name, deny, free_form=False).denied


# ---- runtime policy section -------------------------------------------------

def test_valid_section_round_trips_through_the_policy_file(isolated):
    path = isolated / "runtime_policy.json"
    _write_policy(path, {"enabled": True, "names": ["Custom:7B"], "prefixes": ["Lab-"]})
    loaded = runtime_policy.load(create=False)
    assert loaded["error"] == ""
    assert loaded["reference_lookup"] == {"enabled": True, "names": ["custom:7b"], "prefixes": ["lab-"]}
    assert service.applies("lab-x", loaded).applies is True
    assert service.applies("custom:7b").applies is True       # read from the file
    runtime_policy.update(source="roundtrip")                  # a guarded update carries it through
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["reference_lookup"] == loaded["reference_lookup"]


def test_default_section_is_not_written_to_disk(isolated):
    runtime_policy.update(source="defaults only")
    on_disk = json.loads((isolated / "runtime_policy.json").read_text(encoding="utf-8"))
    assert "reference_lookup" not in on_disk
    assert runtime_policy.load(create=False)["reference_lookup"] == {
        "enabled": True, "names": [], "prefixes": []}


@pytest.mark.parametrize("section, message", [
    ({"enabled": True, "bogus": 1}, "unknown reference_lookup key"),
    ({"path": "C:/index.sqlite"}, "'path' key"),
    ({"names": [], "path": ""}, "'path' key"),
    ({"enabled": "yes"}, "enabled"),
    ({"names": "astra"}, "must be a list"),
    ({"prefixes": [3]}, "invalid reference_lookup.prefixes entry"),
    ({"names": ["bad name with spaces"]}, "invalid reference_lookup.names entry"),
    ("text", "must be an object"),
])
def test_validator_rejects_bad_sections(section, message):
    with pytest.raises(ValueError, match=message):
        runtime_policy.normalize({"reference_lookup": section})


def test_invalid_policy_file_leaves_the_defaults_in_force(isolated):
    path = isolated / "runtime_policy.json"
    _write_policy(path, {"path": "C:/elsewhere.sqlite"})
    loaded = runtime_policy.load(create=False)
    assert "path" in loaded["error"]
    assert loaded["reference_lookup"] == {"enabled": True, "names": [], "prefixes": []}
    assert service.applies("astra-cyber-9b", loaded).applies is True
    assert service.applies("astra-cyber-9b").applies is True
    path.write_text("{not json", encoding="utf-8")
    assert service.applies(SOURCE_TAGS[1]).applies is True
    assert service.applies("sonder:latest").applies is False


def test_applies_follows_the_environment_kill_switch(monkeypatch):
    assert service.applies("astra-cyber-9b").applies is True
    monkeypatch.setenv(config.ENV_ENABLED, "0")
    decision = service.applies("astra-cyber-9b")
    assert (decision.applies, decision.reason) == (False, AppliesReason.DISABLED_BY_KILL_SWITCH)


def test_applies_never_raises_when_the_policy_cannot_be_loaded(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(runtime_policy, "load", boom)
    assert service.applies("astra-cyber-27b").applies is True
    assert service.applies("qwen3:4b").applies is False


# ---- the applies rule is cached by the policy file's version -------------

def _count_loads(monkeypatch):
    reads = []
    real = runtime_policy.load
    monkeypatch.setattr(runtime_policy, "load", lambda *a, **k: reads.append(1) or real(*a, **k))
    return reads


def test_policy_file_is_read_once_while_unchanged_and_again_after_it_changes(isolated, monkeypatch):
    path = isolated / "runtime_policy.json"
    _write_policy(path, {"names": ["custom:7b"]})
    reads = _count_loads(monkeypatch)
    for _ in range(25):
        assert service.applies("custom:7b").applies is True
        assert service.applies("qwen3:4b").applies is False
    assert len(reads) == 1
    _write_policy(path, {"names": ["another-lab-model"]})       # a different size
    assert service.applies("custom:7b").applies is False
    assert service.applies("another-lab-model").applies is True
    assert len(reads) == 2
    path.unlink()
    assert service.applies("another-lab-model").applies is False
    assert service.applies("another-lab-model").applies is False
    assert len(reads) == 3


def test_a_policy_at_another_path_is_never_served_from_the_cache(isolated, monkeypatch):
    first, second = isolated / "a" / "policy.json", isolated / "b" / "policy.json"
    for path, name in ((first, "custom:7b"), (second, "custom:8b")):
        path.parent.mkdir()
        _write_policy(path, {"names": [name]})
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(first))
    assert service.applies("custom:7b").applies is True
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(second))
    assert service.applies("custom:7b").applies is False
    assert service.applies("custom:8b").applies is True


def test_an_unreadable_policy_is_not_cached(isolated, monkeypatch):
    _write_policy(isolated / "runtime_policy.json", {"names": ["custom:7b"]})
    real = runtime_policy.load

    def busy(*_args, **_kwargs):
        raise OSError("policy file busy")

    monkeypatch.setattr(runtime_policy, "load", busy)
    assert service.applies("custom:7b").applies is False        # defaults while unreadable
    monkeypatch.setattr(runtime_policy, "load", real)
    assert service.applies("custom:7b").applies is True


def test_the_rule_cache_is_safe_across_threads(isolated, monkeypatch):
    _write_policy(isolated / "runtime_policy.json", {"names": ["custom:7b"]})
    reads = _count_loads(monkeypatch)
    wrong = []

    def worker():
        for _ in range(50):
            if (service.applies("custom:7b").applies, service.applies("qwen3:4b").applies) != (True, False):
                wrong.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert wrong == [] and 1 <= len(reads) <= len(threads)


def test_status_line_says_the_runtime_system_prompt_is_not_sent():
    assert "runtime system prompt not sent on lookup turns" in service.status_line()


# ---- status ----------------------------------------------------------------

def test_status_reports_path_readable_count_and_configured_names(isolated, monkeypatch):
    index = create_index(isolated / "idx" / "refs.sqlite")
    monkeypatch.setenv(config.ENV_INDEX, str(index))
    _write_policy(isolated / "runtime_policy.json",
                  {"enabled": True, "names": ["custom:7b"], "prefixes": ["lab-"]})
    status = service.status()
    assert status.readable is True and status.passages == len(SMALL_ROWS)
    assert status.index_path == str(index)
    assert "custom:7b" in status.names and "lab-" in status.prefixes and "astra-cyber-" in status.prefixes
    line = service.status_line()
    assert str(index) in line and "readable=yes" in line and f"passages={len(SMALL_ROWS)}" in line
    assert "custom:7b" in line and "astra-cyber-" in line and "2026-01-01" in line
    loaded = runtime_policy.load(create=False)
    assert "reference lookup: " in runtime_policy.format_policy(loaded)
    assert any(text.startswith("  reference lookup: ")
               for text in runtime_policy.diagnostics_lines(loaded))


def test_status_for_an_unusable_index_and_last_error():
    status = service.status()
    assert status.readable is False and status.state.value == "missing"
    assert "readable=no (missing" in service.status_line()
    assert "last error: none" in service.status_line()
    service.search("4625", 3)
    assert "missing" in service.status().last_error
    assert "last error: none" not in service.status_line()


def test_diagnostics_reports_the_reference_lookup_line(isolated):
    import server

    lines = [line for line in server.diagnostics().splitlines() if line.startswith("  reference lookup: ")]
    assert len(lines) == 1
    assert "readable=no (missing" in lines[0] and "astra-cyber-" in lines[0]
    assert any(line.startswith("  runtime policy: revision=") for line in server.diagnostics().splitlines())
