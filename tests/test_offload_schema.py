"""Schema-constrained offload.

Three separate guarantees are under test here, and they are deliberately not the
same mechanism:

1. the schema is handed to Ollama as the decoder-side ``format`` constraint,
2. the text that comes back is validated against that schema again, in-process,
   because a constraint applied by the backend we asked is a claim rather than
   evidence, and
3. the *coverage* of (2) is reported, because the in-process verifier checks a
   strict subset of JSON Schema and silence from a partial check must never read
   as a clean bill of health.

A violation of (2) is a hard failure. Nothing in this path may repair, coerce or
silently re-ask for a response that did not match -- rejecting bad output is the
entire value of asking for a schema in the first place.

A *gap* in (2) is not a failure: Ollama's decoder still applied the whole schema,
so the call keeps working. It is disclosed instead. The tests below pin the
difference, and pin that the disclosure is derived from what the verifier
actually traverses -- so that an absence (a `$ref`, a missing `"type"`) reports
as unverified rather than as clean.
"""
import json

import pytest

import json_schema_verifier
import learning_health
import server
from sonder_runtime.domain.memory import rules as reward_rules


SCHEMA = {
    "type": "object",
    "required": ["name", "age"],
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
}
GOOD = {"name": "ada", "age": 36}


def _capture(monkeypatch, reply):
    """Answer every model request with `reply`, keeping the posted payloads."""
    seen = {"payloads": []}

    def fake_post(path, payload, timeout=None, **_kwargs):
        seen["payloads"].append(payload)
        seen["payload"] = payload
        return {"message": {"content": reply}}

    monkeypatch.setattr(server, "_post", fake_post)
    return seen


def _learning(monkeypatch, interaction_id="abc123"):
    """Route offload through the learning path with a stub store."""
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(server, "_open_db", lambda: Conn())
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: True)
    monkeypatch.setattr(server, "resolve_sonder_model", lambda strict=False: "sonder")
    monkeypatch.setattr(
        server.orchestrator,
        "run_with_learning",
        lambda conn, prompt, tier, gen, **kwargs: (gen(prompt), interaction_id),
    )


# --- the schema reaches Ollama ------------------------------------------------

def test_schema_is_forwarded_as_the_ollama_format_constraint(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert seen["payload"]["format"] == SCHEMA
    assert json.loads(out) == GOOD


def test_learning_offload_forwards_the_schema_too(monkeypatch):
    _learning(monkeypatch)
    seen = _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert seen["payload"]["format"] == SCHEMA
    assert server.parse_interaction_id(out) == "abc123"


def test_make_generate_forwards_the_schema(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GOOD))
    server._make_generate("local", "", 0.2, 32, 2048, schema=SCHEMA)("hi")
    assert seen["payload"]["format"] == SCHEMA


def test_schema_may_be_passed_as_an_already_parsed_object(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GOOD))
    assert json.loads(
        server._offload_impl("describe ada", tier="fast", learn=False, schema=SCHEMA)
    ) == GOOD
    assert seen["payload"]["format"] == SCHEMA


# --- absence of a schema changes nothing --------------------------------------

def test_without_a_schema_no_format_key_is_sent_and_the_text_is_untouched(monkeypatch):
    seen = _capture(monkeypatch, "not json at all")
    out = server.offload("describe ada", tier="fast", learn=False)
    assert "format" not in seen["payload"]
    assert out == "not json at all"


def test_without_a_schema_the_learning_path_is_unchanged(monkeypatch):
    _learning(monkeypatch)
    seen = _capture(monkeypatch, "not json at all")
    out = server.offload("describe ada", tier="code", learn=True)
    assert "format" not in seen["payload"]
    assert server.parse_interaction_id(out) == "abc123"
    assert out.startswith("not json at all")


def test_make_generate_omits_format_without_a_schema(monkeypatch):
    seen = _capture(monkeypatch, "plain text")
    assert server._make_generate("local", "", 0.2, 32, 2048)("hi") == "plain text"
    assert "format" not in seen["payload"]


# --- a violation is a hard, specific failure ----------------------------------

def test_wrong_type_failure_names_the_offending_path(monkeypatch):
    _capture(monkeypatch, json.dumps({"name": "ada", "age": "thirty-six"}))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert "$.age" in out
    assert "expected type integer" in out


