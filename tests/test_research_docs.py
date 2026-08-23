"""Guards for the docs/research contract artifacts.

docs/research/harness-landscape.md cites two JSON Schemas and an experiment
template as the shapes its recommendations (R1, R3) build on. Nothing at
runtime reads them, so nothing at runtime would notice if an edit broke
them; these tests are the only thing standing between "the doc cites a
contract" and "the doc cites a file that no longer parses". Stdlib only —
no jsonschema dependency, so the checks are structural, not full
draft-2020-12 validation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "docs" / "research"
LANDSCAPE = RESEARCH_DIR / "harness-landscape.md"
SCHEMAS = {
    "trace": RESEARCH_DIR / "schemas" / "trace-event.schema.json",
    "eval": RESEARCH_DIR / "schemas" / "golden-eval-case.schema.json",
}
TEMPLATE = RESEARCH_DIR / "templates" / "adoption-experiment.md"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_parses_and_is_2020_12(name: str) -> None:
    schema = _load(SCHEMAS[name])
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    # Both are labeled experimental research artifacts; losing the label
    # would let a reader mistake them for a live runtime contract.
    assert "EXPERIMENTAL" in schema["title"]
    assert "NOT read by the runtime" in schema["description"]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_required_keys_are_declared(name: str) -> None:
    schema = _load(SCHEMAS[name])
    properties = set(schema["properties"])
    missing = set(schema["required"]) - properties
    assert not missing, "required keys absent from properties: %s" % sorted(missing)
    # Both schemas close their top level; a typo'd property name would
    # otherwise validate silently under an open object.
    assert schema["additionalProperties"] is False


@pytest.mark.unit
def test_eval_schema_keeps_model_graders_advisory() -> None:
    schema = _load(SCHEMAS["eval"])
    scorer = schema["properties"]["scorers"]["items"]
    kinds = scorer["properties"]["kind"]["enum"]
    assert "model_grader" in kinds
    # The advisory flag is the doc's whole point: model graders may report
    # but never gate. Removing the property would erase that distinction.
    assert "advisory" in scorer["properties"]
    assert scorer["properties"]["advisory"]["default"] is False


@pytest.mark.unit
def test_trace_schema_keeps_cancellation_distinct() -> None:
    status = _load(SCHEMAS["trace"])["properties"]["status"]["enum"]
    # Sonder-wide rule: a cancelled or timed-out step must never be
    # confusable with a passed one.
    for value in ("ok", "error", "cancelled", "timeout"):
        assert value in status


@pytest.mark.unit
def test_landscape_relative_links_resolve() -> None:
    text = LANDSCAPE.read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#]+)\)", text)
    relative = [
        link
        for link in links
        if not link.startswith(("http://", "https://", "mailto:"))
    ]
    assert relative, "expected the landscape doc to cite local artifacts"
    missing = [link for link in relative if not (RESEARCH_DIR / link).exists()]
    assert not missing, "dangling links in harness-landscape.md: %s" % missing


@pytest.mark.unit
def test_template_carries_default_no_go_constraints() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for anchor in (
        "No-go constraints",
        "SONDER_ALLOW_CLOUD",
        "selfmod.protected_paths()",
        "Acceptance criteria",
        "RED proof",
    ):
        assert anchor in text, "experiment template lost its %r section" % anchor
