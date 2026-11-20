"""Pure logic of the cloud model refresh: parsers, grammar, decisions, overlay delta.

No I/O.  The retirement wording is the provider's measured message
(``tests/support/cloud_refresh_harness.MEASURED_RETIREMENT``); the shipped
retired list is read from the shipped document, never hard-coded.
"""

import copy

import pytest

from sonder_runtime.adapters import model_lists
from sonder_runtime.domain import cloud_model_refresh as crm
from sonder_runtime.domain.cloud_model_refresh import ProbeResult, ProbeStatus, Retirement
from sonder_runtime.domain.cloud_tier_policy import resync_cloud_tiers
from tests.support.cloud_refresh_harness import MEASURED_RETIREMENT, retired_message

SHIPPED = model_lists.load_shipped_document()


# --- 1. parse_retirement -------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", "Error: ", "  Error:   "])
def test_parse_retirement_measured_message_with_and_without_error_prefix(prefix):
    parsed = crm.parse_retirement(prefix + MEASURED_RETIREMENT)
    assert parsed == Retirement("deepseek-v4-flash:0731", "September 25, 2026", "glm-5.3-flash:cloud")


def test_parse_retirement_without_suggestion_and_without_trailing_period():
    parsed = crm.parse_retirement("old:cloud was retired on March 3, 2026")
    assert parsed == Retirement("old:cloud", "March 3, 2026", None)


@pytest.mark.parametrize(
    "message",
    [
        "model 'x:cloud' not found",
        "internal server error: upstream timed out",
        "the model x:cloud was retired on September 25, 2026 and more words",  # lookalike
        "x:cloud was retired on 2026-09-25.",
        "x:cloud was not retired on September 25, 2026.",
        "",
        "Error:",
    ],
)
def test_parse_retirement_rejects_other_text(message):
    assert crm.parse_retirement(message) is None


def test_parse_retirement_rejects_non_string_and_oversized_input():
    assert crm.parse_retirement(None) is None  # type: ignore[arg-type]
    assert crm.parse_retirement("a" * 600 + MEASURED_RETIREMENT) is None


def test_parse_retirement_invalid_suggestion_becomes_none():
    parsed = crm.parse_retirement("x:cloud was retired on May 1, 2026. Try a$b%c.")
    assert parsed is not None and parsed.suggestion is None
    assert parsed.provider_name == "x:cloud"


# --- 2. is_cloud_stub_name -----------------------------------------------------


@pytest.mark.parametrize("name", ["kimi-k3:cloud", "kimi-k3:1017-cloud", "GLM-5.3:CLOUD", "a:b-cloud"])
def test_stub_grammar_accepts(name):
    assert crm.is_cloud_stub_name(name)


@pytest.mark.parametrize(
    "name",
    ["qwen2.5:7b", "foo-cloudy:7b", "cloud", ":cloud", "x:-cloud", "bad name:cloud", "x:cloud\n", "", "/x:cloud/",
     "a" * 130 + ":cloud", "qwen3-coder-cloud"],
)
def test_stub_grammar_rejects(name):
    assert not crm.is_cloud_stub_name(name)


# --- 3. parse_preferences ------------------------------------------------------


def test_preferences_minimal_defaults():
    prefs = crm.parse_preferences({"code": ["a:cloud"], "general": ["b:cloud"]})
    assert prefs == crm.Preferences(("a:cloud",), ("b:cloud",), False, True, True)
    assert crm.parse_preferences({}) == crm.Preferences()


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"surprise": 1},
        {"version": 2},
        {"version": True},
        {"auto_pull_stubs": 1},
        {"auto_refresh": "yes"},
        {"catalog": None},
        {"code": ["qwen2.5:7b"]},
        {"code": "a:cloud"},
        {"code": ["a:cloud"] * 0 + ["m%d:cloud" % i for i in range(17)]},
        {"general": [7]},
    ],
)
def test_preferences_reject_invalid_document_whole(document):
    with pytest.raises(ValueError):
        crm.parse_preferences(document)


def test_preferences_sixteen_entries_ok_and_dedupe_is_case_insensitive_keeping_order():
    names = ["m%d:cloud" % i for i in range(16)]
    assert len(crm.parse_preferences({"code": names}).code) == 16
    prefs = crm.parse_preferences({"code": ["B:cloud", "a:cloud", "b:CLOUD", "A:cloud"], "version": 1})
    assert prefs.code == ("B:cloud", "a:cloud")


