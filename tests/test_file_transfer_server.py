from pathlib import Path

import server


def test_direct_copy_and_move_record_tool_and_file_activity(monkeypatch, tmp_path):
    direct = []
    files = []
    moves = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.file_ops,
        "copy_file",
        lambda *args, **kwargs: {
            "action": "copy", "path": str(tmp_path / "copy.bin"),
            "source": str(tmp_path / "source.bin"),
            "destination": str(tmp_path / "copy.bin"), "bytes": 4,
        },
    )
    monkeypatch.setattr(
        server.file_ops,
        "move_file",
        lambda *args, **kwargs: {
            "action": "move", "path": str(tmp_path / "moved.bin"),
            "source": str(tmp_path / "copy.bin"),
            "destination": str(tmp_path / "moved.bin"), "bytes": 4,
        },
    )
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, *args, **kwargs: direct.append((name, kwargs.get("ok"))),
    )
    monkeypatch.setattr(
        server, "_record_file_activity",
        lambda action, data: files.append((action, data["path"])),
    )
    monkeypatch.setattr(
        server.activity_tracker, "record_file_change",
        lambda action, path, **kwargs: moves.append((action, path)),
    )

    assert server.file_copy("source.bin", "copy.bin").startswith("file copy")
    assert server.file_move("copy.bin", "moved.bin").startswith("file move")
    assert direct == [("file_copy", True), ("file_move", True)]
    assert files == [
        ("copy", str(tmp_path / "copy.bin")),
        ("move", str(tmp_path / "moved.bin")),
    ]
    assert moves == [("move_source", str(tmp_path / "copy.bin"))]


def test_agent_dispatch_routes_transfer_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "file_copy",
        lambda **kwargs: calls.append(("copy", kwargs)) or "copied",
    )
    monkeypatch.setattr(
        server,
        "file_move",
        lambda **kwargs: calls.append(("move", kwargs)) or "moved",
    )
    assert server._agent_dispatch(
        "file_copy",
        {"source": "a.bin", "destination": "b.bin", "overwrite": True},
    ) == "copied"
    assert server._agent_dispatch(
        "file_move", {"source": "b.bin", "destination": "c.bin"},
    ) == "moved"
    assert calls[0][1]["overwrite"] is True
    assert calls[1][1]["overwrite"] is False


def test_project_scope_rebases_and_checks_both_transfer_ends(tmp_path):
    project = str(tmp_path.resolve())
    scoped = server._project_scope_args(
        "file_move",
        {"source": "in/a.bin", "destination": "out/b.bin"},
        project,
    )
    assert Path(scoped["source"]) == tmp_path / "in" / "a.bin"
    assert Path(scoped["destination"]) == tmp_path / "out" / "b.bin"
    assert scoped["extra_roots"] == project
    assert server._repository_scope_path_error("file_move", scoped, project) == ""

    escaped = dict(scoped, destination=str(tmp_path.parent / "escaped.bin"))
    assert "destination is outside" in server._repository_scope_path_error(
        "file_move", escaped, project
    )


def test_transfer_mutation_ledger_and_validation_cover_destination(tmp_path):
    destination = tmp_path / "out.bin"
    record = server._agent_mutation_record(
        "file_move",
        {"source": str(tmp_path / "in.bin"), "destination": str(destination)},
    )
    assert record == {
        "tool": "file_move",
        "path": server._agent_normalized_path(destination),
        "source": server._agent_normalized_path(tmp_path / "in.bin"),
    }
    assert server._agent_validation_covers(
        "file_find",
        {"root": str(tmp_path)},
        [record],
        "file find\n  file out.bin (4 bytes)",
    )


def test_autopilot_transfer_policy_is_workspace_bounded():
    check = server._autopilot_tool_policy({"policy": "workspace"})
    assert {"file_copy", "file_move"} <= server._AUTOPILOT_WORKSPACE_TOOLS
    assert {"file_copy", "file_move"} <= server._AUTOPILOT_MUTATION_EVIDENCE
    assert check(
        "file_copy", {"source": "a.bin", "destination": "b.bin"}
    ) == ""
    assert "exact source and destination" in check(
        "file_move", {"source": "a.bin"}
    )
    assert "cannot overwrite" in check(
        "file_move",
        {"source": "a.bin", "destination": "b.bin", "overwrite": True},
    )
    assert "bypass" in check(
        "file_copy",
        {"source": "a", "destination": "b", "extra_roots": "outside"},
    )


def test_manifest_and_agent_help_expose_transfer_contracts():
    manifest = server.tool_manifest()
    assert "file_copy" in manifest and "file_move" in manifest
    assert "file_copy:" in server.AGENT_TOOL_HELP
    assert "file_move:" in server.AGENT_TOOL_HELP
    assert {"file_copy", "file_move"} <= server._WORK_MUTATION_TOOLS
    assert {"file_copy", "file_move"} <= server._PROJECT_SCOPED_PATH_TOOLS
