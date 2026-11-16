"""Restricted model profiles: the safety predicate, the decision, the ceiling and the secret globs.

Phase 1b lets the operator overlay give a model a short list of read-only
code tools in the ``agent`` / ``workbench_agent`` lanes.  These tests pin the
rule that decides which tools survive (code, not a list), the decision that
turns profiles into ALLOWED / RESTRICTED / DENIED, and that today's behaviour
is unchanged for every model with no tools granted.
"""
from __future__ import annotations

import json

import pytest

import permission_modes
import server  # noqa: F401 - the host tool classifier reads sys.modules["server"]
from sonder_runtime.adapters import model_tool_grants as grants
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import restricted_tool_lane as lane
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.domain import model_tool_grants as rules
from sonder_runtime.domain.model_tool_grants import ModelToolStatus, ToolFacts
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus, ToolLaneDenyList
from tests.support.tool_lane import ALLOWED, DENIED, DENIED_SMALL, HF_ORNITH, clear_environment

SHIPPED_CEILING = ["text_search", "repository_symbol_index", "file_read_range", "directory_tree", "context_pack"]
# The task's suggestion list; find_references fails the predicate (see below).
SUGGESTED_CEILING = SHIPPED_CEILING[:2] + ["find_references"] + SHIPPED_CEILING[2:]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    mtp.clear_cache()
    yield
    mtp.clear_cache()


@pytest.fixture
def overlay_file(tmp_path):
    return tmp_path / "overlay" / "model_tool_policy.json"


def write_overlay(path, profiles=(), **extra):
    path.write_text(json.dumps({"version": 1, "model_profiles": list(profiles), **extra}), encoding="utf-8")


def grant_advisers(path, tools, lanes=("agent",)):
    write_overlay(path, [{"id": "security-advisers", "tools": list(tools), "lanes": list(lanes)}])


def facts(name):
    classify, why = grants.host_classifier()
    assert classify is not None, why
    return classify(name)


def tool_decision(model, policy=None):
    return lane.decide(model, policy if policy is not None else runtime_policy.load(create=False))


# ---- the safety predicate -------------------------------------------------------

def test_every_shipped_ceiling_tool_passes_the_predicate():
    for tool in SHIPPED_CEILING:
        assert rules.restricted_tool_refusal(facts(tool)) == "", tool


def test_the_shipped_ceiling_is_the_suggestion_minus_what_fails_the_predicate():
    document = json.loads(mtp.SHIPPED_PATH.read_text(encoding="utf-8"))
    assert document["restricted_tool_ceiling"] == SHIPPED_CEILING
    dropped = [tool for tool in SUGGESTED_CEILING if rules.restricted_tool_refusal(facts(tool))]
    assert dropped == ["find_references"]
    # find_references is graded 'ask' and has no read-only dispatch in the agent lane.
    assert permission_modes.risk_of("find_references") != "safe"


@pytest.mark.parametrize("tool, reason", [
    ("web_fetch", "network"),
    ("web_search", "network"),
    ("weather_lookup", "network"),
    ("approximate_location_lookup", "network"),
    ("memory_search", "memory"),
    ("run_code", "risk grade"),
    ("file_write", "risk grade"),
    ("file_delete", "risk grade"),
    ("workspace_run", "risk grade"),
    ("runtime_policy_update", "risk grade"),
    ("diagnostics", "dispatch"),       # safe and dispatchable, but not project-confined
    ("system_profile_text", "dispatch"),
    ("tool_help", "dispatch"),
])
def test_dangerous_or_unconfined_tools_fail_the_predicate(tool, reason):
    refusal = rules.restricted_tool_refusal(facts(tool))
    assert refusal and reason in refusal, (tool, refusal)


