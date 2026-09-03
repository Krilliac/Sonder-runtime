import json
import sqlite3

import server


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO records VALUES (1, 'before')")
    conn.commit()
    conn.close()


def test_mcp_activity_preview_apply_and_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "records.db"
    _database(path)
    changes = []
    monkeypatch.setattr(
        server.activity_tracker, "record_file_change",
        lambda action, path, **kwargs: changes.append((action, path, kwargs)),
    )
    preview = json.loads(server.sqlite_mutate(
        str(path), "UPDATE records SET value = ? WHERE id = ?", '["after",1]',
    ))
    assert preview["applied"] is False and changes == []
    applied = json.loads(server.sqlite_mutate(
        str(path), "UPDATE records SET value = ? WHERE id = ?", '["after",1]', mode="apply",
    ))
    assert applied["applied"] is True and changes[0][0] == "sqlite_mutate"
    assert server.sqlite_mutate(str(path), "SELECT ?", "[1]").startswith("ERROR:")


def test_preview_token_is_returned_but_not_written_to_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "records.db"
    _database(path)
    recorded = []
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    preview = json.loads(server.sqlite_mutate(
        str(path), "UPDATE records SET value = ? WHERE id = ?", '["after",1]',
    ))
    token = preview["preview_token"]
    assert token and token != "<redacted>"
    assert recorded and token not in recorded[-1][1]["output"]
    assert "<redacted>" in recorded[-1][1]["output"]


def test_agent_project_mutation_validation_and_autopilot_exclusion(every_tool_allowed_by_rule, monkeypatch, tmp_path):
    assert "sqlite_mutate" in server.tool_manifest()
    assert "- sqlite_mutate:" in server._agent_tool_help()
    assert "sqlite_mutate" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "sqlite_mutate" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "sqlite_mutate" in server._WORK_MUTATION_TOOLS
    assert "sqlite_mutate" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert "sqlite_mutate" not in server._AUTOPILOT_WORKSPACE_TOOLS
    assert server._agent_tool_mutates("sqlite_mutate", {"mode": "preview"}) is False
    assert server._agent_tool_mutates("sqlite_mutate", {"mode": "apply"}) is True
    assert server._agent_dispatch(
        "sqlite_mutate", {"path": "x.db", "sql": "DELETE FROM x WHERE id = ?", "parameters": [1]},
        read_only=True,
    ).startswith("ERROR:")

    project = tmp_path / "project"
    project.mkdir()
    path = project / "records.db"
    _database(path)
    output = server._agent_dispatch_observed(
        "sqlite_mutate", {
            "path": "records.db", "sql": "UPDATE records SET value = ? WHERE id = ?",
            "parameters": ["after", 1], "mode": "apply",
        }, project=str(project),
    )
    assert json.loads(output)["applied"] is True
    records = server._agent_mutation_records(
        "sqlite_mutate", {"path": str(path), "mode": "apply"},
    )
    assert records[0]["path"] == server._agent_normalized_path(str(path))


def test_project_bound_sqlite_mutation_cannot_escape_with_trusted_approval(every_tool_allowed_by_rule, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "records.db"
    _database(path)

    output = server._agent_dispatch_observed(
        "sqlite_mutate", {
            "path": str(path),
            "sql": "UPDATE records SET value = ? WHERE id = ?",
            "parameters": ["escaped", 1],
            "mode": "apply",
        },
        project=str(project),
    )

    assert output.startswith("ERROR: agent project path rejected")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT value FROM records WHERE id = 1").fetchone() == ("before",)
    finally:
        conn.close()


def test_sqlite_module_participates_in_live_reload(monkeypatch):
    original = server.sqlite_mutate_module
    replacement = object()
    monkeypatch.setattr(
        server.live_reload, "reload_changed_modules",
        lambda names: {"sqlite_mutate": replacement},
    )
    try:
        assert "sqlite_mutate" in server.LIVE_RELOAD_MODULES
        server._maybe_live_reload()
        assert server.sqlite_mutate_module is replacement
    finally:
        server.sqlite_mutate_module = original
