"""The natural model-request grammar lives in the domain; root names stay aliases."""
import server
from sonder_runtime.domain import natural_model_request as grammar


def _no_profile(_profile):
    return None, "unknown"


def _no_catalog(_selector, _prompt):
    return None


def test_root_names_are_identity_preserving_aliases():
    assert server.FANOUT_SELECTION_PROFILES is grammar.FANOUT_SELECTION_PROFILES
    assert (
        server._INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES
        is grammar.INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES
    )
    assert (
        server._is_interpreter_like_bare_model_selector
        is grammar.is_interpreter_like_bare_model_selector
    )


def test_profile_scope_reports_the_message_and_the_root_wraps_it():
    assert grammar.fanout_profile_scope("") == (None, None)
    assert grammar.fanout_profile_scope(" Healthy-Local-Chat ") == ("local", None)
    assert grammar.fanout_profile_scope("loaded-local-chat") == ("local", None)
    assert grammar.fanout_profile_scope("bogus") == (None, grammar.UNKNOWN_FANOUT_PROFILE_MESSAGE)
    assert server._fanout_profile_scope("healthy-chat") == ("all", None)
    scope, error = server._fanout_profile_scope("bogus")
    assert scope is None
    assert isinstance(error, server.ModelCallError)
    assert error.kind == "configuration"
    assert error.detail == grammar.UNKNOWN_FANOUT_PROFILE_MESSAGE


def test_interpreter_like_tags_are_command_names_not_model_selectors():
    assert grammar.is_interpreter_like_bare_model_selector("python:3.12")
    assert grammar.is_interpreter_like_bare_model_selector("Node:20")
    assert not grammar.is_interpreter_like_bare_model_selector("python")
    assert not grammar.is_interpreter_like_bare_model_selector("phi4:latest")


def test_the_grammar_recognizes_whole_turn_forms_through_injected_callables():
    def parse(text):
        return grammar.natural_model_request(
            text, profile_scope=grammar.fanout_profile_scope, bare_tagged_request=_no_catalog,
        )

    assert parse("ask all local models to explain the cache") == {
        "kind": "fanout", "scope": "local", "prompt": "explain the cache",
    }
    assert parse("run healthy cloud chat models: summarize") == {
        "kind": "fanout", "scope": "cloud", "profile": "healthy-cloud-chat", "prompt": "summarize",
    }
    assert parse("ask the code and reasoning models to answer: why") == {
        "kind": "ensemble", "tiers": "code,reasoning", "prompt": "why",
    }
    assert parse("use model phi4:latest: hello there") == {
        "kind": "model", "model": "phi4:latest", "prompt": "hello there",
    }
    assert parse("use the best model to fix this") is None
    assert parse("run ubuntu:24.04: reproduce the crash") is None
    assert parse("please summarize the file") is None


def test_the_grammar_defers_terse_tags_to_the_injected_catalog_resolver():
    seen = []

    def resolver(selector, prompt):
        seen.append((selector, prompt))
        return {"kind": "model", "model": "resolved:" + selector, "prompt": prompt.strip()}

    out = grammar.natural_model_request(
        "run gemma3:12b to explain", profile_scope=_no_profile, bare_tagged_request=resolver,
    )
    assert out == {"kind": "model", "model": "resolved:gemma3:12b", "prompt": "explain"}
    assert seen == [("gemma3:12b", "explain")]


def test_root_wrapper_keeps_the_interpreter_guard_and_fanout_routing():
    assert server.natural_model_request("run python:3.12 to reproduce this") is None
    assert server.natural_model_request("ask all models: hi") == {
        "kind": "fanout", "scope": "all", "prompt": "hi",
    }
