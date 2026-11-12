"""The ``tool_lane_denied_models`` runtime-policy section and the seeds that feed tiers.

Each test points ``SONDER_RUNTIME_POLICY`` at a file under ``tmp_path`` and
clears the provider and tier variables, so the result does not depend on the
machine's environment.
"""
from __future__ import annotations

import json
import logging

import pytest

from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.domain import tool_lane_models as rule
from sonder_runtime.domain.runtime_model_configuration import RuntimeModelConfiguration
from sonder_runtime.domain.runtime_policy import rules
from tests.support.tool_lane import (
    ALLOWED,
    BLOCKED,
    DENIED,
    DENIED_SMALL,
    HF_ORNITH,
    HF_QWEN,
    UNREADABLE,
    clear_environment,
    write_policy,
)

DEFAULT_PREFIXES = list(rule.DEFAULT_DENIED_PREFIXES)


@pytest.fixture(autouse=True)
def policy_file(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    return path


def load():
    return runtime_policy.load(create=False)


def deny_of(policy):
    return runtime_policy.tool_lane_deny_list(policy)


def assert_defaults_deny(deny):
    for name in (DENIED, DENIED_SMALL, "Library/Astra-Cyber-27B:latest", HF_QWEN, HF_ORNITH):
        assert rule.decide(name, deny, free_form=False).denied, name


# ---- the section: valid, absent, empty -------------------------------------

def test_valid_section_round_trips_through_the_file(policy_file):
    write_policy(policy_file, {"tool_lane_denied_models": {
        "names": ["Helper-Model:LATEST", "other-model"], "prefixes": ["Acme/"]}})
    policy = load()
    assert policy["error"] == ""
    assert policy["tool_lane_denied_models"] == {
        "names": ["helper-model", "other-model"],
        "prefixes": DEFAULT_PREFIXES + ["acme/"],
    }
    deny = deny_of(policy)
    assert deny.readable is True and deny.extended is True
    assert rule.decide("helper-model", deny, free_form=False).denied
    assert rule.decide("registry/Helper-Model:latest", deny, free_form=False).denied
    assert rule.decide("acme/anything", deny, free_form=False).denied
    assert_defaults_deny(deny)
    # A later update carries the section through and writes it back.
    updated = runtime_policy.update(routing={"chat": "general"})
    assert updated["tool_lane_denied_models"] == policy["tool_lane_denied_models"]
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert on_disk["tool_lane_denied_models"] == policy["tool_lane_denied_models"]
    assert load()["tool_lane_denied_models"] == policy["tool_lane_denied_models"]


@pytest.mark.parametrize("section", [
    "absent",
    None,
    "",
    {},
    {"names": [], "prefixes": []},
    {"names": None, "prefixes": None},
    {"names": "", "prefixes": ""},
])
def test_absent_null_or_empty_section_still_denies_the_defaults(policy_file, section):
    payload = {} if section == "absent" else {"tool_lane_denied_models": section}
    write_policy(policy_file, payload)
    policy = load()
    assert policy["error"] == "", policy["error"]
    assert policy["tool_lane_denied_models"] == {"names": [], "prefixes": DEFAULT_PREFIXES}
    deny = deny_of(policy)
    assert deny.readable is True and deny.extended is False
    assert_defaults_deny(deny)
    assert not rule.decide(ALLOWED, deny, free_form=True).denied


def test_missing_policy_file_denies_the_defaults(policy_file):
    assert not policy_file.exists()
    policy = load()
    assert policy["error"] == ""
    assert_defaults_deny(deny_of(policy))
    assert guard.denied(DENIED, "", None) is True  # the guard reads the (absent) file itself
    assert not policy_file.exists()  # load(create=False) never creates it


def test_a_default_only_section_is_not_written_to_disk(policy_file):
    runtime_policy.update(routing={"chat": "general"})
    assert "tool_lane_denied_models" not in json.loads(policy_file.read_text(encoding="utf-8"))
    assert_defaults_deny(deny_of(load()))


def test_the_section_cannot_remove_a_default(policy_file):
    write_policy(policy_file, {"tool_lane_denied_models": {
        "names": [ALLOWED], "prefixes": []}})
    deny = deny_of(load())
    assert_defaults_deny(deny)
    assert rule.decide(ALLOWED, deny, free_form=False).denied  # extension works
    # Listing a default again does not displace it or duplicate it.
    write_policy(policy_file, {"tool_lane_denied_models": {"prefixes": ["astra-cyber-"]}})
    assert load()["tool_lane_denied_models"]["prefixes"] == DEFAULT_PREFIXES


# ---- malformed section -------------------------------------------------------

@pytest.mark.parametrize("section, needle", [
    ("a string", "must be an object"),
    (["names"], "must be an object"),
    (7, "must be an object"),
    ({"names": "single"}, "names must be a list"),
    ({"prefixes": 5}, "prefixes must be a list"),
    ({"names": {"a": 1}}, "names must be a list"),
    ({"names": [5]}, "invalid tool_lane_denied_models.names entry"),
    ({"names": ["has space"]}, "invalid tool_lane_denied_models.names entry"),
    ({"prefixes": ["bad!char"]}, "invalid tool_lane_denied_models.prefixes entry"),
    ({"names": [None]}, "invalid tool_lane_denied_models.names entry"),
    ({"allow": ["astra-cyber-27b"]}, "unknown tool_lane_denied_models key"),
    ({"names": [], "prefixes": [], "remove": []}, "unknown tool_lane_denied_models key"),
])
def test_malformed_section_invalidates_the_file_and_the_defaults_still_deny(policy_file, section, needle):
    write_policy(policy_file, {
        "local_models": {"code": ALLOWED},
        "tool_lane_denied_models": section,
    })
    policy = load()
    assert needle in policy["error"], policy["error"]
    deny = deny_of(policy)
    assert deny.readable is False
    assert_defaults_deny(deny)
    # The fallback is the built-in policy, which also reverts every tier binding.
    assert policy["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]
    # Caller-named models fail closed until the file is repaired.
    assert rule.decide(ALLOWED, deny, free_form=True).status is rule.ModelLaneStatus.POLICY_UNREADABLE


@pytest.mark.parametrize("text", ["{not json", "", "[]", "null", '"text"', "\x00\x01"])
def test_unparseable_file_is_unreadable_and_the_defaults_still_deny(policy_file, text):
    write_policy(policy_file, text)
    policy = load()
    assert policy["error"]
    deny = deny_of(policy)
    assert deny.readable is False
    assert_defaults_deny(deny)


def test_a_non_dict_policy_is_unreadable_but_denies_the_defaults():
    for policy in (None, "x", [], 3):
        deny = deny_of(policy)
        assert deny.readable is False
        assert_defaults_deny(deny)


def test_update_refuses_an_invalid_file_and_reset_repairs_it(policy_file):
    write_policy(policy_file, {"tool_lane_denied_models": {"names": "single"}})
    assert load()["error"]
    with pytest.raises(ValueError, match="policy is invalid; use reset"):
        runtime_policy.update(routing={"chat": "general"})
    repaired = runtime_policy.update(reset=True)
    assert repaired["error"] == ""
    assert repaired["tool_lane_denied_models"] == {"names": [], "prefixes": DEFAULT_PREFIXES}
    assert deny_of(load()).readable is True


# ---- update(reset=True) ------------------------------------------------------

def test_reset_keeps_denying_the_defaults(policy_file):
    runtime_policy.update(routing={"chat": "fast"})
    reset = runtime_policy.update(reset=True)
    assert reset["error"] == ""
    assert reset["routing"] == runtime_policy.DEFAULT_ROUTING
    assert_defaults_deny(deny_of(reset))
    assert_defaults_deny(deny_of(load()))


def test_reset_carries_the_policy_additions_across(policy_file):
    write_policy(policy_file, {"tool_lane_denied_models": {"names": [ALLOWED], "prefixes": ["acme/"]}})
    reset = runtime_policy.update(reset=True)
    assert ALLOWED in reset["tool_lane_denied_models"]["names"]
    assert "acme/" in reset["tool_lane_denied_models"]["prefixes"]
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert ALLOWED in on_disk["tool_lane_denied_models"]["names"]
    assert_defaults_deny(deny_of(load()))


def test_no_update_parameter_edits_the_list():
    import inspect

    names = list(inspect.signature(runtime_policy.update).parameters)
    assert not [name for name in names if "deny" in name or "tool_lane" in name or "block" in name], names


# ---- a denied model is never bound ------------------------------------------

@pytest.mark.parametrize("name", [DENIED, "Astra-Cyber-9B:latest", "library/astra-cyber-27b", HF_QWEN, HF_ORNITH])
def test_a_denied_model_in_local_models_drops_only_that_tier(policy_file, name):
    write_policy(policy_file, {
        "local_models": {"code": name, "fast": ALLOWED, "general": "qwen3:8b"},
        "routing": {"chat": "fast"},
    })
    policy = load()
    assert policy["error"] == ""  # the file stays valid
    assert policy["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]
    assert name not in policy["local_models"].values()
    # The rest of the file still applies.
    assert policy["local_models"]["fast"] == ALLOWED
    assert policy["local_models"]["general"] == "qwen3:8b"
    assert policy["routing"]["chat"] == "fast"
    assert deny_of(policy).readable is True
    assert policy["tool_lane_notices"] == [rules.tool_lane_binding_notice("code", name)]
    status = runtime_policy.format_policy(policy)
    assert f"WARNING: local_models.code names {name!r}" in status
    assert "ERROR:" not in status


def test_a_denied_model_in_an_optional_tier_leaves_that_tier_unset(policy_file):
    write_policy(policy_file, {"local_models": {"reasoning": DENIED, "code": ALLOWED}})
    policy = load()
    assert policy["error"] == ""
    assert policy["local_models"]["reasoning"] == ""
    assert policy["local_models"]["code"] == ALLOWED
    assert "reasoning" in policy["tool_lane_notices"][0]
    tiers = {"reasoning": "stale"}
    guard.bind_local_tiers(tiers, policy)
    assert "reasoning" not in tiers and tiers["code"] == ALLOWED


def test_a_model_added_by_the_policy_is_dropped_from_its_tier_too(policy_file):
    write_policy(policy_file, {
        "local_models": {"code": ALLOWED},
        "tool_lane_denied_models": {"names": [ALLOWED]},
    })
    policy = load()
    assert policy["error"] == ""
    assert policy["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]
    assert rule.decide(ALLOWED, deny_of(policy), free_form=False).denied


def test_a_clean_file_has_no_notices_and_a_later_update_clears_them(policy_file):
    write_policy(policy_file, {"local_models": {"code": ALLOWED}})
    assert "tool_lane_notices" not in load()
    assert "WARNING" not in runtime_policy.format_policy(load())
    write_policy(policy_file, {"local_models": {"code": DENIED}})
    assert load()["tool_lane_notices"]
    updated = runtime_policy.update(routing={"chat": "general"})
    assert "tool_lane_notices" not in updated
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert DENIED not in json.dumps(on_disk) and "tool_lane_notices" not in on_disk
    assert "tool_lane_notices" not in load()


def test_a_malformed_deny_section_still_invalidates_a_file_that_binds_a_denied_model(policy_file):
    write_policy(policy_file, {
        "local_models": {"code": DENIED},
        "tool_lane_denied_models": {"names": "single"},
    })
    policy = load()
    assert "names must be a list" in policy["error"]
    assert deny_of(policy).readable is False
    assert policy["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]


def test_update_refuses_to_bind_a_denied_model(policy_file):
    runtime_policy.update(routing={"chat": "general"})
    for name in (DENIED, "ASTRA-CYBER-9B", HF_QWEN):
        with pytest.raises(ValueError, match="blocked from tool-using lanes"):
            runtime_policy.update(local_models={"code": name})
    assert load()["local_models"]["code"] == runtime_policy.DEFAULT_MODELS["code"]
    assert load()["error"] == ""


@pytest.mark.parametrize("variable", ["SONDER_FAST", "SONDER_CODE", "SONDER_GENERAL", "SONDER_REASONING"])
def test_a_denied_model_in_an_environment_tier_seed_is_not_bound(variable):
    tier = variable[len("SONDER_"):].lower()
    seeded = rules.default_policy({variable: DENIED})
    assert seeded["local_models"][tier] == rules.DEFAULT_MODELS[tier]
    assert DENIED not in seeded["local_models"].values()
    other = rules.default_policy({variable: ALLOWED})
    assert other["local_models"][tier] == ALLOWED  # an allowed seed is still honoured, so the filter is not blanket


def test_environment_seeds_filter_every_default_spelling():
    for name in ("Astra-Cyber-27B", " astra-cyber-9b:latest ", "library/astra-cyber-27b", HF_QWEN, HF_ORNITH):
        assert rules.seed_model({"SONDER_CODE": name}, "code") == rules.DEFAULT_MODELS["code"], name


def test_the_seed_into_a_fresh_policy_file_is_filtered(policy_file, monkeypatch):
    monkeypatch.setenv("SONDER_GENERAL", DENIED)
    created = runtime_policy.load(create=True)
    assert created["local_models"]["general"] == runtime_policy.DEFAULT_MODELS["general"]
    assert DENIED not in policy_file.read_text(encoding="utf-8")


# ---- SONDER_CODE_LOCAL -------------------------------------------------------

@pytest.mark.parametrize("value", [DENIED, "Astra-Cyber-27B:latest", " astra-cyber-9b ", "library/astra-cyber-9b", HF_ORNITH, HF_QWEN])
def test_a_denied_sonder_code_local_falls_back_to_the_stable_alias(value):
    configuration = RuntimeModelConfiguration.from_environment({"SONDER_CODE_LOCAL": value})
    assert configuration.local_code_model == configuration.stable_alias
    assert configuration.local_code_model not in (value, value.strip())


def test_an_allowed_or_unset_sonder_code_local_is_kept(caplog):
    with caplog.at_level(logging.WARNING):
        kept = RuntimeModelConfiguration.from_environment({"SONDER_CODE_LOCAL": ALLOWED})
    assert kept.local_code_model == ALLOWED
    unset = RuntimeModelConfiguration.from_environment({})
    assert unset.local_code_model == unset.stable_alias
    assert not [r for r in caplog.records if "blocked from tool-using" in r.getMessage()]


def test_the_sonder_code_local_fallback_is_logged(caplog):
    with caplog.at_level(logging.WARNING):
        RuntimeModelConfiguration.from_environment({"SONDER_CODE_LOCAL": DENIED})
    assert any("blocked from tool-using" in r.getMessage() and DENIED in r.getMessage() for r in caplog.records)


def test_the_tier_map_the_server_seeds_from_is_filtered():
    configuration = RuntimeModelConfiguration.from_environment({"SONDER_CODE": DENIED, "SONDER_FAST": ALLOWED})
    seeded = guard.seed_bindings(configuration.tier_map())
    assert DENIED not in seeded.values()
    assert seeded["fast"] == ALLOWED
    assert seeded["code"] == runtime_policy.DEFAULT_MODELS["code"]


# ---- seed_bindings / bind_local_tiers ----------------------------------------

def test_seed_bindings_replace_a_denied_model_and_drop_a_tier_without_a_default():
    seeded = guard.seed_bindings({
        "fast": "Astra-Cyber-9B", "code": ALLOWED, "hosted-x": DENIED, "general": HF_QWEN,
    })
    assert seeded == {
        "fast": runtime_policy.DEFAULT_MODELS["fast"],
        "code": ALLOWED,
        "general": runtime_policy.DEFAULT_MODELS["general"],
    }


def policy_dict(**local_models):
    base = rules.default_policy({})
    base["local_models"] = {**base["local_models"], **local_models}
    return base


def test_bind_local_tiers_copies_allowed_models_and_drops_unset_optional_tiers():
    tiers = {"reasoning": "stale", "vision": "stale"}
    guard.bind_local_tiers(tiers, policy_dict(fast=ALLOWED, code="qwen3:8b"))
    assert tiers == {"fast": ALLOWED, "code": "qwen3:8b", "general": runtime_policy.DEFAULT_MODELS["general"]}


@pytest.mark.parametrize("name", [DENIED, "Astra-Cyber-9B:latest", " library/astra-cyber-27b ", HF_QWEN, HF_ORNITH])
def test_bind_local_tiers_given_a_raw_dict_naming_a_denied_model_keeps_the_default(name):
    """A policy dict that never went through ``normalize`` is still filtered at the binding."""
    tiers = {"code": "previous-binding"}
    guard.bind_local_tiers(tiers, policy_dict(code=name, reasoning=name))
    assert tiers["code"] == runtime_policy.DEFAULT_MODELS["code"]
    assert "reasoning" not in tiers  # the optional tier's default is unbound, never the denied model
    assert name not in tiers.values()


def test_bind_local_tiers_applies_the_policy_additions_too():
    policy = policy_dict(code=ALLOWED)
    policy["tool_lane_denied_models"] = {"names": [ALLOWED], "prefixes": []}
    tiers = {}
    guard.bind_local_tiers(tiers, policy)
    assert tiers["code"] == runtime_policy.DEFAULT_MODELS["code"]


def test_bind_local_tiers_with_an_unreadable_policy_still_filters():
    policy = policy_dict(code=DENIED)
    policy["error"] = "ValueError: broken"
    tiers = {}
    guard.bind_local_tiers(tiers, policy)
    assert tiers["code"] == runtime_policy.DEFAULT_MODELS["code"]


# ---- unreadable policy: tool lanes refuse a named model, text does not ------

def test_unreadable_file_refuses_a_caller_named_model_and_names_the_fix(policy_file):
    write_policy(policy_file, "{not json")
    text = guard.refusal(ALLOWED)
    assert text == UNREADABLE % ALLOWED
    assert "repair the file" in text and "/runtime status" in text and "/runtime reset" in text
    assert guard.denied(ALLOWED, "model:" + ALLOWED) is True
    assert guard.resolved_refusal(ALLOWED, None, guard.DENIED_LABEL) == UNREADABLE % ALLOWED
    assert guard.selector_refusal(ALLOWED, {"code": ALLOWED}) == UNREADABLE % ALLOWED
    assert guard.selector_refusal(f"code,{ALLOWED}", {"code": ALLOWED}) == UNREADABLE % ALLOWED
    # A tier default and the default route are not caller-named.
    assert guard.denied(ALLOWED, "code") is False
    assert guard.selector_refusal("code", {"code": ALLOWED}) == ""
    assert guard.selector_refusal("", {"code": ALLOWED}) == ""
    # A denied name is still reported as blocked, not as an unreadable file.
    assert guard.refusal(DENIED) == BLOCKED % DENIED


def test_repairing_the_file_lifts_the_refusal(policy_file):
    write_policy(policy_file, "{not json")
    assert guard.refusal(ALLOWED) == UNREADABLE % ALLOWED
    write_policy(policy_file, {})
    assert guard.denied(ALLOWED, "model:" + ALLOWED) is False
    assert guard.selector_refusal(ALLOWED, {}) == ""


def test_a_malformed_section_is_refused_the_same_way(policy_file):
    write_policy(policy_file, {"tool_lane_denied_models": {"names": "single"}})
    assert guard.refusal(ALLOWED) == UNREADABLE % ALLOWED
    assert guard.refusal(DENIED) == BLOCKED % DENIED


def test_a_policy_load_failure_fails_closed_and_logs_once(monkeypatch, caplog):
    def broken(create=False):
        raise OSError("disk gone")

    monkeypatch.setattr(guard._policy, "load", broken)
    monkeypatch.setattr(guard, "_warned", set())
    with caplog.at_level(logging.WARNING, logger=guard.__name__):
        first = guard.deny_list(None)
        guard.deny_list(None)
        assert guard.refusal(ALLOWED) == UNREADABLE % ALLOWED
    assert first.readable is False
    assert_defaults_deny(first)
    hits = [r for r in caplog.records if r.name == guard.__name__ and r.levelno == logging.WARNING]
    assert len(hits) == 1, hits


def test_status_line_reports_the_source_and_an_unreadable_file(policy_file):
    defaults_only = runtime_policy.format_tool_lane_denied_models(load())
    assert "defaults only" in defaults_only and "astra-cyber-" in defaults_only
    assert "unreadable" not in defaults_only
    write_policy(policy_file, {"tool_lane_denied_models": {"names": ["helper-x"]}})
    extended = runtime_policy.format_tool_lane_denied_models(load())
    assert "defaults plus policy file" in extended and "helper-x" in extended and "astra-cyber-" in extended
    write_policy(policy_file, "{not json")
    broken = runtime_policy.format_tool_lane_denied_models(load())
    assert "policy file unreadable" in broken and "astra-cyber-" in broken
    assert "tool-lane blocked models" in runtime_policy.format_policy(load())
