import server
import selfmod
import sonder_runtime.interfaces.http.serve as sonder_serve


def test_selfmod_command_surface(monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.setattr(server, "system_improvement_report", lambda: "measured sample defect")

    assert "Sonder self-modification" in server._selfmod_command("status", repository_root=repo)
    assert "usage:" in server._selfmod_command("plan fix sample", repository_root=repo)
    output = server._selfmod_command(
        "plan fix sample --files sample.py --tests python -m pytest -q",
        repository_root=repo,
    )
    assert "phase=proposed" in output
    run = selfmod.list_runs(1)[0]
    assert run["files"] == ["sample.py"]
    assert "explicit user-authorized objective" in run["evidence"][0]


def test_hosted_selfmod_reads_are_safe_but_mutations_need_developer():
    assert not sonder_serve._dangerous_http_slash("/selfmod status")
    assert not sonder_serve._dangerous_http_slash("/selfmod diff selfmod-1")
    assert sonder_serve._dangerous_http_slash("/selfmod run fix --files x.py")
    assert sonder_serve._dangerous_http_slash("/selfmod approve selfmod-1")
    assert sonder_serve._dangerous_http_slash("/selfmod rollback selfmod-1")


def test_selfmod_relative_tool_path_dispatches_only_to_candidate(unattended_effects_allowed, monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    target = repo / "docs" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))

    run = selfmod.create_plan(
        "correct a documentation defect", repo,
        evidence=["docs/note.txt contains measured wrong text"],
        files=["docs/note.txt"], criteria=["corrected text is present"],
        risk="low",
    )
    selfmod.create_backup(run["id"])
    run = selfmod.prepare_workspace(run["id"])
    workspace = selfmod.candidate_path(run["id"])
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(workspace))

    args = {"path": "docs/note.txt", "old": "before", "new": "after"}
    assert server._selfmod_agent_policy(run)("file_edit", args) == ""
    assert args["path"] == str((workspace / "docs/note.txt").resolve())
    assert not server._agent_dispatch("file_edit", args).startswith("ERROR:")

    assert (workspace / "docs/note.txt").read_text(encoding="utf-8") == "after\n"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_selfmod_policy_pins_default_read_root_and_rejects_authority(monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.txt").write_text("sample\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    run = selfmod.create_plan(
        "inspect a bounded sample", repo, evidence=["sample evidence"],
        files=["sample.txt"], criteria=["sample remains bounded"], risk="low",
    )
    selfmod.create_backup(run["id"])
    run = selfmod.prepare_workspace(run["id"])
    policy = server._selfmod_agent_policy(run)

    read_args = {"query": "sample"}
    assert policy("text_search", read_args) == ""
    assert read_args["root"] == str(selfmod.candidate_path(run["id"]).resolve())

    expanded = {"path": "sample.txt", "extra_roots": str(tmp_path)}
    assert "cannot be expanded" in policy("file_read", expanded)


_DEVELOPER = {
    "mode": "account", "authorized": True, "api_key": False,
    "account": {"username": "developer", "role": "developer"},
}
_ADMIN = {
    "mode": "account", "authorized": True, "api_key": False,
    "account": {"username": "admin", "role": "admin"},
}


def test_selfmod_source_and_policy_actions_are_admin_system_operations():
    """G1452: /selfmod deploy/rollback is the same admin tier as /update apply."""
    for command in ("/selfmod", "/selfmodify", "/SELFMOD"):
        for action in ("deploy selfmod-1", "rollback selfmod-1", "prune-backups", "Deploy selfmod-1"):
            assert sonder_serve._slash_system_operation(command, action) == "selfmod_deploy"
        for action in ("enable", "disable", "retention 7 1", "mode review"):
            assert sonder_serve._slash_system_operation(command, action) == "runtime_policy_change"
        # Reads, and the run stages that stay at the developer tier.
        for action in ("", "status", "mode", "diff selfmod-1", "backups", "verify-backup x",
                       "run fix --files x.py", "plan fix --files x.py", "approve selfmod-1",
                       "reject selfmod-1", "resume selfmod-1", "cancel selfmod-1"):
            assert sonder_serve._slash_system_operation(command, action) == ""


def test_developer_account_cannot_deploy_or_roll_back_selfmod(monkeypatch):
    calls = []
    monkeypatch.setattr(sonder_serve, "_http_slash_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(server, "control_command", lambda text, **kw: calls.append(text) or "ran")
    for command in ("/selfmod deploy selfmod-1", "/selfmod rollback selfmod-1",
                    "/selfmod prune-backups", "/selfmod disable", "/selfmod mode review"):
        reply = sonder_serve._handle_slash(command, context=_DEVELOPER)
        assert "administrator authorization is required" in reply, command
    assert calls == []
    # An administrator passes the boundary; a developer keeps the run stages.
    assert sonder_serve._handle_slash("/selfmod deploy selfmod-1", context=_ADMIN) == "ran"
    assert sonder_serve._handle_slash("/selfmod approve selfmod-1", context=_DEVELOPER) == "ran"
    assert calls == ["/selfmod deploy selfmod-1", "/selfmod approve selfmod-1"]


def _allow_selfmod_rule(monkeypatch):
    """Simulate operator opt-in allow rules for selfmod/update/stash (G1452 precondition)."""
    import permission_modes

    orig = permission_modes._rule_action_for
    allowed = {"selfmod", "runtime_source_update", "runtime_source_stash"}

    def _rule(tool, args=None):
        if tool in allowed:
            return permission_modes.ALLOW, "test allow rule"
        return orig(tool, args)

    monkeypatch.setattr(permission_modes, "_rule_action_for", _rule)


def test_nested_sonder_selfmod_and_admin_commands_refused_for_developer(monkeypatch):
    """G1452: /sonder must not smuggle admin-only slash commands past the HTTP role gate."""
    import json

    _allow_selfmod_rule(monkeypatch)
    mutated = []
    monkeypatch.setattr(
        selfmod, "set_enabled",
        lambda flag: mutated.append(("set_enabled", flag)) or {"enabled": flag},
    )
    monkeypatch.setattr(
        server, "runtime_source_update",
        lambda: mutated.append(("update",)) or "updated",
    )
    monkeypatch.setattr(
        server, "runtime_source_stash",
        lambda action: mutated.append(("stash", action)) or f"stashed:{action}",
    )
    monkeypatch.setattr(
        server, "admin_accounts",
        lambda *_a, **_kw: mutated.append(("admin_accounts",)) or "accounts",
    )
    monkeypatch.setattr(
        server, "_runtime_command",
        lambda arg, **_kw: mutated.append(("runtime", arg)) or "runtime-ok",
    )

    nested_loop = json.dumps([{"type": "sonder", "prompt": "/selfmod disable"}], separators=(",", ":"))
    developer_forbidden = (
        "/sonder /selfmod disable",
        "/sonder /selfmod enable",
        "/sonder /selfmod deploy selfmod-1",
        "/sonder /selfmod rollback selfmod-1",
        "/sonder /selfmod prune-backups",
        "/sonder /selfmod retention 7 1",
        "/sonder /selfmod mode review",
        "/sonder /selfmodify disable",
        '/sonder prompt="/selfmod disable"',
        '/sonder tier=sonder prompt="/selfmod disable"',
        "/sonder /sonder /selfmod disable",
        "/sonder /update apply",
        "/sonder /update",
        "/sonder /stash pop",
        "/sonder /stash save_untracked",
        '/sonder prompt="/runtime set code=example"',
        "/sonder /runtime_source_update",
        "/sonder /admin_accounts",
        "/sonder /approve abc",
        f"/sonder /loop {nested_loop}",
    )
    for line in developer_forbidden:
        reply = sonder_serve._handle_slash(line, context=_DEVELOPER)
        assert "administrator authorization is required" in reply, (line, reply)
    assert mutated == []

    # Admin clears the boundary for nested admin-only slash commands.
    assert "selfmod enabled: False" in sonder_serve._handle_slash(
        "/sonder /selfmod disable", context=_ADMIN,
    )
    assert "updated" in sonder_serve._handle_slash("/sonder /update apply", context=_ADMIN)
    assert "stashed:pop" in sonder_serve._handle_slash("/sonder /stash pop", context=_ADMIN)
    assert ("set_enabled", False) in mutated
    assert ("update",) in mutated
    assert ("stash", "pop") in mutated

    # Developer still keeps read-only / developer-tier /selfmod through /sonder.
    monkeypatch.setattr(selfmod, "format_status", lambda: "selfmod status ok")
    assert "selfmod status ok" in sonder_serve._handle_slash(
        "/sonder /selfmod status", context=_DEVELOPER,
    )


def test_loop_sonder_nested_admin_commands_refused_for_developer(monkeypatch):
    """G1452: loop sonder actions must enforce the same admin boundary on nested slash prompts."""
    import json

    _allow_selfmod_rule(monkeypatch)
    mutated = []
    monkeypatch.setattr(
        selfmod, "set_enabled",
        lambda flag: mutated.append(("set_enabled", flag)) or {"enabled": flag},
    )
    monkeypatch.setattr(
        server, "_selfmod_command",
        lambda arg, **_kw: mutated.append(("selfmod", arg)) or f"selfmod:{arg}",
    )
    monkeypatch.setattr(
        server, "runtime_source_update",
        lambda: mutated.append(("update",)) or "updated",
    )
    monkeypatch.setattr(
        server, "runtime_source_stash",
        lambda action: mutated.append(("stash", action)) or f"stashed:{action}",
    )

    for nested_prompt in (
        "/selfmod deploy x",
        "/selfmod disable",
        "/selfmod rollback x",
        "/selfmod prune-backups",
        "/selfmod mode review",
        "/sonder /selfmod disable",
        "/update apply",
        "/stash pop",
        "/stash save_untracked",
        "/runtime set code=example",
        "/runtime_source_update",
    ):
        payload = json.dumps([{"type": "sonder", "prompt": nested_prompt}])
        assert "administrator authorization is required" in sonder_serve._loop_global_operation_refusal(
            payload, _DEVELOPER,
        ), nested_prompt
        assert sonder_serve._loop_global_operation_refusal(payload, _ADMIN) == "", nested_prompt

    # End-to-end through _handle_slash("/loop ...") for developer vs admin.
    deploy_payload = json.dumps([{"type": "sonder", "prompt": "/selfmod deploy x"}])
    dev_reply = sonder_serve._handle_slash(
        f"/loop actions_json='{deploy_payload}'", context=_DEVELOPER,
    )
    assert "administrator authorization is required" in dev_reply, dev_reply
    assert mutated == []

    admin_reply = sonder_serve._handle_slash(
        f"/loop actions_json='{deploy_payload}'", context=_ADMIN,
    )
    assert "administrator authorization is required" not in admin_reply, admin_reply
    assert ("selfmod", "deploy x") in mutated


def test_nested_slash_depth_limit_and_served_control_guard(monkeypatch):
    """Deeply nested /sonder fails closed, and served_control_guard protects direct control_command."""
    _allow_selfmod_rule(monkeypatch)
    mutated = []
    monkeypatch.setattr(
        selfmod, "set_enabled",
        lambda flag: mutated.append(flag) or {"enabled": flag},
    )

    deep = "/sonder " * 10 + "/selfmod disable"
    reply = sonder_serve._handle_slash(deep, context=_DEVELOPER)
    assert "maximum depth" in reply, reply

    # Direct control_command inside a served guard refuses admin commands for a developer.
    dev_guard = sonder_serve._served_control_guard_for(_DEVELOPER)
    with server.served_control_guard(dev_guard):
        guarded = server.control_command("/selfmod disable")
    assert "administrator authorization is required" in guarded, guarded

    # Disabled guard (used by model-only /v1/responses) skips control_command completely.
    disabled_guard = sonder_serve._served_control_guard_for(_DEVELOPER, disabled=True)
    with server.served_control_guard(disabled_guard):
        assert server.control_command("/selfmod disable") is None
    assert mutated == []


