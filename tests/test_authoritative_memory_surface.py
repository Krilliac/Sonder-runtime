from types import SimpleNamespace
import json

import pytest

import server
from sonder_runtime.adapters import embeddings
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import SQLiteAuthoritativeFactSource
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.facade import MemoryLearningFacade


@pytest.fixture
def authoritative_surface(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    def unit_of_work(db_path=None):
        return UnitOfWorkAdapter(str(db), authoritative_fact_source=source)

    application = SimpleNamespace(
        unit_of_work=unit_of_work,
        memory=MemoryLearningFacade(unit_of_work),
    )
    monkeypatch.setattr(server, "_application", lambda: application)
    monkeypatch.setattr(server, "_DB_PATH", str(db))
    monkeypatch.setattr(embeddings, "embed", lambda _text: None)
    return application


def test_remember_and_index_tools_use_explicit_scoped_metadata(authoritative_surface):
    result = server.sonder_remember_fact(
        "Use bounded parsing", project="repo-a",
        entities_json='["parser"]',
        decision_json='{"id":"parser-policy","value":"bounded"}',
        valid_from="2026-01-01T00:00:00+00:00",
        provenance_json='["review:17"]',
    )
    assert "Remembered fact" in result
    payload = json.loads(server.sonder_authoritative_indexes(
        project="repo-a", entity_id="parser", now="2026-02-01T00:00:00+00:00",
    ))
    assert payload["project"] == "repo-a"
    assert payload["entities"][0]["entity_id"] == "parser"
    assert json.loads(payload["decisions"][0]["decision_json"])["value"] == "bounded"
    assert server.sonder_authoritative_indexes(project="repo-b")


def test_surface_rejects_malformed_metadata_without_text_inference(authoritative_surface):
    assert server.sonder_remember_fact(
        "The parser should be safe", project="repo-a", entities_json='{"guess":"parser"}'
    ).startswith("ERROR:")
    assert server.sonder_remember_fact(
        "The parser should be safe", project="repo-a",
        decision_json='{"id":"x","value":"y","policy":"promote"}',
    ).startswith("ERROR:")
    assert server.sonder_remember_fact(
        "The parser should be safe", project="repo-a", valid_from="x" * 65
    ).startswith("ERROR:")


def test_surface_refuses_authoritative_scope_widening(authoritative_surface):
    result = server.sonder_remember_fact(
        "Must stay in repo-a", project="repo-b", entities_json='["parser"]'
    )
    assert result.startswith("ERROR:")
    assert json.loads(server.sonder_authoritative_indexes(project="repo-b"))["entities"] == []


def test_surface_keeps_legacy_fact_calls_without_metadata(authoritative_surface):
    result = server.sonder_remember_fact("Legacy fact", project="repo-a")
    assert "Remembered fact" in result
    assert json.loads(server.sonder_authoritative_indexes(project="repo-a"))["entities"] == []


def test_surface_rejects_non_string_metadata_inputs(authoritative_surface):
    result = server.sonder_remember_fact(
        "Typed input", project="repo-a", entities_json=None
    )
    assert result.startswith("ERROR:")


def test_index_surface_rejects_ambiguous_or_malformed_time(authoritative_surface):
    assert server.sonder_authoritative_indexes(
        project="repo-a", now="2026-01-01T00:00:00"
    ).startswith("ERROR:")
    assert server.sonder_authoritative_indexes(
        project="repo-a", now="not-a-date"
    ).startswith("ERROR:")

