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


def test_agent_project_mutation_validation_and_autopilot_exclusion(monkeypatch, tmp_path):
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
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
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
