"""The pure tool-lane deny rule (``sonder_runtime.domain.tool_lane_models``).

No I/O and no environment: every test builds its deny list from explicit
arguments, so these pass identically with the machine's live variables and
with them cleared.
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain import tool_lane_models as rule
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus, ToolLaneDenyList

DEFAULTS = ToolLaneDenyList.from_policy(None)
QWEN_SOURCE = "mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF"
ORNITH_SOURCE = "DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF"


def denied(name, deny=DEFAULTS, *, free_form=False):
    return rule.decide(name, deny, free_form=free_form).denied


# ---- names denied ------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "astra-cyber-27b",
    "astra-cyber-9b",
    "astra-cyber-",
    "Astra-Cyber-27B",
    "ASTRA-CYBER-9B",
    "  astra-cyber-27b  ",
    "\tastra-cyber-27b\n",
    "astra-cyber-27b:latest",
    "ASTRA-CYBER-27B:LATEST",
    "astra-cyber-27b:q4_k_m",
    "astra-cyber-27b:Q8_0",
    "astra-cyber-27b:27b-instruct-q4_k_m",
    "astra-cyber-27b@sha256:0123456789abcdef",
    "astra-cyber-27b:q4_k_m@sha256:0123456789abcdef",
    "astra-cyber- 27b",
    "library/astra-cyber-27b",
    "library/astra-cyber-27b:latest",
    "registry.ollama.ai/library/astra-cyber-27b:latest",
    "localhost:11434/library/astra-cyber-9b",
    "someone/astra-cyber-70b",
    "a/b/c/astra-cyber-1b:tag",
    "astra-cyber-27b/",  # trailing slash leaves the full name matching the prefix
])
def test_default_prefix_denies_name_variants(name):
    decision = rule.decide(name, DEFAULTS, free_form=False)
    assert decision.status is ModelLaneStatus.DENIED, name
    assert decision.denied is True
    assert decision.model == name.strip()
    assert decision.matched  # says which entry matched


def test_matched_entry_is_the_prefix_for_the_default_family():
    assert rule.decide("Library/Astra-Cyber-27b:latest", DEFAULTS, free_form=False).matched == "astra-cyber-"


@pytest.mark.parametrize("name", [
    f"hf.co/{QWEN_SOURCE}",
    f"hf.co/{QWEN_SOURCE}:Q3_K_M",
    f"hf.co/{QWEN_SOURCE}:Q4_K_M",
    f"hf.co/{QWEN_SOURCE}:Q8_0",
    f"hf.co/{QWEN_SOURCE}:latest",
    f"hf.co/{QWEN_SOURCE}:IQ4_XS",
    f"HF.CO/{QWEN_SOURCE.upper()}:q4_k_m",
    f"  hf.co/{QWEN_SOURCE}:Q4_K_M  ",
    f"hf.co/{ORNITH_SOURCE}",
    f"hf.co/{ORNITH_SOURCE}:Q4_K_M",
    f"hf.co/{ORNITH_SOURCE}:Q8_0",
    f"hf.co/{ORNITH_SOURCE}:latest",
    f"hf.co/{ORNITH_SOURCE.lower()}:F16",
    f"huggingface.co/{QWEN_SOURCE}",
    f"huggingface.co/{QWEN_SOURCE}:Q4_K_M",
    f"huggingface.co/{ORNITH_SOURCE}",
    f"huggingface.co/{ORNITH_SOURCE}:Q8_0",
    f"HuggingFace.co/{ORNITH_SOURCE}:Q5_K_S",
])
def test_hf_sources_denied_in_every_quantisation_tagged_or_not(name):
    assert denied(name), name


@pytest.mark.parametrize("source", (QWEN_SOURCE, ORNITH_SOURCE))
def test_each_builtin_source_is_denied_under_every_repository_and_file_spelling(source):
    stem = source.rsplit("/", 1)[-1].removesuffix("-GGUF")
    spellings = (
        source, source + ":Q4_K_M", source.rsplit("/", 1)[-1],
        stem, stem + ":Q8_0", stem + "-Q4_K_M.gguf",
        "hf.co/" + source, "huggingface.co/" + source,
        "https://hf.co/" + source, "https://huggingface.co/" + source,
        "registry.ollama.ai/hf.co/" + source + ":Q4_K_M",
        "cache/" + stem + "/quantised.gguf",
    )
    for name in spellings:
        assert denied(name), name


@pytest.mark.parametrize("name", (
    "qwen3.5:4b", "qwen3:8b", "sonder:latest", "kimi-k2.7-code:cloud",
    "glm-5.2:cloud", "qwen3-coder:480b-cloud", "qwen3.8", "qwen3-coder",
    "hf.co/mradermacher/Qwen3.8-27B-GGUF:Q4_K_M",
    "hf.co/mradermacher/Qwen3.8-cyber-Surgical-GGUF",
    "hf.co/DuoNeural/Cyber-Ornith-1.5-8B-GGUF",
    "registry.ollama.ai/unsloth/Qwen3.8-27B-GGUF",
))
def test_existing_defaults_helpers_and_other_gguf_repositories_remain_allowed(name):
    assert not denied(name), name


def test_a_longer_repo_name_under_a_denied_hf_source_is_denied_by_design():
    assert denied(f"hf.co/{QWEN_SOURCE}-extra")


def test_hf_defaults_are_prefixes_not_exact_names():
    """The hosted ids, hostless ids and stems all come from the source list."""
    prefixes = set(rule.DEFAULT_DENIED_PREFIXES)
    for host in ("hf.co/", "huggingface.co/"):
        for source in (QWEN_SOURCE, ORNITH_SOURCE):
            assert (host + source).casefold() in prefixes
    for source in (QWEN_SOURCE, ORNITH_SOURCE):
        assert source.casefold() in prefixes
        assert source.rsplit("/", 1)[-1].removesuffix("-GGUF").casefold() in prefixes
    assert "astra-cyber-" in prefixes
    assert DEFAULTS.names == frozenset()


# ---- names allowed -----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "qwen3.5:4b",
    "qwen3:8b",
    "sonder:latest",
    "llama3.1:8b",
    "astra-sentinel",  # near miss: shares the first word only
    "astra-sentinel-27b",
    "astracyber",  # no hyphen
    "astra-cyber",  # missing the trailing hyphen the prefix requires
    "astra_cyber_27b",
    "astra-cybernetic-27b",
    "my-astra-cyber-27b",  # prefix appears mid-string only
    "x/my-astra-cyber-27b",
    "not-astra-cyber-",
    "astra",
    "cyber-astra-27b",
    "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL",
    "huggingface.co/unsloth/Qwen3.8-27B-GGUF",
])
def test_ordinary_and_near_miss_names_are_allowed(name):
    decision = rule.decide(name, DEFAULTS, free_form=False)
    assert decision.status is ModelLaneStatus.ALLOWED, name
    assert decision.denied is False


@pytest.mark.parametrize("name", ["", "   ", "\t\n", None])
def test_empty_and_none_are_allowed_even_when_unreadable(name):
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    for deny in (DEFAULTS, unreadable):
        for free_form in (False, True):
            assert rule.decide(name, deny, free_form=free_form).status is ModelLaneStatus.ALLOWED


def test_non_string_names_are_judged_by_their_text():
    assert not denied(12345)
    assert denied(type("Name", (), {"__str__": lambda self: "astra-cyber-27b"})())


# ---- normalisation -----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Astra-Cyber-27B", "astra-cyber-27b"),
    ("  qwen3:8B  ", "qwen3:8b"),
    ("a   b\t\tc", "a b c"),
    ("model:latest", "model"),
    ("MODEL:LATEST", "model"),
    ("model:latest:latest", "model:latest"),  # exactly one trailing tag is dropped
    ("model:latest-extra", "model:latest-extra"),
    (":latest", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_name(raw, expected):
    assert rule.normalize_name(raw) == expected


def test_case_folding_is_applied_to_the_name():
    """Dropping the case fold would let an upper-case spelling through."""
    assert rule.normalize_name("ASTRA-CYBER-27B") == "astra-cyber-27b"
    assert denied("ASTRA-CYBER-27B")
    assert denied("Astra-Cyber-9B:LATEST")


def test_unicode_look_alikes_follow_casefold_only():
    """Normalisation is ``casefold``, nothing more: no NFKC or confusable mapping.

    A name that casefolds onto the denied spelling is denied (the long s,
    U+017F, folds to ``s``); a different code point that merely looks the same
    is a different name, which no Ollama catalog resolves to the blocked model.
    """
    assert denied("aſtra-cyber-27b")  # LATIN SMALL LETTER LONG S -> "s"
    assert denied("ASTRA-CYBER-27B".replace("S", "ſ"))
    assert not denied("аstra-cyber-27b")  # CYRILLIC SMALL LETTER A
    assert not denied("astra-cyber-27b".replace("a", "ａ"))  # FULLWIDTH A
    assert not denied("astra‑cyber-27b")  # NON-BREAKING HYPHEN
    assert not denied("astra-cyber\u200b-27b")  # ZERO WIDTH SPACE is not whitespace here
    assert not denied("\u200bastra-cyber-27b")


def test_a_look_alike_slash_does_not_create_a_path_segment():
    assert not denied("x∕astra-cyber-27b")  # DIVISION SLASH is not a path separator
    assert denied("x/astra-cyber-27b")


# ---- last path segment -------------------------------------------------------

def test_a_prefix_is_matched_against_the_last_path_segment():
    for name in ("x/astra-cyber-27b", "a/b/astra-cyber-27b:q4", "registry.example.com:5000/ns/Astra-Cyber-9B"):
        decision = rule.decide(name, DEFAULTS, free_form=False)
        assert decision.denied, name
        assert decision.matched == "astra-cyber-"


def test_an_exact_name_is_matched_against_the_last_path_segment():
    deny = ToolLaneDenyList.from_policy({"names": ["Helper-Model"], "prefixes": []})
    for name in ("helper-model", "HELPER-MODEL:latest", "owner/helper-model", "reg.io/owner/Helper-Model:latest"):
        assert denied(name, deny), name
    assert not denied("helper-model-2", deny)
    assert not denied("owner/helper-model/other", deny)  # policy names keep last-segment matching
    assert not denied("my-helper-model", deny)


def test_an_exact_name_does_not_match_as_a_prefix():
    deny = ToolLaneDenyList.from_policy({"names": ["helper"], "prefixes": []})
    assert denied("helper", deny)
    assert not denied("helper-2", deny)
    assert not denied("helper:2b", deny)  # a tag other than :latest is a different exact name


def test_a_policy_prefix_matches_by_prefix():
    deny = ToolLaneDenyList.from_policy({"names": [], "prefixes": ["Team-X/"]})
    assert denied("team-x/anything:7b", deny)
    assert denied("TEAM-X/other", deny)
    assert not denied("other/team-x/anything", deny)  # policy prefixes keep full-name/last-segment matching
    assert not denied("team-xylophone", deny)


# ---- the list itself ---------------------------------------------------------

def test_defaults_are_always_in_the_list_and_nothing_is_required_of_the_policy():
    for section in (None, {}, {"names": [], "prefixes": []}, {"names": None, "prefixes": None}, "nope", 7, []):
        deny = ToolLaneDenyList.from_policy(section)
        assert deny.prefixes[: len(rule.DEFAULT_DENIED_PREFIXES)] == rule.DEFAULT_DENIED_PREFIXES, section
        assert deny.extended is False, section
        assert denied("astra-cyber-27b", deny), section


def test_policy_names_and_prefixes_extend_the_defaults():
    deny = ToolLaneDenyList.from_policy({"names": [" Extra-One:LATEST ", "extra-two"], "prefixes": ["Acme/"]})
    assert deny.names == frozenset({"extra-one", "extra-two"})
    assert deny.prefixes == rule.DEFAULT_DENIED_PREFIXES + ("acme/",)
    assert deny.extended is True
    assert denied("extra-one", deny) and denied("extra-two:latest", deny) and denied("acme/model", deny)
    assert denied("astra-cyber-27b", deny)  # the defaults survive the union
    assert denied(f"hf.co/{ORNITH_SOURCE}:Q4_K_M", deny)


def test_policy_entries_can_never_remove_the_defaults():
    """Whatever the section holds, every default entry is still present and still denies."""
    attempts = (
        {"names": [], "prefixes": []},
        {"names": ["qwen3.5:4b"], "prefixes": ["x"]},
        {"allow": ["astra-cyber-27b"], "remove": ["astra-cyber-"], "names": [], "prefixes": []},
        {"prefixes": ["astra-cyber-"]},  # re-listing a default does not duplicate or displace it
    )
    for section in attempts:
        deny = ToolLaneDenyList.from_policy(section)
        for default in rule.DEFAULT_DENIED_PREFIXES:
            assert default in deny.prefixes, (section, default)
        assert denied("astra-cyber-27b", deny)
        assert denied(f"hf.co/{QWEN_SOURCE}", deny)
    deny = ToolLaneDenyList.from_policy({"prefixes": ["astra-cyber-", "ASTRA-CYBER-"]})
    assert deny.prefixes.count("astra-cyber-") == 1


def test_malformed_entries_are_ignored_not_fatal():
    deny = ToolLaneDenyList.from_policy({"names": "single-string", "prefixes": 5})
    assert deny.extended is False
    deny = ToolLaneDenyList.from_policy({"names": ["", "   ", None, ":latest", "ok"], "prefixes": [""]})
    assert deny.names == frozenset({"ok"})
    assert deny.prefixes == rule.DEFAULT_DENIED_PREFIXES


def test_an_empty_prefix_entry_cannot_deny_everything():
    deny = ToolLaneDenyList.from_policy({"prefixes": ["", "  ", ":latest"]})
    assert not denied("qwen3.5:4b", deny)
    assert not denied("sonder", deny)


def test_the_list_is_immutable():
    with pytest.raises(AttributeError):
        DEFAULTS.readable = False
    assert isinstance(DEFAULTS.names, frozenset) and isinstance(DEFAULTS.prefixes, tuple)


# ---- unreadable policy -------------------------------------------------------

def test_unreadable_policy_refuses_caller_named_models_only():
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    assert unreadable.readable is False
    named = rule.decide("qwen3.5:4b", unreadable, free_form=True)
    assert named.status is ModelLaneStatus.POLICY_UNREADABLE and named.denied is True
    bound = rule.decide("qwen3.5:4b", unreadable, free_form=False)
    assert bound.status is ModelLaneStatus.ALLOWED
    # A denied name stays DENIED (not "unreadable"): the lists still apply.
    assert rule.decide("astra-cyber-27b", unreadable, free_form=True).status is ModelLaneStatus.DENIED
    assert rule.decide("astra-cyber-27b", unreadable, free_form=False).status is ModelLaneStatus.DENIED
    # Readable lists never produce the unreadable status.
    assert rule.decide("qwen3.5:4b", DEFAULTS, free_form=True).status is ModelLaneStatus.ALLOWED


# ---- selector decision -------------------------------------------------------

BINDINGS = {"fast": "qwen3.5:4b", "code": "qwen3:8b", "general": "llama3.1:8b", "bad": "astra-cyber-9b"}


@pytest.mark.parametrize("selector", ["", "  ", None, "sonder", "SONDER", " Sonder ", "local", "LOCAL", " local\t"])
def test_default_route_selectors_are_always_allowed(selector):
    decision = rule.decide_selector(selector, BINDINGS, DEFAULTS)
    assert decision.status is ModelLaneStatus.ALLOWED
    # Even when the policy is unreadable: they name no model.
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    assert rule.decide_selector(selector, BINDINGS, unreadable).status is ModelLaneStatus.ALLOWED


def test_a_bound_tier_is_judged_by_the_model_it_is_bound_to():
    assert rule.decide_selector("fast", BINDINGS, DEFAULTS).status is ModelLaneStatus.ALLOWED
    assert rule.decide_selector("FAST", BINDINGS, DEFAULTS).status is ModelLaneStatus.ALLOWED
    bad = rule.decide_selector("bad", BINDINGS, DEFAULTS)
    assert bad.status is ModelLaneStatus.DENIED and bad.model == "astra-cyber-9b"
    # A bound tier is not caller-named, so an unreadable list does not refuse it.
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    assert rule.decide_selector("fast", BINDINGS, unreadable).status is ModelLaneStatus.ALLOWED
    assert rule.decide_selector("bad", BINDINGS, unreadable).status is ModelLaneStatus.DENIED


def test_any_other_selector_is_a_caller_named_model():
    assert rule.decide_selector("Astra-Cyber-27B", BINDINGS, DEFAULTS).denied
    assert rule.decide_selector("  library/astra-cyber-9b:latest ", BINDINGS, DEFAULTS).denied
    assert rule.decide_selector(f"hf.co/{ORNITH_SOURCE}:Q4_K_M", BINDINGS, DEFAULTS).denied
    assert rule.decide_selector("qwen3.5:4b", BINDINGS, DEFAULTS).status is ModelLaneStatus.ALLOWED
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    assert rule.decide_selector("qwen3.5:4b", BINDINGS, unreadable).status is ModelLaneStatus.POLICY_UNREADABLE


def test_a_tier_name_is_not_resolved_against_the_deny_list_by_name():
    """A bound tier whose own name looks denied is judged by its binding, not its key."""
    bindings = {"astra-cyber-lab": "qwen3.5:4b"}
    assert rule.decide_selector("astra-cyber-lab", bindings, DEFAULTS).status is ModelLaneStatus.ALLOWED


# ---- served identity ---------------------------------------------------------

@pytest.mark.parametrize("served", ["", "   ", None, "default", "DEFAULT", " Default ", "default:latest"])
def test_an_undetermined_served_model_is_unknown(served):
    decision = rule.decide_served(served, DEFAULTS)
    assert decision.status is ModelLaneStatus.SERVED_UNKNOWN
    assert decision.denied is True


@pytest.mark.parametrize("served", [
    "astra-cyber-9b",
    "Astra-Cyber-27B:latest",
    "acme/astra-cyber-27b",
    "openrouter/acme/astra-cyber-27b:free",
    f"hf.co/{QWEN_SOURCE}:Q4_K_M",
])
def test_a_denied_served_model_is_denied(served):
    assert rule.decide_served(served, DEFAULTS).status is ModelLaneStatus.DENIED


def test_an_ordinary_served_model_is_allowed_and_a_served_name_is_not_free_form():
    assert rule.decide_served("qwen3.5:4b", DEFAULTS).status is ModelLaneStatus.ALLOWED
    assert rule.decide_served("gpt-4o-mini", DEFAULTS).status is ModelLaneStatus.ALLOWED
    # A configured served model is bound configuration, not a caller-named model.
    unreadable = ToolLaneDenyList.from_policy(None, readable=False)
    assert rule.decide_served("qwen3.5:4b", unreadable).status is ModelLaneStatus.ALLOWED
    assert rule.decide_served("astra-cyber-9b", unreadable).status is ModelLaneStatus.DENIED


def test_a_name_containing_default_is_not_the_provider_default():
    assert rule.decide_served("default-model", DEFAULTS).status is ModelLaneStatus.ALLOWED
    assert rule.decide_served("my/default", DEFAULTS).status is ModelLaneStatus.ALLOWED


# ---- bindings ----------------------------------------------------------------

def test_allowed_bindings_replace_a_denied_model_with_the_tier_default():
    defaults = {"fast": "d-fast", "code": "d-code"}
    out = rule.allowed_bindings(
        {"fast": "Astra-Cyber-9B", "code": "qwen3:8b", "hosted": "astra-cyber-27b", "other": "ok"},
        DEFAULTS, defaults,
    )
    assert out == {"fast": "d-fast", "code": "qwen3:8b", "other": "ok"}  # "hosted" has no default: dropped


def test_allowed_bindings_do_not_mutate_the_input():
    source = {"fast": "astra-cyber-9b"}
    rule.allowed_bindings(source, DEFAULTS, {"fast": "d"})
    assert source == {"fast": "astra-cyber-9b"}


# ---- messages ----------------------------------------------------------------

def test_refusal_messages_are_plain_text_and_name_the_fix():
    blocked = rule.refusal_message("astra-cyber-27b")
    assert blocked == (
        "model 'astra-cyber-27b' is blocked from tool-using lanes by runtime policy; "
        "it can be used for chat by name only."
    )
    unreadable = rule.refusal_message("m", ModelLaneStatus.POLICY_UNREADABLE)
    assert "unreadable" in unreadable and "/runtime reset" in unreadable and "repair the file" in unreadable
    for text in (blocked, unreadable):
        assert not text.startswith("ERROR")


def test_served_refusal_messages():
    denied_text = rule.served_refusal_message(
        "code", "sonder_inference", rule.ModelLaneDecision(ModelLaneStatus.DENIED, "astra-cyber-9b"))
    assert denied_text == (
        "tier 'code' is served by sonder_inference with model 'astra-cyber-9b', "
        "which is blocked by runtime policy, and a blocked model does not serve a tier; "
        "ask for 'astra-cyber-9b' by name for a text answer, or change the model that "
        "provider serves for this tier in its configuration."
    )
    with_setting = rule.served_refusal_message(
        "code", "sonder_inference", rule.ModelLaneDecision(ModelLaneStatus.DENIED, "astra-cyber-9b"),
        setting="SONDER_INFERENCE_MODEL")
    assert with_setting.endswith(
        "or change the model sonder_inference serves for tier 'code' (SONDER_INFERENCE_MODEL).")
    unknown = rule.served_refusal_message(
        "code", "openrouter", rule.ModelLaneDecision(ModelLaneStatus.SERVED_UNKNOWN, ""))
    assert "cannot be determined" in unknown and "chat still works" in unknown
    no_provider = rule.served_refusal_message(
        "code", "", rule.ModelLaneDecision(ModelLaneStatus.SERVED_UNKNOWN, ""))
    assert "a non-Ollama provider" in no_provider
