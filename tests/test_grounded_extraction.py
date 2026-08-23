"""Extraction that must cite the source it came from.

The measured worst case for the local 7B is being asked to *remember* rather
than *transform*: handed every fact it needs it does well, asked to supply one
it was not given it invents a confident answer (a Win32 ROP2 table came back 10
of 16 rows wrong, presented as fact). Free-text output makes that invisible --
an invented row looks exactly like a recalled one.

The helper under test makes it *mechanically* detectable instead. Every field
must come back with the span of source text that states it, and every span is
checked by literal substring against the source the caller supplied. A model
that invented a value has no real span to point at, so the invention is rejected
by string comparison rather than by anyone reading the answer carefully.

What these tests deliberately pin, because each is a way the check could be
quietly weakened into uselessness:

* the comparison is EXACT -- no whitespace, case or unicode folding. Every
  normalisation widens the set of strings an invented span can match, so there
  are none;
* an empty or blank span is rejected, because it is a substring of every source
  and would satisfy the check vacuously;
* a span the source contains more than once is DISCLOSED as ambiguous, not
  silently resolved to the first hit;
* the source never reaches a cloud tier through this path at all.

What they do NOT claim, and the reason there is no test for it: a span that is
genuinely in the source does not prove the *value* attached to it is supported
by that span. The check is "did you point at real text", not "does that text say
what you claim". See the module docstring.
"""
import json

import pytest

import grounded_extraction
import server


SOURCE = (
    "Ada Lovelace was born in London on 10 December 1815.\n"
    "She wrote the first algorithm intended for a machine.\n"
)
SCHEMA = {
    "type": "object",
    "required": ["name", "birth_year"],
    "properties": {
        "name": {"type": "string"},
        "birth_year": {"type": "integer"},
    },
}
GROUNDED_REPLY = {
    "name": {"value": "Ada Lovelace", "quote": "Ada Lovelace was born in London"},
    "birth_year": {"value": 1815, "quote": "on 10 December 1815"},
}


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
    """Route the call through the learning path with a stub store."""
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


def _outcomes(monkeypatch):
    rows = []
    monkeypatch.setattr(
        server, "_record_outcome_signal",
        lambda interaction_id, signal: rows.append((interaction_id, signal)),
    )
    return rows


# --- the schema is rewritten so every field has to carry its evidence ---------

def test_each_field_is_wrapped_into_a_value_and_a_quote():
    wrapped = grounded_extraction.grounded_schema(SCHEMA)
    assert wrapped["type"] == "object"
    name = wrapped["properties"]["name"]
    assert name["type"] == "object"
    assert sorted(name["required"]) == ["quote", "value"]
    # the caller's own subschema still constrains the value
    assert name["properties"]["value"] == {"type": "string"}
    assert name["properties"]["quote"] == {"type": "string"}
    assert wrapped["properties"]["birth_year"]["properties"]["value"] == {
        "type": "integer"
    }


def test_a_required_field_absent_from_properties_is_refused_not_filtered():
    # Silently dropping the name turned a caller's typo into a clean success:
    # {"required": ["ghost"]} with no matching property produced a schema with
    # no fields at all, and the tool answered {"fields": {}} as though the
    # document had been read and nothing found. The caller believes a question
    # was asked that never was. It also contradicts _parse_schema_arg five
    # hundred lines away, which refuses a malformed schema outright rather than
    # running the call unconstrained while the caller believes otherwise.
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.grounded_schema(
            {
                "type": "object",
                "required": ["ghost"],
                "properties": {"name": {"type": "string"}},
            }
        )
    assert "ghost" in str(excinfo.value)


def test_the_callers_required_list_is_honoured_so_an_absent_fact_can_be_omitted():
    # A field the source never states cannot be grounded, so forcing the model
    # to return one would force it to invent. Optional stays optional.
    wrapped = grounded_extraction.grounded_schema(
        {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "spouse": {"type": "string"}},
        }
    )
    assert wrapped["required"] == ["name"]
    assert set(wrapped["properties"]) == {"name", "spouse"}


def test_a_schema_without_properties_is_refused():
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.grounded_schema({"type": "object"})


def test_a_non_object_schema_is_refused():
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.grounded_schema(["nope"])


# --- the span check itself ----------------------------------------------------