# --- 4. retired_union ----------------------------------------------------------


def test_retired_union_order_stable_case_insensitive_and_keeps_shipped():
    union = crm.retired_union(["A:cloud", "b:cloud"], ["B:CLOUD", "c:cloud"], ["C:cloud", "d:cloud"])
    assert union == ["A:cloud", "b:cloud", "c:cloud", "d:cloud"]


@pytest.mark.parametrize("overlay", [None, [], ["zzz:cloud"], ["only-first:cloud"]])
def test_retired_union_always_contains_every_shipped_entry(overlay):
    shipped = list(SHIPPED["retired_cloud_models"])
    assert shipped, "shipped list is expected to be non-empty"
    folded = {n.casefold() for n in crm.retired_union(shipped, overlay, ["new:cloud"])}
    assert {n.casefold() for n in shipped} <= folded


def test_retired_heal_detection():
    shipped = ["a:cloud", "b:cloud"]
    assert not crm.retired_heal_needed(shipped, None)  # no overlay key: shipped list applies as is
    assert not crm.retired_heal_needed(shipped, ["B:cloud", "a:cloud", "x:cloud"])
    assert crm.retired_heal_needed(shipped, ["a:cloud"])
    assert crm.retired_heal_needed(shipped, [])


# --- 5. decide_tier ------------------------------------------------------------


def _probe(name, status=ProbeStatus.LIVE, suggestion=None):
    retirement = Retirement(name, "May 1, 2026", suggestion) if status is ProbeStatus.RETIRED else None
    return ProbeResult(name, status, retirement)


def _decide(**overrides):
    args = dict(
        env_value="", default_model="old:cloud", retired_cf=frozenset({"old:cloud"}), probes={},
        pulled_cf=frozenset(), ladder=(), suggestion=None,
    )
    args.update(overrides)
    return crm.decide_tier("code", **args)


def test_decide_rebinds_to_first_live_pulled_entry_skipping_retired_and_unpulled():
    probes = {
        "A:cloud": _probe("A:cloud", ProbeStatus.RETIRED),
        "B:cloud": _probe("B:cloud"),
        "C:cloud": _probe("C:cloud"),
    }
    decision = _decide(
        probes=probes, retired_cf=frozenset({"old:cloud", "a:cloud"}),
        pulled_cf=frozenset({"c:cloud"}), ladder=("A:cloud", "B:cloud", "C:cloud"),
    )
    assert (decision.action, decision.new_model, decision.effective_source) == ("rebind", "C:cloud", "default")
    assert [(e.model, e.state) for e in decision.ladder] == [
        ("A:cloud", "retired"), ("B:cloud", "not_pulled"), ("C:cloud", "selected")]


def test_decide_without_ladder_needs_operator_choice_and_never_picks_the_suggestion():
    probes = {
        "old:cloud": _probe("old:cloud", ProbeStatus.RETIRED, suggestion="glm-5.3:cloud"),
        "glm-5.3:cloud": _probe("glm-5.3:cloud"),  # live and pulled, but not in the ladder
    }
    decision = _decide(probes=probes, pulled_cf=frozenset({"glm-5.3:cloud"}), ladder=("other:cloud",))
    assert decision.action == "needs_operator_choice"
    assert decision.new_model is None
    assert decision.suggestion == "glm-5.3:cloud"
    assert "needs operator choice" in decision.note
    assert _decide(probes=probes, pulled_cf=frozenset({"glm-5.3:cloud"})).new_model is None


def test_decide_suggestion_taken_only_when_it_is_itself_the_first_qualifying_ladder_entry():
    probes = {
        "old:cloud": _probe("old:cloud", ProbeStatus.RETIRED, suggestion="glm-5.3:cloud"),
        "glm-5.3:cloud": _probe("glm-5.3:cloud"),
    }
    decision = _decide(probes=probes, pulled_cf=frozenset({"glm-5.3:cloud"}), ladder=("glm-5.3:cloud",))
    assert (decision.action, decision.new_model) == ("rebind", "glm-5.3:cloud")