@pytest.mark.parametrize("name, field", [
    ("web_anything", None), ("weather_now", None), ("location_guess", None),
    ("memory_peek", None), ("learn_this", None), ("remember_x", None), ("forget_x", None),
    ("record_outcome", None), ("apply_learned", None), ("lesson_list", None),
    ("run_thing", None), ("thing_run", None), ("build_thing", None), ("admin_peek", None),
    ("safe_tool", "operator"), ("safe_tool", "execution"),
])
def test_the_name_rules_hold_even_for_a_safe_dispatchable_tool(name, field):
    fields = {"operator": False, "execution": False}
    if field:
        fields[field] = True
    assert rules.restricted_tool_refusal(ToolFacts(name, "safe", dispatchable=True, **fields))


def test_a_safe_project_read_passes_only_with_every_fact():
    assert rules.restricted_tool_refusal(ToolFacts("text_search", "safe", False, False, True)) == ""
    assert rules.restricted_tool_refusal(ToolFacts("text_search", "ask", False, False, True))
    assert rules.restricted_tool_refusal(ToolFacts("text_search", "safe", False, False, False))


def test_without_the_host_no_tool_is_granted(monkeypatch, overlay_file):
    grant_advisers(overlay_file, ["text_search"])
    monkeypatch.setattr(grants, "_host_sets", lambda host: (_ for _ in ()).throw(AttributeError("gone")))
    result = grants.grant_set()
    assert result.grants == ()
    assert result.dropped and "classification is unavailable" in result.dropped[0].reason
    assert tool_decision(DENIED).status is ModelToolStatus.DENIED


# ---- the decision ---------------------------------------------------------------

def test_no_tools_granted_is_exactly_todays_denial():
    for model in (DENIED, DENIED_SMALL, HF_ORNITH):
        decision = tool_decision(model)
        assert decision.status is ModelToolStatus.DENIED and not decision.permits("agent")
        assert guard.refusal(model) == guard._models.refusal_message(model, ModelLaneStatus.DENIED)
    assert tool_decision(ALLOWED).status is ModelToolStatus.ALLOWED


def test_an_empty_tools_list_on_a_restricted_profile_is_denied(overlay_file):
    grant_advisers(overlay_file, [], lanes=["agent"])
    assert tool_decision(DENIED).status is ModelToolStatus.DENIED


def test_a_floor_profile_with_overlay_tools_is_restricted(overlay_file):
    grant_advisers(overlay_file, ["text_search", "file_read_range"], lanes=["agent"])
    decision = tool_decision(DENIED)
    assert decision.status is ModelToolStatus.RESTRICTED
    assert decision.tools == {"text_search", "file_read_range"}
    assert decision.lanes == {"agent"}
    assert decision.permits("agent") and not decision.permits("workbench_agent")
    assert not decision.permits("codegen_build_loop")
    # still on the deny list: every other lane keeps refusing
    assert guard.denied(DENIED) is True
    assert guard.refusal(DENIED) == guard._models.refusal_message(DENIED, ModelLaneStatus.DENIED)


@pytest.mark.parametrize("tool", ["web_fetch", "run_code", "file_write", "memory_search", "workspace_run", "tool_help"])
def test_an_overlay_grant_of_a_dangerous_tool_is_dropped_with_an_error(overlay_file, tool):
    grant_advisers(overlay_file, [tool], lanes=["agent"])
    config = mtp.load()
    assert config.readable_overlay  # the overlay itself is valid
    result = grants.grant_set(config)
    assert result.grants == ()
    assert [(item.profile_id, item.tool) for item in result.dropped] == [("security-advisers", tool)]
    assert tool_decision(DENIED).status is ModelToolStatus.DENIED
    status = runtime_policy.format_policy(runtime_policy.load(create=False))
    assert "DROPPED: profile 'security-advisers': tool %r (overlay) is not granted" % tool in status
    payload = grants.status_payload(config)
    assert payload["dropped_tools"][0]["tool"] == tool and payload["profiles"][0]["status"] == "denied"