def test_a_span_present_in_the_source_is_accepted_and_located():
    fields = grounded_extraction.verify_grounding(GROUNDED_REPLY, SOURCE)
    assert fields["name"]["value"] == "Ada Lovelace"
    assert fields["name"]["quote"] == "Ada Lovelace was born in London"
    assert fields["name"]["quote_offset"] == 0
    assert fields["name"]["quote_occurrences"] == 1
    assert fields["birth_year"]["value"] == 1815


def test_a_span_absent_from_the_source_is_rejected_and_the_field_is_named():
    reply = dict(GROUNDED_REPLY)
    reply["birth_year"] = {"value": 1852, "quote": "born on 10 December 1852"}
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding(reply, SOURCE)
    assert "birth_year" in str(excinfo.value)
    assert "does not appear in the source" in str(excinfo.value)


def test_every_ungrounded_field_is_named_not_just_the_first():
    reply = {
        "name": {"value": "Grace Hopper", "quote": "Grace Hopper was born"},
        "birth_year": {"value": 1906, "quote": "in 1906"},
    }
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding(reply, SOURCE)
    message = str(excinfo.value)
    assert "name" in message and "birth_year" in message


def test_an_empty_span_is_rejected_rather_than_trivially_satisfying_the_check():
    reply = {"name": {"value": "Ada Lovelace", "quote": ""}}
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding(reply, SOURCE)
    assert "name" in str(excinfo.value)


def test_a_blank_span_is_rejected_too():
    reply = {"name": {"value": "Ada Lovelace", "quote": "  \n "}}
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.verify_grounding(reply, SOURCE)


@pytest.mark.parametrize(
    "quote",
    [
        "ada lovelace was born in london",       # case folded
        "Ada  Lovelace was born in London",      # whitespace collapsed
        "Ada Lovelace was born in London on 10 December 1815. She wrote",  # newline
        "Ada" + chr(0xA0) + "Lovelace was born in London",  # non-breaking space
    ],
)
def test_the_comparison_is_exact_with_no_normalisation(quote):
    # Each of these would pass under a "reasonable" normalisation, and each
    # normalisation admitted here would widen what an invented span can match.
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.verify_grounding({"name": {"value": "x", "quote": quote}}, SOURCE)


def test_a_span_the_source_repeats_is_reported_as_ambiguous_not_resolved_silently():
    source = "Paid 40 USD. Refunded 40 USD."
    fields = grounded_extraction.verify_grounding(
        {"amount": {"value": 40, "quote": "40 USD"}}, source
    )
    assert fields["amount"]["quote_occurrences"] == 2
    assert fields["amount"]["quote_offset"] == source.find("40 USD")


def test_a_field_that_is_not_a_value_quote_pair_is_rejected():
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding({"name": "Ada Lovelace"}, SOURCE)
    assert "name" in str(excinfo.value)


def test_a_field_missing_its_quote_is_rejected():
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding({"name": {"value": "Ada"}}, SOURCE)
    assert "name" in str(excinfo.value)


def test_a_non_string_quote_is_rejected():
    with pytest.raises(grounded_extraction.GroundingError) as excinfo:
        grounded_extraction.verify_grounding(
            {"name": {"value": "Ada", "quote": 1815}}, SOURCE
        )
    assert "name" in str(excinfo.value)


def test_a_non_object_extraction_is_rejected():
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.verify_grounding(["Ada"], SOURCE)


# --- end to end through the offload path --------------------------------------

