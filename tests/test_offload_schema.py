"""Schema-constrained offload.

Two separate guarantees are under test here, and they are deliberately not the
same mechanism:

1. the schema is handed to Ollama as the decoder-side ``format`` constraint, and
2. the text that comes back is validated against that schema again, in-process,
   because a constraint applied by the backend we asked is a claim rather than
   evidence.

A violation of (2) is a hard failure. Nothing in this path may repair, coerce or
silently re-ask for a response that did not match -- rejecting bad output is the
entire value of asking for a schema in the first place.
"""
import json

import pytest

import server


SCHEMA = {
    "type": "object",
    "required": ["name", "age"],
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
}
GOOD = {"name": "ada", "age": 36}


def _capture(monkeypatch, reply):
    """Answer every model request with `reply`, keeping the posted payloads."""
    seen = {"payloads": []}

    def fake_post(path, payload, timeout=None):
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


@pytest.mark.parametrize("empty", ["", "   "])
def test_a_blank_schema_argument_means_no_schema(monkeypatch, empty):
    seen = _capture(monkeypatch, "not json at all")
    assert server.offload(
        "describe ada", tier="fast", learn=False, schema=empty,
    ) == "not json at all"
    assert "format" not in seen["payload"]