def test_missing_required_key_failure_names_the_key(monkeypatch):
    _capture(monkeypatch, json.dumps({"name": "ada"}))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert "missing required key 'age'" in out


def test_a_nested_violation_names_the_nested_path(monkeypatch):
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
        },
    }
    _capture(monkeypatch, json.dumps({"items": [1, "two", 3]}))
    out = server.offload(
        "list them", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert "$.items[1]" in out


def test_a_non_json_response_under_a_schema_is_rejected(monkeypatch):
    _capture(monkeypatch, "Sure! Here is the object you asked for.")
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert "not valid JSON" in out


def test_a_violation_is_never_repaired_or_silently_re_asked(monkeypatch):
    seen = _capture(monkeypatch, json.dumps({"name": "ada"}))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    # One request, one verdict: no second attempt, and the partial object is
    # not handed back as though it had passed.
    assert len(seen["payloads"]) == 1
    assert not out.startswith("{")


def test_the_learning_path_rejects_a_violation_too(monkeypatch):
    _learning(monkeypatch)
    _capture(monkeypatch, json.dumps({"name": "ada", "age": "thirty-six"}))
    out = server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert "$.age" in out


def test_a_malformed_schema_argument_never_reaches_the_model(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema="{not json",
    )
    assert out.startswith("ERROR:")
    assert "schema" in out
    assert seen["payloads"] == []


def test_a_non_object_schema_argument_is_refused(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(["nope"]),
    )
    assert out.startswith("ERROR:")
    assert seen["payloads"] == []


# --- a rejection is filed as caller-judged evidence ---------------------------

def _outcomes(monkeypatch):
    """Capture the outcome rows the offload path files."""
    rows = []
    monkeypatch.setattr(
        server, "_record_outcome_signal",
        lambda interaction_id, signal: rows.append((interaction_id, signal)),
    )
    return rows


def test_rejected_lands_in_the_caller_judged_population(monkeypatch):
    # The point of filing this at all: `failed` would bury a real caller-facing
    # rejection in the self-graded curriculum's thousands of autograded rows,
    # where the only quality figure anyone should trust cannot see it.
    assert "rejected" in reward_rules.VALID_SIGNALS
    assert "rejected" not in learning_health._AUTOGRADED_SIGNALS
    assert not reward_rules.reward_is_good("rejected")


def test_a_schema_violation_is_filed_as_a_rejected_outcome(monkeypatch):
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-violation")
    _capture(monkeypatch, json.dumps({"name": "ada", "age": "thirty-six"}))
    out = server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert rows == [("iid-violation", "rejected")]


def test_an_unparseable_response_is_filed_as_rejected_too(monkeypatch):
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-garbage")
    _capture(monkeypatch, "Sure! Here is the object you asked for.")
    server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert rows == [("iid-garbage", "rejected")]


def test_a_conforming_response_files_nothing(monkeypatch):
    # Matching the requested shape is not the same as being a good answer, and
    # the caller has not judged it yet. Filing `accepted` here would inflate the
    # reviewed rate on evidence that never measured quality.
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-good")
    _capture(monkeypatch, json.dumps(GOOD))
    server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert rows == []


def test_an_unschemaed_call_files_nothing(monkeypatch):
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-plain")
    _capture(monkeypatch, "not json at all")
    server.offload("describe ada", tier="code", learn=True)
    assert rows == []


def test_the_non_learning_path_has_no_interaction_to_judge(monkeypatch):
    # learn=False captures no interaction, so there is no row to attach an
    # outcome to; inventing one would be a fabricated judgement.
    rows = _outcomes(monkeypatch)
    _capture(monkeypatch, json.dumps({"name": "ada"}))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert out.startswith("ERROR:")
    assert rows == []


def test_a_failed_outcome_write_never_masks_the_schema_failure(monkeypatch):
    def boom(interaction_id, signal):
        raise RuntimeError("outcome store unavailable")

    monkeypatch.setattr(server, "_record_outcome_signal", boom)
    _learning(monkeypatch)
    _capture(monkeypatch, json.dumps({"name": "ada"}))
    out = server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    )
    assert "missing required key 'age'" in out


