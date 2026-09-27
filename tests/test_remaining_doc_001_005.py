"""Contract tests for documentation authority and generated references."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import check_documentation_authority as checker
from scripts import generate_documentation_catalogs as catalogs

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "docs" / "architecture" / "generated"


def test_authority_checker_passes_and_inventory_is_complete():
    assert checker.check() == []
    inventory = json.loads((GENERATED / "focused-contract-inventory.json").read_text(encoding="utf-8"))
    assert len(inventory["documents"]) == 6
    assert all(item["classification"] == "current" and item["exists"] for item in inventory["documents"])
    assert all(item["classification"] == "superseded" and item["exists"] for item in inventory["historical"])


def test_generated_runtime_reference_covers_available_metadata():
    reference = json.loads((GENERATED / "runtime-reference.json").read_text(encoding="utf-8"))
    assert reference["schema"] == "sonder-runtime-document-reference-v1"
    assert reference["counts"]["commands"] >= 300
    assert reference["counts"]["tools"] >= 204
    assert reference["counts"]["events"] >= 43
    assert reference["counts"]["configuration"] >= 65
    assert len({row["name"] for row in reference["tools"]}) == reference["counts"]["tools"]
    assert len({row["name"] for row in reference["commands"]}) == reference["counts"]["commands"]
    config_keys = {(row["section"], row["field"]) for row in reference["configuration"]}
    assert {("root", "schema_version"), ("secrets", "api_key"), ("server", "host")} <= config_keys
    assert len(reference["digest"]) == 64
    assert reference["counts"]["schemas"] == 4
    assert reference["counts"]["capabilities"] == reference["counts"]["tools"]
    assert reference["schemas"]["catalog_digest"] == reference["capabilities"]["catalog_digest"]
    assert {"mcp", "openai", "client", "events"} <= set(reference["schemas"])
    assert reference["capabilities"]["sdk"]["authorization"] == "runtime-evaluated"
    assert "operational" in reference["capabilities"]


def test_generated_runtime_reference_projects_the_native_mcp_catalog():
    """The ``mcp --native`` catalog is generated, so notes cannot drift from it.

    The hand-written migration note kept saying ``vision_analyze`` was not
    exposed after it shipped, and its counts lagged; nothing generated the
    native catalog to check it against.
    """
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry

    reference = json.loads((GENERATED / "runtime-reference.json").read_text(encoding="utf-8"))
    live = sorted(tool.name for tool in native_tool_registry().list_all())
    assert [row["name"] for row in reference["native_tools"]] == live
    assert reference["counts"]["native_tools"] == len(live)
    assert "vision_analyze" in live
    markdown = (GENERATED / "runtime-reference.md").read_text(encoding="utf-8")
    assert "## Native MCP tools" in markdown and "| `vision_analyze` |" in markdown
    note = (ROOT / "docs" / "architecture" / "WP8-NATIVE-MCP-MIGRATION.md").read_text(encoding="utf-8")
    assert "generated/runtime-reference.md" in note
    assert "does not expose `vision_analyze`" not in note


def test_generated_runtime_reference_covers_specialized_memory_replication_contract():
    reference = catalogs._runtime_reference()
    configuration: dict[str, set[tuple[str, str]]] = {}
    for row in reference["configuration"]:
        configuration.setdefault(row["section"], set()).add(
            (row["field"], row["type"])
        )

    assert configuration["memory_replication"] == {
        ("enabled", "bool"),
        ("local_node_id", "str"),
        ("project_scope", "str"),
        ("receiver_enabled", "bool"),
        ("accepted_source_ids", "tuple[str, ...]"),
        ("peers", "tuple[MemoryReplicationPeerConfig, ...]"),
        ("request_timeout_seconds", "int"),
        ("max_request_bytes", "int"),
        ("max_response_bytes", "int"),
        ("max_batch_records", "int"),
    }
    assert configuration["memory_replication.peers[]"] == {
        ("node_id", "str"),
        ("project_scope", "str"),
        ("origin", "str"),
    }


def test_public_generator_freshness_check_passes():
    result = subprocess.run(
        [sys.executable, "scripts/generate_documentation_catalogs.py", "--check"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_reference_fails_closed_when_tool_source_is_unavailable(monkeypatch):
    # The slash-command catalog is read first and imports ``server`` itself on
    # its first (memoised) call. Prime it so the synthetic failure below hits
    # the tool-source read this test is about, whatever ran earlier in the
    # worker; otherwise the ImportError escapes from the command catalog.
    catalogs.importlib.import_module(
        "sonder_runtime.adapters.command_catalog"
    ).command_catalog.catalog()
    original = catalogs.importlib.import_module

    def unavailable(name):
        if name == "server":
            raise ImportError("synthetic server import failure")
        return original(name)

    monkeypatch.setattr(catalogs.importlib, "import_module", unavailable)
    try:
        catalogs._runtime_reference()
    except RuntimeError as exc:
        assert str(exc) == "runtime tool source unavailable"
    else:
        raise AssertionError("unavailable tool source must fail closed")


def test_adr_namespace_accepts_historical_series_and_requires_date_prefix_for_new_adrs():
    policy = (ROOT / "docs" / "architecture" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "New ADRs belong under `docs/adr/`" in policy
    assert "globally unique" in policy
    assert checker.DATE_ADR.fullmatch("ADR-2026-08-20-doc-authority.md")
    assert not checker.DATE_ADR.fullmatch("ADR-010-new-decision.md")