def test_a_dangerous_tool_beside_a_safe_one_is_dropped_and_the_safe_one_kept(overlay_file):
    grant_advisers(overlay_file, ["text_search", "web_fetch", "file_write"], lanes=["agent"])
    decision = tool_decision(DENIED)
    assert decision.status is ModelToolStatus.RESTRICTED and decision.tools == {"text_search"}


def test_a_tool_outside_the_ceiling_is_dropped(overlay_file):
    grant_advisers(overlay_file, ["file_read"], lanes=["agent"])  # safe, but not in the shipped ceiling
    result = grants.grant_set()
    assert result.grants == () and "restricted_tool_ceiling" in result.dropped[0].reason


def test_an_unknown_lane_is_ignored_with_an_error(overlay_file):
    grant_advisers(overlay_file, ["text_search"], lanes=["codegen_build_loop"])
    result = grants.grant_set()
    assert any("not a restricted lane" in error for error in result.errors)
    decision = tool_decision(DENIED)
    assert decision.status is ModelToolStatus.RESTRICTED and decision.lanes == frozenset()
    assert not decision.permits("agent") and not decision.permits("codegen_build_loop")


def test_an_explicit_deny_entry_beats_a_grant(overlay_file):
    write_overlay(overlay_file, [
        {"id": "security-advisers", "tools": ["text_search"], "lanes": ["agent"]},
        {"id": "deny-27b", "match": {"names": [DENIED]}, "tools": [], "lanes": []},
    ])
    assert tool_decision(DENIED).status is ModelToolStatus.DENIED
    assert tool_decision(DENIED_SMALL).status is ModelToolStatus.RESTRICTED


def test_the_legacy_section_denies_outright(overlay_file, tmp_path):
    (tmp_path / "runtime_policy.json").write_text(
        json.dumps({"tool_lane_denied_models": {"names": [DENIED], "prefixes": []}}), encoding="utf-8",
    )
    grant_advisers(overlay_file, ["text_search"])
    assert tool_decision(DENIED).status is ModelToolStatus.DENIED
    assert tool_decision(DENIED_SMALL).status is ModelToolStatus.RESTRICTED


def test_two_matching_grants_intersect(overlay_file):
    write_overlay(overlay_file, [
        {"id": "security-advisers", "tools": ["text_search", "directory_tree"], "lanes": ["agent", "workbench_agent"]},
        {"id": "only-27b", "match": {"names": [DENIED]}, "tools": ["text_search"], "lanes": ["agent"]},
    ])
    narrow = tool_decision(DENIED)
    assert narrow.tools == {"text_search"} and narrow.lanes == {"agent"}
    wide = tool_decision(DENIED_SMALL)
    assert wide.tools == {"text_search", "directory_tree"} and wide.lanes == {"agent", "workbench_agent"}


def test_an_unreadable_policy_file_withholds_the_grant(overlay_file, tmp_path):
    (tmp_path / "runtime_policy.json").write_text("{broken", encoding="utf-8")
    grant_advisers(overlay_file, ["text_search"])
    assert tool_decision(DENIED).status is ModelToolStatus.POLICY_UNREADABLE


def test_an_invalid_overlay_grants_nothing(overlay_file):
    overlay_file.write_text("{broken", encoding="utf-8")
    decision = tool_decision(DENIED)
    assert decision.status is ModelToolStatus.OVERLAY_UNUSABLE
    assert not decision.tools


def test_a_missing_shipped_file_grants_nothing(monkeypatch, tmp_path, overlay_file):
    grant_advisers(overlay_file, ["text_search"])
    monkeypatch.setattr(mtp, "SHIPPED_PATH", tmp_path / "gone.json")
    mtp.clear_cache()
    assert tool_decision(DENIED).status is ModelToolStatus.CONFIG_UNAVAILABLE