def test_decide_env_bound_live_model_is_kept_even_if_default_is_retired():
    decision = _decide(env_value="mine:cloud", ladder=("a:cloud",),
                       probes={"a:cloud": _probe("a:cloud")}, pulled_cf=frozenset({"a:cloud"}))
    assert (decision.action, decision.effective_source, decision.effective_model) == ("keep", "env", "mine:cloud")
    assert decision.new_model is None


def test_decide_retired_env_with_live_default_keeps_and_notes_the_env():
    decision = _decide(env_value="gone:cloud", default_model="fine:cloud",
                       retired_cf=frozenset({"gone:cloud"}))
    assert (decision.action, decision.effective_source, decision.effective_model) == ("keep", "default", "fine:cloud")
    assert "env SONDER_CLOUD_CODE names retired gone:cloud; not changed" in decision.note


def test_decide_retired_env_and_retired_default_goes_to_the_ladder():
    decision = _decide(
        env_value="gone:cloud", retired_cf=frozenset({"gone:cloud", "old:cloud"}),
        probes={"a:cloud": _probe("a:cloud")}, pulled_cf=frozenset({"a:cloud"}), ladder=("a:cloud",))
    assert (decision.action, decision.new_model) == ("rebind", "a:cloud")
    assert "env SONDER_CLOUD_CODE names retired gone:cloud; not changed" in decision.note


def test_decide_never_chooses_non_stub_ladder_entries():
    probes = {"qwen2.5:7b": _probe("qwen2.5:7b"), "c:cloud": _probe("c:cloud")}
    pulled = frozenset({"qwen2.5:7b", "c:cloud"})
    decision = _decide(probes=probes, pulled_cf=pulled, ladder=("qwen2.5:7b", "c:cloud"))
    assert decision.new_model == "c:cloud"
    assert decision.ladder[0].state == "invalid"
    only_local = _decide(probes=probes, pulled_cf=pulled, ladder=("qwen2.5:7b",))
    assert only_local.action == "needs_operator_choice"


def test_decide_ladder_entry_must_be_live_this_run_not_error_notfound_or_unprobed():
    probes = {"e:cloud": _probe("e:cloud", ProbeStatus.ERROR), "n:cloud": _probe("n:cloud", ProbeStatus.NOT_FOUND)}
    decision = _decide(probes=probes, pulled_cf=frozenset({"e:cloud", "n:cloud", "u:cloud"}),
                       ladder=("e:cloud", "n:cloud", "u:cloud"))
    assert decision.action == "needs_operator_choice"
    assert [e.state for e in decision.ladder] == ["error", "not_live", "unprobed"]


def test_decide_live_default_is_kept_and_ladder_unused():
    decision = _decide(default_model="fine:cloud", ladder=("a:cloud",),
                       probes={"a:cloud": _probe("a:cloud")}, pulled_cf=frozenset({"a:cloud"}))
    assert decision.action == "keep" and decision.new_model is None


# --- 6. collect_known_models ---------------------------------------------------


def _effective(**changes):
    effective = copy.deepcopy(SHIPPED)
    effective.update(changes)
    return effective


def _collect(**overrides):
    args = dict(shipped=SHIPPED, effective=SHIPPED, env={}, pulled=(), prefs=None, catalog_tags=(),
                state_models={}, now=1_800_000_000.0)
    args.update(overrides)
    return crm.collect_known_models(**args)


def test_collect_covers_every_source_and_drops_non_cloud_names():
    effective = _effective(default_cloud_models={"code": "ov-code:cloud", "general": SHIPPED["default_cloud_models"]["general"]})
    prefs = crm.Preferences(code=("lad:cloud",))
    known = {m.name: m.sources for m in _collect(
        effective=effective,
        env={"SONDER_CLOUD_CODE": "envc:cloud", "SONDER_CODE": "qwen3-coder:480b-cloud"},
        pulled=[{"name": "pul:cloud"}, {"name": "tagrow:1b", "remote_host": "https://ollama.com:443"},
                {"name": "local:7b"}],
        prefs=prefs, catalog_tags=["cat:cloud", "plain-model:7b"],
    )}
    assert "env" in known["envc:cloud"] and "env" in known["qwen3-coder:480b-cloud"]
    assert known["ov-code:cloud"] >= {"default", "overlay"}
    assert "default" in known[SHIPPED["default_cloud_models"]["code"]]
    assert "legacy" in known[SHIPPED["legacy_cloud_general_model"]]
    assert known["lad:cloud"] == {"ladder"}
    assert known["pul:cloud"] == {"pulled"} and known["tagrow:1b"] == {"pulled"}
    assert known["cat:cloud"] == {"catalog"}
    assert "local:7b" not in known and "plain-model:7b" not in known


