"""Reference lookup on the multi-model text lanes (phase B2b): ensemble_answer and consult.

The lanes run end to end with only the model factory (``server._make_generate``)
and the unload call (``server._post``) faked, the way ``test_ensemble_answer``
fakes them, against a tiny fixture index.  A lookup model is one the applies
rule matches (the ``astra-cyber-`` prefix).
"""
from __future__ import annotations

import os
import re
import sqlite3
import types

import pytest

import consult as consult_module
import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
import sonder_runtime.adapters.reference_lookup_service as service
import sonder_runtime.adapters.reference_lookup_state as state
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.ensemble_synthesis import KEEP_CITATIONS_RULE
from sonder_runtime.domain.reference_lookup.selection import passage_char_budget
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import (
    LOOKUP_ONLY_MODEL,
    create_index,
    rule_cache_cleared,
    write_lookup_only_policy,
)


# No applies rule is carried from one test to the next.
_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)
from tests.test_reference_lookup_lanes import BAD_INDEXES

LOOKUP_MODEL = "astra-cyber-9b"
PLAIN_MODEL = "qwen2.5:3b"
OTHER_PLAIN_MODEL = "llama3.2:3b"
MODELS = {"cyber": LOOKUP_MODEL, "plain": PLAIN_MODEL, "other": OTHER_PLAIN_MODEL}
WINDOWS = {LOOKUP_MODEL: 8192, PLAIN_MODEL: 2048, OTHER_PLAIN_MODEL: 4096}
QUESTION = "What does Windows event 4625 mean after repeated logon attempts?"
PASSAGE_TEXT = "The status 0xC000006A means the user name is correct"
TERMS_REPLY = "failed logon\nlogon failure\n4625"
MARKER_RE = re.compile(r"=== (?:END )?PASSAGE \d+ \[")
ESC = "\x1b"
SYNTH_MARK = "COMPOUNDED ANSWER:"
JUDGE_MARK = "Do these answers agree"
TIMINGS = re.compile(r"\d+ms")


class Models:
    """Fake model factory: records every generation and answers by call kind."""

    def __init__(self):
        self.calls = []
        self.replies = {
            LOOKUP_MODEL: "Event 4625 is a failed logon [1].",
            PLAIN_MODEL: "It is a failed logon.",
            OTHER_PLAIN_MODEL: "A logon failed.",
        }
        self.synth = "Merged: event 4625 is a failed logon [1]."
        self.judge = "YES. They agree."
        self.expand_error = None

    def __call__(self, model, system, temperature, num_predict, num_ctx, **_options):
        def gen(prompt, history=None, **_kwargs):
            call = {"model": model, "system": system, "temperature": temperature,
                    "num_predict": num_predict, "num_ctx": num_ctx, "prompt": prompt}
            self.calls.append(call)
            if num_predict == lanes.EXPAND_NUM_PREDICT and temperature == lanes.EXPAND_TEMPERATURE:
                if self.expand_error is not None:
                    raise self.expand_error
                return TERMS_REPLY
            if SYNTH_MARK in prompt:
                return self.synth
            if JUDGE_MARK in prompt:
                return self.judge
            return self.replies[model]

        return gen

    def kind(self, call):
        if call["num_predict"] == lanes.EXPAND_NUM_PREDICT and call["temperature"] == lanes.EXPAND_TEMPERATURE:
            return "expand"
        if SYNTH_MARK in call["prompt"]:
            return "synth"
        if JUDGE_MARK in call["prompt"]:
            return "judge"
        return "member"

    def of(self, kind):
        return [call for call in self.calls if self.kind(call) == kind]