def test_a_restricted_model_never_serves_a_tier(overlay_file):
    grant_advisers(overlay_file, ["text_search"])
    deny = guard.deny_list(runtime_policy.load(create=False))
    assert rules.decide_tools_served(DENIED, deny).status is ModelToolStatus.DENIED
    assert rules.decide_tools_served("default", deny).status is ModelToolStatus.SERVED_UNKNOWN


def test_the_domain_decision_is_pure():
    floor = mtp.shipped_floor()
    deny = ToolLaneDenyList.from_policy(None, floor=floor)
    grant = rules.ToolGrant("security-advisers", frozenset(), floor.prefixes, frozenset({"text_search"}), frozenset({"agent"}))
    nothing = ToolLaneDenyList(names=frozenset(), prefixes=(), floor=floor)
    assert rules.decide_tools(DENIED, deny, [grant], nothing, free_form=True).status is ModelToolStatus.RESTRICTED
    assert rules.decide_tools(ALLOWED, deny, [grant], nothing, free_form=True).status is ModelToolStatus.ALLOWED
    blocking = ToolLaneDenyList(names=frozenset({DENIED}), prefixes=(), floor=floor)
    assert rules.decide_tools(DENIED, deny, [grant], blocking, free_form=True).status is ModelToolStatus.DENIED
    empty = rules.ToolGrant("security-advisers", frozenset(), floor.prefixes, frozenset(), frozenset({"agent"}))
    assert rules.decide_tools(DENIED, deny, [empty], nothing, free_form=True).status is ModelToolStatus.DENIED


# ---- floors ---------------------------------------------------------------------

def test_the_overlay_cannot_narrow_a_floor_match(overlay_file):
    write_overlay(overlay_file, [{
        "id": "security-advisers", "match": {"names": [], "prefixes": ["astra-cyber-27b"]},
        "tools": ["text_search"], "lanes": ["agent"],
    }])
    config = mtp.load()
    advisers = config.profile("security-advisers")
    assert advisers.floor
    assert set(mtp.shipped_floor().prefixes) <= set(advisers.values("prefixes"))
    for model in (DENIED_SMALL, HF_ORNITH):
        assert guard.denied(model) is True  # the floor still lists them for every other lane


def test_the_overlay_cannot_set_the_ceiling(overlay_file):
    write_overlay(overlay_file, [], restricted_tool_ceiling=["file_write"])
    config = mtp.load()
    assert not config.readable_overlay and "only the shipped file sets" in " ".join(config.errors)
    assert config.tool_ceiling == tuple(SHIPPED_CEILING)


def test_the_overlay_may_add_secret_globs_but_not_remove_them(overlay_file):
    write_overlay(overlay_file, [], restricted_secret_path_globs=["*.kdbx"])
    config = mtp.load()
    assert "*.kdbx" in config.secret_globs and ".env" in config.secret_globs
    assert config.secret_path_globs[-1].source is mtp.ConfigSource.OVERLAY


@pytest.mark.parametrize("glob", ["/etc/passwd", "../x", "C:\\x", ""])
def test_a_bad_secret_glob_makes_the_overlay_invalid(overlay_file, glob):
    write_overlay(overlay_file, [], restricted_secret_path_globs=[glob])
    assert not mtp.load().readable_overlay


# ---- the secret globs -------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".env", "app/.env", ".env.local", "deploy/server.pem", "keys/api.key", "cert.pfx",
    "home/.ssh/id_rsa", ".ssh/config", "id_ed25519.pub", "config/secrets/db.yaml", "secrets/x",
    "aws_credentials.json", "github_token.txt", ".git/config", ".netrc", "SERVER.PEM", "a\\b\\.env",
])
def test_secret_paths_match_a_shipped_glob(path):
    assert rules.restricted_glob_match(path, mtp.load().secret_globs), path


@pytest.mark.parametrize("path", ["src/app.py", "README.md", "docs/environment.md", ".", "", "envoy/main.go"])
def test_ordinary_paths_match_no_shipped_glob(path):
    assert rules.restricted_glob_match(path, mtp.load().secret_globs) == "", path
