"""The model tool policy files: shipped defaults, operator overlay, floors and fail-closed rules.

Phase 1a moved the tool-lane deny list out of Python into
``sonder_runtime/adapters/data/model_tool_policy.json`` plus an optional
operator overlay (``<state home>/model_tool_policy.json``, or the path in
``SONDER_MODEL_TOOL_POLICY``).  These tests pin the shipped content, the
packaging, the merge rules and the failure rules.
"""
from __future__ import annotations
from sonder_runtime.adapters import model_lists as _test_model_lists

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.domain import tool_lane_models as rule
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus
from sonder_runtime.platform import paths as runtime_paths
from tests.support.tool_lane import (
    ALLOWED,
    DENIED,
    DENIED_SMALL,
    HF_ORNITH,
    HF_QWEN,
    clear_environment,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The security-advisers floor, written out literally on purpose: this is the
# pin.  It equals what the removed constants DEFAULT_DENIED_PREFIXES derived.
EXPECTED_FLOOR_PREFIXES = [
    "astra-cyber-",
    "hf.co/mradermacher/qwen3.8-cyber-redteam-surgical-abliterated-gguf",
    "huggingface.co/mradermacher/qwen3.8-cyber-redteam-surgical-abliterated-gguf",
    "mradermacher/qwen3.8-cyber-redteam-surgical-abliterated-gguf",
    "hf.co/duoneural/cyber-ornith-1.5-9b-obliterated-gguf",
    "huggingface.co/duoneural/cyber-ornith-1.5-9b-obliterated-gguf",
    "duoneural/cyber-ornith-1.5-9b-obliterated-gguf",
    "qwen3.8-cyber-redteam-surgical-abliterated",
    "cyber-ornith-1.5-9b-obliterated",
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    mtp.clear_cache()
    yield
    mtp.clear_cache()


@pytest.fixture
def overlay_file(tmp_path):
    return tmp_path / "overlay" / "model_tool_policy.json"


def write_overlay(path, profiles=None, **extra):
    document = {"version": 1, "model_profiles": profiles or [], **extra}
    path.write_text(json.dumps(document), encoding="utf-8")


def profile(profile_id, names=(), prefixes=(), **fields):
    return {"id": profile_id, "match": {"names": list(names), "prefixes": list(prefixes)}, **fields}


def deny():
    return runtime_policy.tool_lane_deny_list(runtime_policy.load(create=False))


def decide(name, *, free_form=False):
    return rule.decide(name, deny(), free_form=free_form)


@pytest.fixture
def missing_shipped(monkeypatch, tmp_path):
    path = tmp_path / "gone" / "model_tool_policy.json"
    monkeypatch.setattr(mtp, "SHIPPED_PATH", path)
    mtp.clear_cache()
    return path


# ---- the shipped file ----------------------------------------------------------

def test_shipped_file_content_is_pinned():
    document = json.loads(mtp.SHIPPED_PATH.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["undetermined_served_models"] == ["", "default"]
    assert len(document["model_profiles"]) == 1
    advisers = document["model_profiles"][0]
    assert advisers["id"] == "security-advisers"
    assert advisers["floor"] is True
    assert advisers["tools"] == [] and advisers["lanes"] == []
    assert advisers["match"]["names"] == []
    assert advisers["match"]["prefixes"] == EXPECTED_FLOOR_PREFIXES


def test_loaded_floor_equals_the_pinned_set():
    config = mtp.load()
    assert config.readable_shipped and not config.issues
    assert config.floor.available
    assert config.floor.prefixes == tuple(EXPECTED_FLOOR_PREFIXES)
    assert config.floor.names == frozenset()
    assert config.floor.undetermined_served == frozenset({"", "default"})
    assert len(set(config.floor.prefixes)) == len(config.floor.prefixes)


def test_domain_module_holds_no_model_names():
    for constant in ("DEFAULT_DENIED_PREFIXES", "DEFAULT_DENIED_NAMES", "UNDETERMINED_SERVED_MODELS", "_DENIED_SOURCES"):
        assert not hasattr(rule, constant), constant
    source = Path(rule.__file__).read_text(encoding="utf-8")
    assert "astra-cyber" not in source.split('"""', 2)[2]  # the code after the module docstring


def test_shipped_file_sits_with_the_other_package_data():
    data = Path(mtp.__file__).resolve().parent / "data"
    assert mtp.SHIPPED_PATH == data / "model_tool_policy.json"
    assert mtp.SHIPPED_PATH.is_file()
    assert (data / "semantic_tier_head.json").is_file()  # the file whose packaging this mirrors


def test_shipped_file_is_tracked_and_not_ignored_by_git():
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    relative = "sonder_runtime/adapters/data/model_tool_policy.json"
    tracked = subprocess.run(
        [git, "ls-files", "--error-unmatch", relative], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    ignored = subprocess.run(
        [git, "check-ignore", "-q", relative], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if tracked.returncode != 0:  # new file, not yet added: it must at least not be ignored
        assert ignored.returncode == 1, "model_tool_policy.json is git-ignored, so it would not ship"
    else:
        assert ignored.returncode == 1


# ---- shipped file missing or invalid: every tool lane refused --------------------

def assert_every_lane_refused(missing_path):
    verdict = decide(ALLOWED)
    assert verdict.status is ModelLaneStatus.CONFIG_UNAVAILABLE and verdict.denied
    assert decide("", free_form=False).status is ModelLaneStatus.CONFIG_UNAVAILABLE
    assert decide(DENIED, free_form=True).status is ModelLaneStatus.CONFIG_UNAVAILABLE
    assert rule.decide_selector("sonder", {}, deny()).status is ModelLaneStatus.CONFIG_UNAVAILABLE
    assert rule.decide_served("some-model", deny()).status is ModelLaneStatus.CONFIG_UNAVAILABLE
    assert str(missing_path) in verdict.matched
    message = rule.refusal_message(verdict.model, verdict.status, verdict.matched)
    assert "shipped model tool policy is unavailable" in message and str(missing_path) in message
    assert guard.denied(ALLOWED) is True
    assert guard.refusal(ALLOWED) == rule.refusal_message(ALLOWED, verdict.status, verdict.matched)
    with pytest.raises(Exception, match="shipped model tool policy is unavailable") as caught:
        guard.require_generation(ALLOWED)
    assert getattr(caught.value, "tool_lane_refused", False)
    status = runtime_policy.format_policy(runtime_policy.load(create=False))
    assert str(missing_path) in status and "every tool-using lane" in status


def test_missing_shipped_file_refuses_every_tool_lane(missing_shipped):
    assert_every_lane_refused(missing_shipped)


def test_missing_shipped_file_still_allows_text_only_chat(missing_shipped):
    guard.require_generation(ALLOWED, text_only=True)  # a reply that is only shown is not a tool lane


def test_missing_shipped_file_does_not_rewrite_tier_bindings(missing_shipped):
    tiers = {"code": "qwen3.5:4b", "fast": "qwen3.5:4b"}
    assert guard.seed_bindings(tiers) == tiers
    policy = runtime_policy.load(create=False)
    bound = {}
    guard.bind_local_tiers(bound, policy)
    assert bound["code"] == policy["local_models"]["code"]


@pytest.mark.parametrize("model", [DENIED, ALLOWED])
def test_missing_shipped_file_refuses_writing_a_tier_binding(missing_shipped, tmp_path, model):
    """Without the floor no binding can be judged, so none is written (not even an ordinary one)."""
    policy_file = tmp_path / "runtime_policy.json"
    runtime_policy.load(create=True)
    before = policy_file.read_bytes()
    with pytest.raises(ValueError, match="shipped model tool policy is unavailable"):
        runtime_policy.update(local_models={"general": model})
    assert policy_file.read_bytes() == before


def test_the_same_binding_write_succeeds_with_the_shipped_file():
    runtime_policy.update(local_models={"general": ALLOWED})
    assert runtime_policy.load(create=False)["local_models"]["general"] == ALLOWED


def test_missing_shipped_file_seeds_no_tier_from_the_environment(missing_shipped):
    from sonder_runtime.domain.runtime_policy import rules as policy_rules

    floor = mtp.shipped_floor()
    assert not floor.available
    for model in (DENIED, ALLOWED):
        seeded = policy_rules.seed_model(
            {"SONDER_CODE": model},
            "code",
            floor=floor,
            lists=_test_model_lists.load().values,
        )
        assert (
            seeded
            == _test_model_lists.load().document()["default_local_models"]["code"]
        ), model


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"version": 2, "model_profiles": []}),
    json.dumps({"version": 1, "model_profiles": []}),  # no floor profile: would silently drop protection
    json.dumps({"version": 1, "model_profiles": [profile("p", prefixes=["x-"])]}),  # present but not a floor
    json.dumps({"version": 1, "model_profiles": [profile("Bad Id", prefixes=["x-"], floor=True)]}),
    json.dumps({"version": 1, "model_profiles": [profile("p", prefixes=[""], floor=True)]}),
    json.dumps({"version": 1, "model_profiles": [profile("p", prefixes=["x-"], floor=True)], "extra": 1}),
])
def test_invalid_shipped_file_refuses_every_tool_lane(monkeypatch, tmp_path, content):
    path = tmp_path / "shipped.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(mtp, "SHIPPED_PATH", path)
    mtp.clear_cache()
    config = mtp.load()
    assert not config.readable_shipped and not config.floor.available
    assert_every_lane_refused(path)


def test_shipped_failure_is_not_cached(missing_shipped, monkeypatch):
    assert not mtp.load().readable_shipped
    shutil.copy(REPO_ROOT / "sonder_runtime" / "adapters" / "data" / "model_tool_policy.json", _prepare(missing_shipped))
    assert mtp.load().readable_shipped
    assert decide(DENIED).status is ModelLaneStatus.DENIED


def _prepare(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---- the overlay: floors, additions ------------------------------------------------

def test_no_overlay_file_is_normal(overlay_file):
    assert not overlay_file.exists()
    config = mtp.load()
    assert config.readable_overlay and not config.overlay_present and not config.issues
    assert decide(DENIED).status is ModelLaneStatus.DENIED
    assert decide(ALLOWED).status is ModelLaneStatus.ALLOWED


def test_overlay_that_omits_the_floor_profile_leaves_the_floor(overlay_file):
    write_overlay(overlay_file, [profile("mine", names=["helper-model"])])
    for name in (DENIED, DENIED_SMALL, HF_QWEN, HF_ORNITH):
        assert decide(name).status is ModelLaneStatus.DENIED, name


def test_overlay_that_narrows_the_floor_profile_leaves_the_floor(overlay_file):
    write_overlay(overlay_file, [profile("security-advisers", prefixes=["astra-cyber-27b"])])
    config = mtp.load()
    assert config.readable_overlay
    for name in (DENIED, DENIED_SMALL, HF_QWEN, HF_ORNITH, "huggingface.co/DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF"):
        assert decide(name).status is ModelLaneStatus.DENIED, name
    assert deny().floor.prefixes == tuple(EXPECTED_FLOOR_PREFIXES)


def test_overlay_that_empties_everything_leaves_the_floor(overlay_file):
    write_overlay(overlay_file, [], undetermined_served_models=[])
    assert decide(DENIED_SMALL).status is ModelLaneStatus.DENIED
    assert rule.decide_served("default", deny()).status is ModelLaneStatus.SERVED_UNKNOWN


def test_overlay_cannot_claim_a_floor_but_may_add_tools_to_one(overlay_file):
    write_overlay(overlay_file, [profile("mine", names=["helper-model"], floor=True)])
    config = mtp.load()
    assert not config.readable_overlay and "only the shipped file" in config.errors[0]
    # Phase 1b: the overlay may add tools and lanes to a floor profile; the
    # floor's match is untouched and every model it matches stays on the deny list.
    write_overlay(overlay_file, [profile("security-advisers", prefixes=["x-"], tools=["text_search"], lanes=["agent"])])
    config = mtp.load()
    assert config.readable_overlay and not config.issues
    advisers = config.profile("security-advisers")
    assert advisers.floor and advisers.values("tools") == ("text_search",)
    assert advisers.values("prefixes")[: len(EXPECTED_FLOOR_PREFIXES)] == tuple(EXPECTED_FLOOR_PREFIXES)
    assert advisers.source_of("tools", "text_search") is mtp.ConfigSource.OVERLAY
    assert decide(DENIED).status is ModelLaneStatus.DENIED
    assert decide("x-model").status is ModelLaneStatus.DENIED


def test_overlay_adding_a_name_and_a_prefix_denies_them(overlay_file):
    write_overlay(overlay_file, [
        profile("mine", names=["Helper-Model:LATEST"], prefixes=["lab-"]),
        profile("more", names=["other:7b"], tools=["text_search"]),  # tools are not enforced in 1a: still denied
    ])
    assert decide("helper-model").status is ModelLaneStatus.DENIED
    assert decide("lab-x:7b").status is ModelLaneStatus.DENIED
    assert decide("other:7b").status is ModelLaneStatus.DENIED
    assert decide(ALLOWED).status is ModelLaneStatus.ALLOWED
    assert deny().extended
    assert "defaults plus policy file" in runtime_policy.format_tool_lane_denied_models(runtime_policy.load(create=False))


def test_overlay_unions_with_the_legacy_policy_section(overlay_file, tmp_path):
    (tmp_path / "runtime_policy.json").write_text(
        json.dumps({"tool_lane_denied_models": {"names": ["legacy-model"], "prefixes": []}}), encoding="utf-8",
    )
    write_overlay(overlay_file, [profile("mine", names=["overlay-model"])])
    for name in ("legacy-model", "overlay-model", DENIED):
        assert decide(name).status is ModelLaneStatus.DENIED, name


def test_overlay_added_undetermined_served_ids_are_unioned(overlay_file):
    write_overlay(overlay_file, [], undetermined_served_models=["auto"])
    assert rule.decide_served("auto", deny()).status is ModelLaneStatus.SERVED_UNKNOWN
    assert rule.decide_served("default", deny()).status is ModelLaneStatus.SERVED_UNKNOWN


def test_overlay_change_is_picked_up_without_a_restart(overlay_file):
    assert decide("late-model").status is ModelLaneStatus.ALLOWED
    write_overlay(overlay_file, [profile("mine", names=["late-model"])])
    assert decide("late-model").status is ModelLaneStatus.DENIED
    write_overlay(overlay_file, [profile("mine", names=["later-model-name"])])
    assert decide("late-model").status is ModelLaneStatus.ALLOWED
    assert decide("later-model-name").status is ModelLaneStatus.DENIED


def test_overlay_path_follows_the_environment_then_the_state_home(monkeypatch, tmp_path):
    assert mtp.overlay_path() == tmp_path / "overlay" / "model_tool_policy.json"
    monkeypatch.delenv(mtp.OVERLAY_ENV)
    assert mtp.overlay_path() == Path(runtime_paths.state_path("model_tool_policy.json"))
    assert mtp.overlay_path().name == "model_tool_policy.json"


# ---- invalid overlay: every model refused in tool lanes, never rewritten ----------

INVALID_OVERLAYS = [
    "{not json",
    "[]",
    json.dumps({"version": 9, "model_profiles": []}),
    json.dumps({"version": 1, "model_profiles": [profile("mine", names=["bad name!"])]}),
    json.dumps({"version": 1, "model_profiles": [{"id": "mine"}]}),
    json.dumps({"version": 1, "model_profiles": "nope"}),
    json.dumps({"version": 1, "model_profiles": [], "surprise": True}),
    json.dumps({"version": 1, "model_profiles": [profile("a", names=["x"]), profile("a", names=["y"])]}),
]


@pytest.mark.parametrize("content", INVALID_OVERLAYS)
def test_invalid_overlay_refuses_every_model_and_is_never_rewritten(overlay_file, content):
    overlay_file.write_text(content, encoding="utf-8")
    before = (overlay_file.read_bytes(), overlay_file.stat().st_mtime_ns)
    config = mtp.load()
    assert config.readable_shipped and not config.readable_overlay and config.issues
    # every model is refused, tier-bound or caller-named, listed or not
    for name in (DENIED, HF_QWEN, ALLOWED):
        for free_form in (False, True):
            assert decide(name, free_form=free_form).status is ModelLaneStatus.OVERLAY_UNUSABLE, name
    verdict = decide(ALLOWED)
    assert str(overlay_file) in verdict.matched
    assert guard.refusal(ALLOWED) == rule.refusal_message(ALLOWED, verdict.status, verdict.matched)
    assert str(overlay_file) in guard.refusal(ALLOWED)
    # the error is visible in the status text
    status = runtime_policy.format_policy(runtime_policy.load(create=False))
    assert str(overlay_file) in status and "ERROR" in status and "overlay" in status
    assert "every tool-using lane refuses every model" in status
    # nothing rewrote the file
    runtime_policy.load(create=True)
    mtp.load()
    assert (overlay_file.read_bytes(), overlay_file.stat().st_mtime_ns) == before


@pytest.mark.parametrize("content", INVALID_OVERLAYS[:2])
def test_an_unusable_overlay_refuses_a_model_it_restricted_however_it_is_reached(overlay_file, content):
    """A model the overlay alone denies does not come back through a tier, the default route or a provider."""
    write_overlay(overlay_file, [profile("mine", names=["helper-model"])])
    bindings = {"general": "helper-model", "code": ALLOWED}
    assert decide("helper-model").status is ModelLaneStatus.DENIED
    overlay_file.write_text(content, encoding="utf-8")
    # tier-bound
    with pytest.raises(Exception, match="overlay") as caught:
        guard.require_generation("helper-model")
    assert getattr(caught.value, "tool_lane_refused", False)
    assert guard.resolved_refusal("general", "helper-model", "general")
    assert guard.denied("helper-model", "general") is True
    # the default route and a by-tier selector
    assert rule.decide_selector("sonder", bindings, deny()).status is ModelLaneStatus.OVERLAY_UNUSABLE
    assert rule.decide_selector("general", bindings, deny()).status is ModelLaneStatus.OVERLAY_UNUSABLE
    assert guard.selector_refusal("sonder", bindings)
    assert guard.default_route_refusal(bindings)
    # a model a bridged provider serves
    assert rule.decide_served("helper-model", deny()).status is ModelLaneStatus.OVERLAY_UNUSABLE
    # an ordinary model too: the overlay cannot say which models it restricted
    with pytest.raises(Exception, match="overlay"):
        guard.require_generation(ALLOWED)
    # text-only chat still works
    guard.require_generation("helper-model", text_only=True)
    # and the binding cannot be written while the overlay cannot judge it
    with pytest.raises(ValueError, match="overlay"):
        runtime_policy.update(local_models={"general": ALLOWED})


def test_an_overlay_that_cannot_be_inspected_refuses_every_model(overlay_file, monkeypatch):
    real = mtp._identity
    monkeypatch.setattr(mtp, "_identity", lambda path: None if Path(path) == overlay_file else real(path))
    config = mtp.load()
    assert not config.readable_overlay
    assert decide(ALLOWED).status is ModelLaneStatus.OVERLAY_UNUSABLE


def test_a_failed_overlay_read_reused_from_the_cache_still_refuses(overlay_file, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mtp.time, "monotonic", lambda: clock[0])
    overlay_file.write_text("{broken", encoding="utf-8")
    assert decide(ALLOWED).status is ModelLaneStatus.OVERLAY_UNUSABLE
    assert decide(ALLOWED).status is ModelLaneStatus.OVERLAY_UNUSABLE  # served from the cache
    with pytest.raises(Exception, match="overlay"):
        guard.require_generation(ALLOWED)


def test_invalid_overlay_is_not_cached_and_recovers(overlay_file):
    overlay_file.write_text("{broken", encoding="utf-8")
    assert not mtp.load().readable_overlay
    assert not mtp.load().readable_overlay
    write_overlay(overlay_file, [profile("mine", names=["fixed-model"])])
    assert mtp.load().readable_overlay
    assert decide("fixed-model").status is ModelLaneStatus.DENIED
    assert decide(ALLOWED, free_form=True).status is ModelLaneStatus.ALLOWED


def test_a_failed_read_is_reused_briefly_for_the_same_files(overlay_file, monkeypatch):
    """Every model call asks; a bad overlay is not parsed again on each one."""
    overlay_file.write_text("{broken", encoding="utf-8")
    builds = []
    real = mtp._build
    monkeypatch.setattr(mtp, "_build", lambda: builds.append(1) or real())
    clock = [1000.0]
    monkeypatch.setattr(mtp.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        assert not mtp.load().readable_overlay
    assert len(builds) == 1
    clock[0] += mtp.FAILED_READ_TTL_SECONDS + 0.01  # the bound passes: read again
    assert not mtp.load().readable_overlay
    assert len(builds) == 2
    write_overlay(overlay_file, [profile("mine", names=["fixed-model"])])  # the file changes: read again
    assert mtp.load().readable_overlay
    assert len(builds) == 3


def test_oversized_overlay_is_refused(overlay_file):
    overlay_file.write_text(" " * (mtp.MAX_FILE_BYTES + 1), encoding="utf-8")
    config = mtp.load()
    assert not config.readable_overlay and "larger than" in config.errors[0]


def test_generation_check_reads_the_overlay_again_after_an_edit(overlay_file):
    guard.require_generation(ALLOWED)
    write_overlay(overlay_file, [profile("mine", names=[ALLOWED])])
    with pytest.raises(Exception, match="blocked from tool-using lanes"):
        guard.require_generation(ALLOWED)