@pytest.mark.parametrize("empty", ["", "   "])
def test_a_blank_schema_argument_means_no_schema(monkeypatch, empty):
    seen = _capture(monkeypatch, "not json at all")
    assert server.offload(
        "describe ada", tier="fast", learn=False, schema=empty,
    ) == "not json at all"
    assert "format" not in seen["payload"]


# --- C1: a legal schema the verifier cannot traverse must not crash the tool ---

UNION = {"type": ["string", "null"]}
REF = {
    "$ref": "#/$defs/Thing",
    "$defs": {"Thing": {"type": "object", "required": ["name"]}},
}


def test_a_type_union_schema_does_not_escape_as_an_uncaught_exception(monkeypatch):
    # {"type": ["string", "null"]} is legal JSON Schema and Ollama's `format`
    # accepts it. The in-process verifier indexes its type table with the raw
    # value, so a list used to raise TypeError straight out of the MCP tool --
    # after the request had already been posted.
    seen = _capture(monkeypatch, json.dumps("ada"))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(UNION),
    )
    assert seen["payload"]["format"] == UNION
    assert isinstance(out, str)


def test_a_type_union_is_reported_as_unverified_rather_than_rejected(monkeypatch):
    # Capability is kept: the decoder still constrained the whole schema, so the
    # call succeeds. What changes is that it stops claiming to have checked it.
    _capture(monkeypatch, json.dumps("ada"))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(UNION),
    )
    assert not out.startswith("ERROR:")
    assert "schema_unverified" in out
    assert '"ada"' in out


def test_a_verifier_that_cannot_traverse_at_all_never_escapes(monkeypatch):
    def explode(data, schema):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(json_schema_verifier, "validate", explode)
    _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert isinstance(out, str)
    assert "schema_unverified" in out


def test_an_unexpected_check_failure_still_files_a_rejection(monkeypatch):
    # The learning path caught only ModelCallError, so anything else skipped the
    # `rejected` write -- making the failure invisible to the exact population
    # this feature exists to feed.
    rows = _outcomes(monkeypatch)

    def explode(text, schema):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(server, "_require_schema_match", explode)
    _learning(monkeypatch, interaction_id="iid-unforeseen")
    _capture(monkeypatch, json.dumps(GOOD))
    with pytest.raises(RuntimeError):
        server.offload(
            "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
        )
    assert rows == [("iid-unforeseen", "rejected")]


# --- I1: coverage is derived from what the verifier actually traversed --------

def test_an_unfollowed_ref_is_reported_as_unverified_not_as_clean(monkeypatch):
    # The end-to-end case: a $ref node carries no "type", the verifier's type
    # defaults to "any", and a wholly wrong-shaped reply came back "verified".
    _capture(monkeypatch, json.dumps("totally the wrong shape"))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(REF),
    )
    assert "schema_unverified" in out
    assert "$ref" in out


def test_properties_without_an_explicit_object_type_are_unverified(monkeypatch):
    # No "type" means the verifier accepts any value and never applies
    # required/properties at all -- an absence, which a static keyword list
    # would have scored as full coverage.
    schema = {"required": ["name"], "properties": {"name": {"type": "string"}}}
    _capture(monkeypatch, json.dumps(42))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert "schema_unverified" in out