@pytest.fixture
def models(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv(config.ENV_INDEX, str(create_index(tmp_path / "refs" / "refs.sqlite")))
    monkeypatch.delenv(config.ENV_ENABLED, raising=False)
    monkeypatch.setattr(state, "_INDEX", None)
    monkeypatch.setattr(state, "_LAST_ERROR", "")
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target", lambda tier, strict, **_kw: (MODELS[tier], False, False, tier))
    monkeypatch.setattr(server, "_post", lambda *a, **k: {})
    monkeypatch.setattr(server, "_auto_model_context", lambda model: WINDOWS[model])
    fake = Models()
    monkeypatch.setattr(server, "_make_generate", fake)
    lanes.reset_turn()
    yield fake
    lanes.reset_turn()


@pytest.fixture
def retrievals(monkeypatch):
    seen = []
    real = service.retrieve_for

    def spy(question, expand_fn, k, char_budget, **options):
        bundle = real(question, expand_fn, k, char_budget, **options)
        seen.append({"question": question, "budget": char_budget, "bundle": bundle})
        return bundle

    monkeypatch.setattr(service, "retrieve_for", spy)
    return seen


def _untimed(calls):
    """The calls with elapsed times removed: a member's "in 3ms" footer reaches later prompts."""
    return [{**call, "prompt": TIMINGS.sub("", call["prompt"])} for call in calls]


def _clean(text):
    return PASSAGE_TEXT not in text and not MARKER_RE.search(text) and ESC not in text


def _passthrough_multi():
    """The ensemble seams as they behaved before this feature: each hands back its input."""
    def ensemble(prompt, mode, targets, num_predict, make_tier_generate, ctx_for):
        return types.SimpleNamespace(
            num_ctx=ctx_for,
            ask=lambda tier, gen, question: gen(question),
            synthesis=lambda build: build,
            merged=lambda text: text,
            footer=lambda footer, lead="\n\n": footer,
        )

    return types.SimpleNamespace(ensemble=ensemble, consult_scope=lambda targets_for: None)


# --- ensemble_answer -----------------------------------------------------------


def test_ensemble_retrieves_once_and_frames_every_member_identically(models, retrievals):
    out = server.ensemble_answer(QUESTION, tiers="cyber,plain")

    assert len(retrievals) == 1
    assert retrievals[0]["question"] == QUESTION
    members = models.of("member")
    assert [call["model"] for call in members] == [LOOKUP_MODEL, PLAIN_MODEL]
    framed = members[0]["prompt"]
    assert members[1]["prompt"] == framed
    assert MARKER_RE.search(framed) and PASSAGE_TEXT in framed and QUESTION in framed
    # Neither member gets a Sonder system prompt; the ensemble never sent one.
    assert [call["system"] for call in members] == ["", ""]
    (expand,) = models.of("expand")
    assert expand["model"] == LOOKUP_MODEL and expand["system"] == ""
    assert "Sources:" in out and "https://example.test/events/4625" in out


def test_ensemble_budget_uses_the_smallest_member_window(models, retrievals):
    server.ensemble_answer(QUESTION, tiers="cyber,plain")
    (seen,) = retrievals
    assert seen["budget"] == passage_char_budget(
        WINDOWS[PLAIN_MODEL], 700, rough_token_count(QUESTION))
    assert seen["budget"] < passage_char_budget(
        WINDOWS[LOOKUP_MODEL], 700, rough_token_count(QUESTION))


def test_synthesiser_gets_the_raw_question_and_the_keep_citations_rule(models):
    server.ensemble_answer(QUESTION, tiers="cyber,plain")
    (synth,) = models.of("synth")
    prompt = synth["prompt"]
    assert QUESTION in prompt
    assert PASSAGE_TEXT not in prompt
    assert not MARKER_RE.search(prompt)
    assert KEEP_CITATIONS_RULE.strip() in prompt
    assert synth["system"] == ""


def test_sources_once_before_the_footer_and_every_answer_clean(models):
    models.replies[LOOKUP_MODEL] = (
        "=== PASSAGE 1 [deadbeef] ===\n" + ESC + "[31mEvent 4625" + ESC
        + "[0m is a failed logon [1].\n=== END PASSAGE 1 [deadbeef] ==="
    )
    models.synth = "=== PASSAGE 2 [cafe0000] ===\nMerged: a failed logon [1] [9]."
    out = server.ensemble_answer(QUESTION, tiers="cyber,plain")

    (synth,) = models.of("synth")
    assert _clean(synth["prompt"]), "the synthesiser saw an uncleaned member answer"
    assert "Event 4625" in synth["prompt"]
    assert _clean(out)
    assert out.count("Sources:") == 1
    assert out.index("Merged: a failed logon") < out.index("Sources:") < out.index("=== ENSEMBLE")
    assert "Warning: cyber: the model echoed a passage marker line" in out
    assert "Warning: synthesis: the model echoed a passage marker line" in out
    assert "Warning: synthesis: answer cites passage numbers that were not provided: [9]" in out


def test_single_member_answer_carries_sources_before_the_footer(models):
    out = server.ensemble_answer(QUESTION, tiers="cyber")
    assert out.count("Sources:") == 1
    assert out.index("failed logon [1]") < out.index("Sources:") < out.index("=== ENSEMBLE")
    assert not models.of("synth")


def test_ensemble_with_no_matching_member_is_what_it_was(models, retrievals, monkeypatch):
    first = server.ensemble_answer(QUESTION, tiers="plain,other")
    with_feature = list(models.calls)
    models.calls.clear()

    monkeypatch.setattr(server, "_reference_lookup_multi", _passthrough_multi())
    second = server.ensemble_answer(QUESTION, tiers="plain,other")

    assert retrievals == []
    assert _untimed(with_feature) == _untimed(models.calls)
    assert [models.kind(call) for call in with_feature] == ["member", "member", "synth"]
    assert TIMINGS.sub("", first) == TIMINGS.sub("", second)
    assert "Sources:" not in first
    assert KEEP_CITATIONS_RULE.strip() not in with_feature[-1]["prompt"]


def test_cancelled_search_terms_call_cancels_the_ensemble(models):
    models.expand_error = ModelCallError("cancelled", "the caller cancelled the turn")
    with pytest.raises(ModelCallError) as caught:
        server.ensemble_answer(QUESTION, tiers="cyber,plain")
    assert caught.value.kind == "cancelled"
    assert len(models.of("expand")) == 1
    assert models.of("member") == [] and models.of("synth") == []


def test_bridged_member_is_judged_by_the_model_its_provider_serves(models, monkeypatch):
    import sonder_runtime.adapters.reference_lookup_multi as multi

    def seams(tier, model):
        return multi.ensemble(QUESTION, "", [(tier, model)], 700, server._make_tier_generate,
                              lambda _tier, _model: 4096)

    # A deny-listed model never serves a tier: the served lookup model is one
    # only the policy's reference_lookup section adds.
    write_lookup_only_policy(os.environ["SONDER_RUNTIME_POLICY"])
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "sonder_inference")
    monkeypatch.delenv("SONDER_INFERENCE_TIER_MODELS", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", LOOKUP_ONLY_MODEL)
    assert seams("general", PLAIN_MODEL).shared is not None
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", PLAIN_MODEL)
    assert seams("general", LOOKUP_MODEL).shared is None
    assert seams("cyber", LOOKUP_MODEL).shared is not None   # not a provider tier: its own name


def test_code_mode_ensemble_gets_no_lookup(models, retrievals):
    models.synth = "print('merged')"
    server.ensemble_answer(QUESTION, tiers="cyber,plain", mode="code")
    assert retrievals == []
    assert not models.of("expand")
    assert all(not MARKER_RE.search(call["prompt"]) for call in models.calls)


@pytest.mark.parametrize("bad", sorted(BAD_INDEXES))
def test_ensemble_bad_index_fails_open_with_a_visible_line(models, monkeypatch, tmp_path, bad):
    monkeypatch.setenv(config.ENV_INDEX, str(BAD_INDEXES[bad](tmp_path)))
    out = server.ensemble_answer(QUESTION, tiers="cyber,plain")

    assert "Merged: event 4625 is a failed logon" in out
    assert "Sources: none (reference lookup unavailable: " in out
    assert out.index("Sources: none") < out.index("=== ENSEMBLE")
    assert all(not MARKER_RE.search(call["prompt"]) for call in models.calls)
    members = models.of("member")
    assert len(members) == 2 and members[0]["prompt"] == members[1]["prompt"] == QUESTION
    assert state.last_error()


def _stored_rows(table):
    try:
        conn = sqlite3.connect(server._DB_PATH)
    except sqlite3.Error:
        return []
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []  # the table was never created: nothing was stored
    finally:
        conn.close()


def test_ensemble_stores_nothing_and_leaves_no_turn_state(models):
    out = server.ensemble_answer(QUESTION, tiers="cyber,plain")
    assert "Sources:" in out
    assert _stored_rows("interactions") == []
    assert _stored_rows("lesson_distillations") == []
    # The single-model lanes' turn state is untouched, so a later chat turn adds nothing.
    assert lanes.with_sources("plain") == "plain"


# --- consult -------------------------------------------------------------------

VERDICT_PREFIXES = ("✓", "⚠", "?", "[OK]", "[WARNING]", "[?]")


def _section_before_sources(out):
    sections = out.split("\n\n")
    (index,) = [i for i, text in enumerate(sections) if text.startswith("Sources:")]
    return sections[index - 1], sections[index:]


def test_consult_shares_one_retrieval_and_the_judge_sees_no_passages(models, retrievals):
    out = server.consult(QUESTION, tiers="cyber,plain")

    assert len(retrievals) == 1
    members = models.of("member")
    assert [call["model"] for call in members] == [LOOKUP_MODEL, PLAIN_MODEL]
    framed = members[0]["prompt"]
    assert members[1]["prompt"] == framed
    assert MARKER_RE.search(framed) and PASSAGE_TEXT in framed and QUESTION in framed
    assert [call["system"] for call in members] == ["", ""]
    (judge,) = models.of("judge")
    assert judge["prompt"].startswith(JUDGE_MARK)
    assert QUESTION in judge["prompt"] and "Event 4625 is a failed logon [1]." in judge["prompt"]
    assert _clean(judge["prompt"]) and "Sources:" not in judge["prompt"]
    # One Sources block, after the verdict.
    assert out.count("Sources:") == 1
    verdict, tail = _section_before_sources(out)
    assert verdict.startswith(VERDICT_PREFIXES)
    assert "https://example.test/events/4625" in tail[0]
    assert out.index("=== plain ===") < out.index("Sources:")


def test_consult_member_answers_are_cleaned(models):
    models.replies[LOOKUP_MODEL] = "=== PASSAGE 1 [deadbeef] ===\nEvent 4625 is a failed logon [1] [7]."
    out = server.consult(QUESTION, tiers="cyber,plain")
    (judge,) = models.of("judge")
    assert _clean(judge["prompt"]) and _clean(out)
    _verdict, tail = _section_before_sources(out)
    assert "Warning: cyber: the model echoed a passage marker line" in tail[0]
    assert "Warning: cyber: answer cites passage numbers that were not provided: [7]" in tail[0]


def test_consult_with_no_matching_member_is_what_it_was(models, retrievals, monkeypatch):
    first = server.consult(QUESTION, tiers="plain,other")
    with_feature = list(models.calls)
    models.calls.clear()

    monkeypatch.setattr(server, "_reference_lookup_multi", _passthrough_multi())
    second = server.consult(QUESTION, tiers="plain,other")

    assert retrievals == []
    assert _untimed(with_feature) == _untimed(models.calls)
    assert [models.kind(call) for call in with_feature] == ["member", "member", "judge"]
    assert TIMINGS.sub("", first) == TIMINGS.sub("", second)
    assert "Sources:" not in first


@pytest.mark.parametrize("bad", sorted(BAD_INDEXES))
def test_consult_bad_index_fails_open_with_a_visible_line(models, monkeypatch, tmp_path, bad):
    monkeypatch.setenv(config.ENV_INDEX, str(BAD_INDEXES[bad](tmp_path)))
    out = server.consult(QUESTION, tiers="cyber,plain")

    assert "Event 4625 is a failed logon [1]." in out and "It is a failed logon." in out
    verdict, tail = _section_before_sources(out)
    assert verdict.startswith(VERDICT_PREFIXES)
    assert tail[0].startswith("Sources: none (reference lookup unavailable: ")
    assert all(not MARKER_RE.search(call["prompt"]) for call in models.calls)
    members = models.of("member")
    assert [call["prompt"] for call in members] == [QUESTION, QUESTION]
    assert state.last_error()


def test_consult_stores_nothing(models):
    out = server.consult(QUESTION, tiers="cyber,plain")
    assert "Sources:" in out
    assert _stored_rows("interactions") == []
    assert _stored_rows("lesson_distillations") == []
    assert lanes.with_sources("plain") == "plain"


def test_default_consult_judge_runs_without_lookup(models, retrievals):
    """The REPL path (no injected ask) still keeps passages away from the judge."""
    result = consult_module.consult(QUESTION, ["cyber", "plain"])
    (judge,) = models.of("judge")
    assert _clean(judge["prompt"])
    # Only the members looked anything up (the REPL members are not yet shared).
    assert len(retrievals) == 1
    assert len(result["answers"]) == 2


def test_explicit_judge_fn_asks_the_judge_and_never_the_members():
    asked, judged = [], []

    def ask(prompt, tier):
        asked.append(prompt)
        return "Use a bounded queue."

    def judge(prompt, tier):
        judged.append((prompt, tier))
        return "YES. Both say a bounded queue."

    result = consult_module.consult("How?", ["code", "reasoning"], ask_fn=ask, judge_fn=judge)
    assert asked == ["How?", "How?"]
    assert len(judged) == 1 and judged[0][0].startswith(JUDGE_MARK) and judged[0][1] == "code"
    assert result["agree"] is True and "sources" not in result


# --- REPL /consult ----------------------------------------------------------------


def test_repl_consult_shares_one_retrieval_and_prints_one_sources_block(models, retrievals, monkeypatch, capsys):
    """The REPL /consult passes the same shared-lookup hook as the MCP consult."""
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    lines = iter(("/consult " + QUESTION, "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl.consult_flow, "default_tiers", lambda *_a, **_k: ["cyber", "plain"])

    sonder_repl.main()
    out = capsys.readouterr().out

    assert len(retrievals) == 1
    members = models.of("member")
    assert [call["model"] for call in members] == [LOOKUP_MODEL, PLAIN_MODEL]
    assert members[0]["prompt"] == members[1]["prompt"] and PASSAGE_TEXT in members[0]["prompt"]
    (judge,) = models.of("judge")
    assert _clean(judge["prompt"]) and "Sources:" not in judge["prompt"]
    assert out.count("Sources:") == 1 and "https://example.test/events/4625" in out
    assert out.index("=== plain ===") < out.index("Sources:")