def test_collect_priority_order_and_probe_budget():
    prefs = crm.Preferences(code=("lad:cloud",))
    known = _collect(env={"SONDER_CLOUD_GENERAL": "envg:cloud"}, prefs=prefs,
                     pulled=[{"name": "pul:cloud"}], catalog_tags=["cat:cloud"], max_probes=3)
    names = [m.name for m in known]
    assert names[0] == "envg:cloud"
    assert names.index("lad:cloud") < names.index("pul:cloud") < names.index("cat:cloud")
    assert [m.probe for m in known] == [True, True, True] + [False] * (len(known) - 3)
    assert len(known) > 3


def test_collect_default_budget_is_max_probes():
    tags = ["t%d:cloud" % i for i in range(crm.MAX_PROBES + 10)]
    known = _collect(catalog_tags=tags)
    assert sum(m.probe for m in known) == crm.MAX_PROBES
    assert not known[-1].probe


def test_collect_skips_already_retired_unbound_names_but_reconfirms_bound_ones():
    retired = SHIPPED["retired_cloud_models"][0]
    only_catalog = {m.name for m in _collect(catalog_tags=[retired, "fresh:cloud"], pulled=[{"name": retired}])}
    assert retired not in only_catalog and "fresh:cloud" in only_catalog
    ladder = {m.name for m in _collect(prefs=crm.Preferences(code=(retired,)))}
    assert retired in ladder
    env = {m.name for m in _collect(env={"SONDER_CLOUD_CODE": retired})}
    assert retired in env


def test_collect_catalog_tag_reprobe_window():
    now = 1_800_000_000.0
    fresh = {"probed_at": crm.iso_timestamp(now - 3600)}
    stale = {"probed_at": crm.iso_timestamp(now - 8 * 24 * 3600)}
    state = {"fresh:cloud": fresh, "stale:cloud": stale}
    names = {m.name for m in _collect(catalog_tags=["fresh:cloud", "stale:cloud", "new:cloud"],
                                      state_models=state, now=now)}
    assert "fresh:cloud" not in names and {"stale:cloud", "new:cloud"} <= names


def test_collect_dedupes_case_insensitively_and_rejects_invalid_names():
    known = _collect(pulled=[{"name": "Dup:cloud"}], catalog_tags=["dup:cloud", "bad name:cloud", 5])
    assert [m.name for m in known].count("Dup:cloud") == 1
    assert not [m for m in known if m.name.casefold() == "dup:cloud" and m.name != "Dup:cloud"]
    assert all(" " not in m.name for m in known)


# --- 7. catalog parsers --------------------------------------------------------

SEARCH_HTML = """
<a href="/library/kimi-k3">kimi</a> <a href="/library/glm-5.3">glm</a>
<a href="/library/kimi-k3">again</a>
<a href="/library/../etc/passwd">x</a> <a href="/library/UPPER">x</a>
<a href="/library/a/b">x</a> <a href="https://evil.example/library/zzz">x</a>
<a href="/library/%2e%2e">x</a> <a href='/library/single'>x</a> <a href="/library/<script>">x</a>
"""

TAGS_HTML = """
kimi-k3:cloud kimi-k3:1017-cloud kimi-k3:latest kimi-k3:7b x-kimi-k3:cloud
<td>kimi-k3:cloud</td> glm-5.3:cloud kimi-k3:-cloud kimi-k3:cloudy
"""


def test_catalog_families_from_library_hrefs_only_deduped():
    assert crm.parse_catalog_families(SEARCH_HTML) == ("kimi-k3", "glm-5.3")


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "garbage \x00\xff"])
def test_catalog_empty_or_garbage_page_gives_nothing(html):
    assert crm.parse_catalog_families(html) == ()
    assert crm.parse_catalog_tags("kimi-k3", html) == ()