def test_extended_keyword_violation_is_named_at_the_node_that_carries_it(monkeypatch):
    schema = {
        "type": "object",
        "required": ["color"],
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
    }
    _capture(monkeypatch, json.dumps({"color": "purple"}))
    out = server.offload(
        "pick one", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert out.startswith("ERROR:")
    assert "$.color" in out
    assert "enum" in out


def test_an_unknown_keyword_is_unverified_by_default(monkeypatch):
    # Coverage is the complement of what the verifier enforces, not a list of
    # keywords someone remembered to enumerate, so a keyword nobody anticipated
    # is reported rather than assumed safe.
    schema = {"type": "string", "x-invented-by-nobody": True}
    _capture(monkeypatch, json.dumps("ada"))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert "x-invented-by-nobody" in out


def test_a_gap_is_reported_at_its_nested_path(monkeypatch):
    schema = {
        "type": "object",
        "required": ["rows"],
        "properties": {
            "rows": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
        },
    }
    _capture(monkeypatch, json.dumps({"rows": [-5]}))
    out = server.offload(
        "list them", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert "$.rows[0]" in out
    assert "minimum" in out


def test_a_fully_supported_schema_is_returned_byte_for_byte(monkeypatch):
    # The common case must stay clean, parseable JSON with nothing appended.
    _capture(monkeypatch, json.dumps(GOOD))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    )
    assert out == json.dumps(GOOD)
    assert json.loads(out) == GOOD


def test_a_violation_message_is_complete_when_all_keywords_are_verified(monkeypatch):
    # With both constraints implemented, the error is a complete local check
    # rather than a partial-coverage disclosure.
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "minLength": 3}},
    }
    _capture(monkeypatch, json.dumps({"name": 7}))
    out = server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(schema),
    )
    assert out.startswith("ERROR:")
    assert "$.name: expected type string" in out
    assert "unverified" not in out.casefold()


def test_the_verifier_really_enforces_every_keyword_coverage_claims():
    # Guards the mirror: if json_schema_verifier ever stops enforcing one of
    # these, coverage would keep claiming it and this fails.
    assert server._VERIFIED_SCHEMA_KEYWORDS == frozenset(
        {"type", "required", "properties", "items", "enum", "minimum",
         "minLength", "additionalProperties", "uniqueItems", "pattern"}
    )
    assert json_schema_verifier.validate(1, {"type": "string"})
    assert json_schema_verifier.validate({}, {"type": "object", "required": ["a"]})
    assert json_schema_verifier.validate(
        {"a": 1}, {"type": "object", "properties": {"a": {"type": "string"}}},
    )
    assert json_schema_verifier.validate(
        ["x"], {"type": "array", "items": {"type": "integer"}},
    )


@pytest.mark.parametrize(
    "schema, datum",
    [
        ({"type": "string", "enum": ["red"]}, "purple"),
        ({"type": "integer", "minimum": 0}, -5),
        ({"type": "string", "minLength": 3}, "a"),
        ({"type": "object", "additionalProperties": False}, {"a": 1}),
        ({"type": "array", "uniqueItems": True}, [1, 1]),
        ({"type": "string", "pattern": "^a$"}, "zzz"),
    ],
)
def test_the_verifier_really_enforces_extended_schema_keywords(schema, datum):
    # The verifier now implements these constraints, so a violating datum must
    # be rejected rather than merely carrying an "unverified" disclosure.
    assert json_schema_verifier.validate(datum, schema)
    gaps = server._schema_coverage_gaps(datum, schema)
    assert not gaps, schema


def test_coverage_reports_rather_than_raises_on_a_malformed_schema_node():
    assert server._schema_coverage_gaps(1, "not a schema node")


# --- I2: both paths record a schema violation the same way --------------------

def _model_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.activity_tracker, "record_model_call",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def test_both_paths_record_a_schema_violation_identically(monkeypatch):
    # The model call itself succeeded on both paths -- HTTP 200, tokens back.
    # Recording it as failed on one path and successful on the other was an
    # undisclosed asymmetry in the activity feed. The bad verdict is carried by
    # the `rejected` outcome, not by lying about the transport.
    bad = json.dumps({"name": "ada", "age": "thirty-six"})

    plain = _model_calls(monkeypatch)
    _capture(monkeypatch, bad)
    assert server.offload(
        "describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA),
    ).startswith("ERROR:")

    learned = _model_calls(monkeypatch)
    _learning(monkeypatch)
    _capture(monkeypatch, bad)
    assert server.offload(
        "describe ada", tier="code", learn=True, schema=json.dumps(SCHEMA),
    ).startswith("ERROR:")

    assert [call["ok"] for call in plain] == [call["ok"] for call in learned] == [True]


def test_a_schema_violation_keeps_the_offending_text_visible_for_debugging(monkeypatch):
    calls = _model_calls(monkeypatch)
    bad = json.dumps({"name": "ada", "age": "thirty-six"})
    _capture(monkeypatch, bad)
    server.offload("describe ada", tier="fast", learn=False, schema=json.dumps(SCHEMA))
    assert calls[0]["response_preview"] == bad
