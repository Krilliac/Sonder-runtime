"""Catalog parsing lives in the domain; the root discovery functions keep the fetch and seams."""
import server
from sonder_runtime.domain import model_catalog

PAYLOAD = {"models": [
    {"name": "Phi4:latest", "digest": "abc123", "details": {"family": "phi"}},
    {"model": "gemma3:12b", "details": {"digest": "def456"}},
    {"name": "phi4:LATEST"},
    {"name": ""},
    "junk",
    {"name": "zeta", "digest": ""},
]}


def test_catalog_parsing_deduplicates_case_insensitively_and_sorts():
    assert model_catalog.catalog_names(PAYLOAD) == ["gemma3:12b", "Phi4:latest", "zeta"]
    records = model_catalog.catalog_records(PAYLOAD)
    assert [name for name, _record in records] == ["gemma3:12b", "Phi4:latest", "zeta"]
    assert records[1][1]["digest"] == "abc123"
    installed = model_catalog.installed_records(PAYLOAD)
    assert isinstance(installed, tuple)
    assert [name for name, _record in installed] == ["Phi4:latest", "gemma3:12b", "zeta"]
    for empty in ({}, [], {"models": "x"}, None):
        assert model_catalog.catalog_names(empty) == []
        assert model_catalog.catalog_records(empty) == []
        assert model_catalog.installed_records(empty) == ()


def test_revision_and_resolution_honour_latest_and_case():
    records = model_catalog.catalog_records(PAYLOAD)
    assert model_catalog.catalog_revision("PHI4", records) == "abc123"
    assert model_catalog.catalog_revision("phi4:latest", records) == "abc123"
    assert model_catalog.catalog_revision("gemma3:12b", records) == "def456"
    assert model_catalog.catalog_revision("zeta", records) == ""
    assert model_catalog.catalog_revision("", records) == ""
    assert model_catalog.resolve_record("GEMMA3:12B", records) == ("gemma3:12b", records[0][1])
    assert model_catalog.resolve_record("missing", records) is None
    assert model_catalog.resolve_record("", records) is None


def test_root_discovery_functions_fetch_once_and_parse_through_the_domain(monkeypatch):
    fetched = []

    def fake_get(path):
        fetched.append(path)
        return PAYLOAD

    monkeypatch.setattr(server, "_get", fake_get)
    assert server.discovered_models() == model_catalog.catalog_names(PAYLOAD)
    assert server.discovered_model_records() == model_catalog.catalog_records(PAYLOAD)
    assert server._runtime_installed_model_records() == model_catalog.installed_records(PAYLOAD)
    assert server.resolve_discovered_model_record("phi4:latest")[0] == "Phi4:latest"
    assert server.resolve_discovered_model("GEMMA3:12b") == "gemma3:12b"
    assert server._cache_model_revision("phi4") == "abc123"
    assert fetched == ["/api/tags"] * 6


def test_root_delegates_keep_their_short_circuits_and_failure_shapes(monkeypatch):
    def failing(_path):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(server, "_get", failing)
    assert server._cache_model_revision("") == ""
    assert server._cache_model_revision("phi4") == ""
    assert server.resolve_discovered_model_record("") is None
    monkeypatch.setattr(server, "discovered_model_records", lambda: [("Phi4:latest", {"digest": "zzz"})])
    assert server._cache_model_revision("phi4") == "zzz"
    assert server.resolve_discovered_model("phi4:latest") == "Phi4:latest"