def test_catalog_families_capped():
    html = "".join('<a href="/library/m%d">x</a>' % i for i in range(crm.MAX_FAMILIES + 20))
    assert len(crm.parse_catalog_families(html)) == crm.MAX_FAMILIES


def test_catalog_tags_only_that_familys_cloud_stubs():
    assert crm.parse_catalog_tags("kimi-k3", TAGS_HTML) == ("kimi-k3:cloud", "kimi-k3:1017-cloud")
    assert crm.parse_catalog_tags("glm-5.3", TAGS_HTML) == ("glm-5.3:cloud",)


@pytest.mark.parametrize("family", ["../etc", "UP", "a/b", "", "x y", None])
def test_catalog_tags_hostile_family_rejected(family):
    assert crm.parse_catalog_tags(family, TAGS_HTML) == ()  # type: ignore[arg-type]


# --- 8. apply_to_overlay -------------------------------------------------------


def _decision(tier, action, effective, new=None):
    return crm.TierDecision(tier, effective, "default", action, new,
                            (crm.LadderEntry(new, "selected"),) if new else (), None, "")


def _plan(retire=(), decisions=(), write_retired=True):
    shipped_retired = SHIPPED["retired_cloud_models"]
    after = crm.retired_union(shipped_retired, None, [name for name, _ in retire])
    return crm.RefreshPlan(tuple(retire), tuple(after), write_retired, tuple(decisions), (), (), ())


def _observed():
    return dict(SHIPPED["default_cloud_models"])


def test_apply_to_overlay_writes_only_retired_defaults_and_version():
    code = SHIPPED["default_cloud_models"]["code"]
    plan = _plan(
        retire=[(code, Retirement("kimi:0801", "August 1, 2026", None))],
        decisions=[_decision("code", "rebind", code, "new-code:cloud")],
    )
    base = {"version": 1, "default_local_models": {"code": "custom:7b"}}
    document, changes = crm.apply_to_overlay(base, plan, shipped=SHIPPED, observed_defaults=_observed())
    assert set(document) == {"version", "default_local_models", "retired_cloud_models", "default_cloud_models"}
    assert document["default_local_models"] == {"code": "custom:7b"}
    assert document["default_cloud_models"] == {"code": "new-code:cloud"}
    assert document["retired_cloud_models"] == crm.retired_union(SHIPPED["retired_cloud_models"], None, [code])
    assert [c.action for c in changes] == ["retire", "rebind"]
    assert base == {"version": 1, "default_local_models": {"code": "custom:7b"}}  # input untouched
    merged = model_lists.merge(SHIPPED, document)
    assert merged["default_cloud_models"]["code"] == "new-code:cloud"


def test_apply_to_overlay_compare_and_swap_skips_a_concurrently_changed_tier():
    code = SHIPPED["default_cloud_models"]["code"]
    plan = _plan(decisions=[_decision("code", "rebind", code, "new-code:cloud")], write_retired=False)
    base = {"version": 1, "default_cloud_models": {"code": "operator-choice:cloud"}}
    document, changes = crm.apply_to_overlay(base, plan, shipped=SHIPPED, observed_defaults=_observed())
    assert document["default_cloud_models"] == {"code": "operator-choice:cloud"}
    assert [(c.action, c.key) for c in changes] == [("changed_concurrently", "default_cloud_models.code")]


def test_apply_to_overlay_heals_a_retired_list_that_hides_shipped_entries():
    plan = _plan(write_retired=True)
    base = {"version": 1, "retired_cloud_models": ["operator:cloud"]}
    document, changes = crm.apply_to_overlay(base, plan, shipped=SHIPPED, observed_defaults=_observed())
    folded = {n.casefold() for n in document["retired_cloud_models"]}
    assert {n.casefold() for n in SHIPPED["retired_cloud_models"]} <= folded
    assert "operator:cloud" in document["retired_cloud_models"]
    assert [c.action for c in changes] == ["heal_retired_union"]


def test_apply_to_overlay_is_a_noop_when_nothing_to_do_and_never_drops_overlay_retirements():
    plan = _plan(write_retired=False)
    base = {"version": 1, "retired_cloud_models": list(SHIPPED["retired_cloud_models"]) + ["mine:cloud"]}
    document, changes = crm.apply_to_overlay(base, plan, shipped=SHIPPED, observed_defaults=_observed())
    assert document == base and changes == ()