def test_extraction_from_real_supplied_text_succeeds_with_spans(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    result = json.loads(out)
    assert result["fields"]["name"]["value"] == "Ada Lovelace"
    assert result["fields"]["name"]["quote"] in SOURCE
    assert result["fields"]["birth_year"]["value"] == 1815
    assert result["fields"]["birth_year"]["quote_occurrences"] == 1
    # Model-aware context sizing may probe /api/show before the actual
    # generation request.  The contract under test is one extraction call,
    # not the absence of an internal metadata probe.
    assert sum("messages" in payload for payload in seen["payloads"]) == 1


def test_the_source_text_is_actually_handed_to_the_model(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    sent = "\n".join(m["content"] for m in seen["payload"]["messages"])
    assert SOURCE in sent


def test_the_callers_task_note_reaches_the_model(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), task="Only the subject herself.",
        tier="fast", learn=False,
    )
    sent = "\n".join(m["content"] for m in seen["payload"]["messages"])
    assert "Only the subject herself." in sent


def test_the_grounded_schema_is_what_constrains_the_decoder(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    assert seen["payload"]["format"] == grounded_extraction.grounded_schema(SCHEMA)


def test_a_fabricated_value_whose_span_is_absent_is_rejected_naming_the_field(monkeypatch):
    # The whole point: the model returns a confident, schema-valid answer whose
    # evidence is not in the source. No human reads it; substring search rejects it.
    seen = _capture(monkeypatch, json.dumps({
        "name": {"value": "Ada Lovelace", "quote": "Ada Lovelace was born in London"},
        "birth_year": {"value": 1852, "quote": "Ada Lovelace was born in 1852"},
    }))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    assert out.startswith("ERROR:")
    assert "birth_year" in out
    # rejected, not repaired, and not re-asked: no partial object comes back
    assert len(seen["payloads"]) == 1
    assert not out.startswith("{")


def test_a_response_that_is_not_json_is_rejected(monkeypatch):
    _capture(monkeypatch, "Sure! Ada Lovelace was born in 1815.")
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    assert out.startswith("ERROR:")


def test_the_learning_path_grounds_the_same_way(monkeypatch):
    _learning(monkeypatch)
    _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="code", learn=True,
    )
    assert server.parse_interaction_id(out) == "abc123"
    assert "Ada Lovelace" in out


def test_an_ungrounded_extraction_is_filed_as_a_rejected_outcome(monkeypatch):
    # The id here is deliberately not lowercase hex: only NEGATIVE outcomes are
    # filed from this path, so an id format that quietly failed to parse would
    # drop rejections and nothing else -- an error in the flattering direction.
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-ungrounded")
    _capture(monkeypatch, json.dumps({
        "name": {"value": "Ada Lovelace", "quote": "Ada Lovelace was born in London"},
        "birth_year": {"value": 1852, "quote": "born in 1852"},
    }))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="code", learn=True,
    )
    assert out.startswith("ERROR:")
    assert rows == [("iid-ungrounded", "rejected")]


def test_a_grounded_extraction_files_nothing(monkeypatch):
    # A real span is not a judgement that the answer was good.
    rows = _outcomes(monkeypatch)
    _learning(monkeypatch, interaction_id="iid-ok")
    _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="code", learn=True,
    )
    assert rows == []


# --- the source stays on this machine -----------------------------------------

@pytest.mark.parametrize("tier", sorted(server.CLOUD_TIERS))
def test_a_cloud_tier_is_refused_before_any_request_is_posted(monkeypatch, tier):
    # This helper is handed whole source documents. It routes them to local
    # tiers only, so no document can leave the machine through this path --
    # even with SONDER_ALLOW_CLOUD set.
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier=tier, learn=False,
    )
    assert out.startswith("ERROR:")
    assert seen["payloads"] == []


# --- argument handling --------------------------------------------------------

def test_an_empty_source_is_refused_before_any_request_is_posted(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(source="   ", schema=json.dumps(SCHEMA))
    assert out.startswith("ERROR:")
    assert seen["payloads"] == []


def test_a_missing_schema_is_refused_before_any_request_is_posted(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(source=SOURCE, schema="")
    assert out.startswith("ERROR:")
    assert seen["payloads"] == []


def test_a_malformed_schema_is_refused_before_any_request_is_posted(monkeypatch):
    seen = _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(source=SOURCE, schema="{not json")
    assert out.startswith("ERROR:")
    assert seen["payloads"] == []


# --- partial schema coverage is disclosed, not hidden -------------------------

def test_a_subschema_the_re_check_verifies_is_not_disclosed(monkeypatch):
    # `enum` is enforced in process, so a valid nested value needs no partial-
    # coverage disclosure.
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "enum": ["Ada Lovelace"]}},
    }
    _capture(monkeypatch, json.dumps({
        "name": {"value": "Ada Lovelace", "quote": "Ada Lovelace was born"},
    }))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(schema), tier="fast", learn=False,
    )
    result = json.loads(out)
    assert "schema_unverified" not in result


def test_a_fully_checkable_schema_discloses_nothing(monkeypatch):
    _capture(monkeypatch, json.dumps(GROUNDED_REPLY))
    out = server.extract_grounded(
        source=SOURCE, schema=json.dumps(SCHEMA), tier="fast", learn=False,
    )
    assert "schema_unverified" not in json.loads(out)


# --- the tool is reachable ----------------------------------------------------

def test_the_tool_is_registered():
    names = [tool.name for tool in server.mcp._tool_manager.list_tools()]
    assert "extract_grounded" in names
