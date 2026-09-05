"""Actual configured Application and canonical memory conversation acceptance."""

import json

import server
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.platform.config import load_config
from sonder_runtime.interfaces.repl import repl
from sonder_runtime.interfaces.standalone_agent_lanes import controller_scope
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger


def test_two_work_turns_reuse_same_persisted_conversation(
    tmp_path, tmp_path_factory, monkeypatch
):
    project = tmp_path_factory.mktemp("repeat-work-project")
    config = tmp_path / "sonder.toml"
    config.write_text(
        "[state]\nworkspace_roots = " + json.dumps([str(project)]) + "\n",
        encoding="utf-8",
    )
    application = build_application(config=load_config(config))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: project)
    monkeypatch.setattr(server, "_application", lambda: application)
    monkeypatch.setattr(repl, "_legacy_runtime", None)
    repl.configure_legacy_runtime(server)
    controllers = []

    def work(**arguments):
        with controller_scope(server._application, project=str(project)) as controller:
            controller.require_current()
            controllers.append(controller)
            controller.begin_host_turn(
                HostObservationLedger(project_scope=str(project))
            )
            result = controller._managed_session.report_metadata()
            controller.freeze_host_terminal(
                json.dumps(result), terminal_class="NORMAL", blockers=()
            )
            return result

    monkeypatch.setattr(server, "workbench_agent", work)
    try:
        with server._managed_repl_conversation_scope():
            first = repl._run_session_work(
                "3333333333333333",
                host_project="repeat",
                project=str(project),
                prompt="inspect first",
            )
            second = repl._run_session_work(
                "3333333333333333",
                host_project="repeat",
                project=str(project),
                prompt="inspect again",
            )
        assert first["continuation_id"] == second["continuation_id"]
        assert len(controllers) == 2
        with server._open_db() as connection:
            assert server.memory_store.get_session(connection, "3333333333333333")
    finally:
        application.close_providers(timeout=5)


def test_actual_console_two_work_commands_share_owner_until_exit(
    tmp_path, tmp_path_factory, monkeypatch
):
    from tests.test_tier_escalation import _install_agent_fakes

    project = tmp_path_factory.mktemp("console-repeat-project")
    config = tmp_path / "console.toml"
    config.write_text(
        "[state]\nworkspace_roots = " + json.dumps([str(project)]) + "\n",
        encoding="utf-8",
    )
    application = build_application(config=load_config(config))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: project)
    monkeypatch.setattr(server, "_application", lambda: application)
    monkeypatch.setattr(repl, "_legacy_runtime", None)
    repl.configure_legacy_runtime(server)
    models = _install_agent_fakes(
        monkeypatch, {"m-code": '{"final":"repository inspected"}'}
    )
    monkeypatch.setattr(repl.memory_store, "new_id", lambda: "4444444444444444")
    lines = iter(
        (
            "/workspace " + str(project),
            "/work inspect repository",
            "/work inspect repository again",
            "/exit",
        )
    )
    monkeypatch.setattr(repl, "_read_input", lambda *_a, **_k: next(lines))
    monkeypatch.setattr(repl, "_startup_banner", lambda *_a: "")
    monkeypatch.setattr(repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(repl, "_named_command_gate", lambda *_a: (True, ""))
    try:
        repl.main()
        assert models == ["m-code", "m-code"]
        store = application.agent_lanes().store
        with store.transaction() as tx:
            rows = tx.conn.execute(
                "SELECT data FROM agent_lane_continuations"
            ).fetchall()
            record = next(
                json.loads(row[0])
                for row in rows
                if json.loads(row[0])["host_conversation_id"]
                == "repl-session:4444444444444444"
            )
            assert record["host_turn"]["ordinal"] == 2
            assert len(record["host_turn_history"]) == 1
            assert store.owner_definitely_stopped(record["owner"])
    finally:
        application.close_providers(timeout=5)