def test_apply_to_overlay_new_retirement_is_not_added_twice():
    name = "fresh-retired:cloud"
    plan = _plan(retire=[(name, Retirement("fresh:0801", "May 1, 2026", None))])
    base = {"version": 1, "retired_cloud_models": list(SHIPPED["retired_cloud_models"]) + [name]}
    document, changes = crm.apply_to_overlay(base, plan, shipped=SHIPPED, observed_defaults=_observed())
    assert document["retired_cloud_models"].count(name) == 1 and changes == ()


# --- 9. resync_cloud_tiers -----------------------------------------------------

LEGACY = "gpt-oss:120b-cloud"


def _resync(tiers, *, env=None, applied=None, defaults=None, retired=()):
    moves = resync_cloud_tiers(
        tiers, env or {}, applied=applied if applied is not None else {"code": {"a:cloud"}, "general": {"g:cloud"}},
        current_defaults=defaults or {"code": "b:cloud", "general": "g:cloud"},
        retired=retired, legacy_cloud_general_model=LEGACY)
    return moves


def test_resync_default_derived_tier_follows_new_default():
    tiers = {"cloud-code": "a:cloud", "cloud-general": "g:cloud"}
    moves = _resync(tiers)
    assert tiers == {"cloud-code": "b:cloud", "cloud-general": "g:cloud"}
    assert moves == [("cloud-code", "a:cloud", "b:cloud")]


def test_resync_env_bound_live_tier_is_untouched():
    tiers = {"cloud-code": "mine:cloud", "cloud-general": "g:cloud"}
    applied = {"code": {"a:cloud", "mine:cloud"}, "general": {"g:cloud"}}
    assert _resync(tiers, env={"SONDER_CLOUD_CODE": "mine:cloud"}, applied=applied) == []
    assert tiers["cloud-code"] == "mine:cloud"


def test_resync_user_binding_that_was_never_a_default_is_untouched():
    tiers = {"cloud-code": "hand-set:cloud", "cloud-general": "g:cloud"}
    assert _resync(tiers) == []
    assert tiers["cloud-code"] == "hand-set:cloud"


def test_resync_retired_binding_moves_to_a_live_default_even_when_env_bound():
    tiers = {"cloud-code": "dead:cloud", "cloud-general": "g:cloud"}
    moves = _resync(tiers, env={"SONDER_CLOUD_CODE": "dead:cloud"}, applied={"code": set()}, retired=["DEAD:cloud"])
    assert tiers["cloud-code"] == "b:cloud" and moves == [("cloud-code", "dead:cloud", "b:cloud")]


def test_resync_never_moves_to_a_retired_default():
    tiers = {"cloud-code": "a:cloud", "cloud-general": "g:cloud"}
    assert _resync(tiers, retired=["b:cloud"]) == []
    assert tiers["cloud-code"] == "a:cloud"


def test_resync_honours_the_legacy_preserve_flag():
    tiers = {"cloud-code": "a:cloud", "cloud-general": LEGACY}
    applied = {"code": {"a:cloud"}, "general": {LEGACY}}
    defaults = {"code": "a:cloud", "general": "g:cloud"}
    assert _resync(tiers, env={"SONDER_PRESERVE_LEGACY_CLOUD_GENERAL": "1"}, applied=applied, defaults=defaults) == []
    assert tiers["cloud-general"] == LEGACY
    assert _resync(tiers, env={}, applied=applied, defaults=defaults) == [("cloud-general", LEGACY, "g:cloud")]


def test_resync_working_on_a_copy_does_not_stop_the_real_map_following_later():
    applied = {"code": {"a:cloud"}, "general": {"g:cloud"}}
    real = {"cloud-code": "a:cloud", "cloud-general": "g:cloud"}
    _resync(dict(real), applied=applied)  # prepared-workbench style copy advances the shared snapshot
    _resync(real, applied=applied)
    assert real["cloud-code"] == "b:cloud"


def test_retirement_message_helper_matches_the_parser():
    assert crm.parse_retirement(retired_message("p:1", "s:cloud")) == Retirement("p:1", "September 25, 2026", "s:cloud")
