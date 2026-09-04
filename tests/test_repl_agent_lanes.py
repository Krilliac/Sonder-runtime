import pytest
from tests.test_interactive_agent_lanes import env


def test_lane_console_facade_exists():
    from sonder_runtime.interfaces.repl.facades.agent_lanes import LaneConsoleFacade

    assert LaneConsoleFacade


from dataclasses import replace
from types import SimpleNamespace
import io
import json
import unicodedata

import server
import permission_modes as pm
import sonder_runtime.interfaces.repl.repl as repl
from sonder_runtime.interfaces.repl.facades.agent_lanes import (
    LaneConsoleFacade,
    parse,
    terminal_text,
)


def app_for(env, roots=None):
    return SimpleNamespace(
        agent_lanes=lambda: env[0],
        config=SimpleNamespace(
            state=SimpleNamespace(
                workspace_roots=roots if roots is not None else (str(env[-1]),)
            ),
            ollama=SimpleNamespace(allow_remote=False),
        ),
    )


def child(env, task="parser task"):
    return env[0].spawn(
        command_id="spawn",
        parent_session_id="actual-parent",
        task=task,
        workspace_root=str(env[-1] / "child"),
        context=env[-2],
    )["lane"]["id"]


def facade(env, approve=lambda args: (True, ""), app=None):
    return LaneConsoleFacade(lambda: app or app_for(env), approve)


def test_real_controls_reports_and_authorship(env):
    service, _, _, model, context, root = env
    lane = child(env)
    ui = facade(env)
    assert lane in ui.run("list")
    text = "Preserve Unicode\n  add tests"
    assert "Recorded message" in ui.run("message " + lane + " " + text)
    assert service.inspect(lane, context)["messages"][-1]["author"] == "user"
    assert "Recorded interrupt" in ui.run("interrupt " + lane)
    service.run_pending(lane, context)
    assert "Recorded resume" in ui.run("resume " + lane)
    service.run_pending(lane, context)
    assert "A verified result" in ui.run("show " + lane)
    reports = service.reports("actual-parent", context)["reports"]
    assert reports[0]["id"] in ui.run("reports " + lane)
    assert "Recorded ack" in ui.run("ack " + lane + " " + reports[0]["id"])
    assert service.reports("actual-parent", context)["reports"][0]["acknowledged"]
    assert "Recorded cancel" in ui.run("cancel " + lane)


@pytest.mark.parametrize("action", ["message", "interrupt", "resume", "cancel", "ack"])
def test_denied_commands_have_no_service_mutation(env, action):
    lane = child(env)
    service, _, _, _, context, _ = env
    service.run_pending(lane, context)
    report = service.reports("actual-parent", context)["reports"][0]
    before = service.inspect(lane, context)
    args = lane + (
        " text"
        if action == "message"
        else (" " + report["id"] if action == "ack" else "")
    )
    approved = []

    def deny(command):
        approved.append(command)
        return False, "configured mode"

    assert "refused" in facade(env, deny).run(action + " " + args)
    after = service.inspect(lane, context)
    assert before == after
    assert approved[0]["author"] == "user"
    assert approved[0]["workspace_root"] == str(env[-1] / "child")
    assert "parent_token" not in json.dumps(approved)


def test_exact_approved_content_and_scope_recheck(env):
    lane = child(env)
    app = app_for(env)

    def tamper(command):
        command["content"] = "substituted"
        command["lane_id"] = "other"
        return True, ""

    assert "Recorded message" in facade(env, tamper, app).run(
        "message " + lane + " approved"
    )
    assert env[0].inspect(lane, env[-2])["messages"][-1]["content"] == "approved"
    before = env[0].inspect(lane, env[-2])

    def revoke(command):
        app.config.state.workspace_roots = ()
        return True, ""

    assert "changed after approval" in facade(env, revoke, app).run("cancel " + lane)
    assert env[0].inspect(lane, env[-2]) == before


def test_missing_or_outside_workspace_is_not_shown(env):
    lane = child(env)
    other = env[-1] / "other"
    other.mkdir()
    ui = facade(env, app=app_for(env, (str(other),)))
    result = ui.run("list")
    assert lane not in result and "filtered" in result
    assert "outside" in ui.run("show " + lane)
    assert "outside" in ui.run("cancel " + lane)
    assert "no available configured" in facade(env, app=app_for(env, ())).run("list")


def test_unknown_empty_and_unavailable_are_distinct(env):
    assert "No durable lanes yet" in facade(env).run("list")
    assert "not found" in facade(env).run("show lane-missing")
    ui = LaneConsoleFacade(
        lambda: (_ for _ in ()).throw(RuntimeError("secret")), lambda args: (True, "")
    )
    text = ui.run("list")
    assert "unavailable" in text and "secret" not in text
    assert "Invalid lane command" in facade(env).run("destroy")


@pytest.mark.parametrize(
    "command",
    [
        "show ../file",
        "show x -1",
        "list 2147483648",
        "list 999999999999999999",
        "ack x y 1 extra",
        "message x",
        "spawn",
    ],
)
def test_parser_rejects_unbounded_or_unknown_forms(command):
    with pytest.raises(ValueError):
        parse(command)


def test_terminal_text_preserves_multiline_and_escapes_controls():
    text = terminal_text(
        "first\n  line\t\x1b]52;c;secret\x07\x1b[2J\r\x9b2J\u202eend", width=40
    )
    assert text.startswith("first\n  line")
    assert "\\x1b" in text and "\\u202e" in text
    assert not any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text if c != "\n"
    )
    narrow = terminal_text("界" * 40 + "\n  next", width=20)
    for line in narrow.splitlines():
        assert (
            sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in line)
            <= 20
        )


def test_real_transcript_is_sanitized_and_bounded(env):
    lane = child(env, "bad\x1b[2Jtitle")
    facade(env).run("message " + lane + " first\nsecond\x1b]0;hostile\x07")
    text = facade(env).run("show " + lane, width=32)
    assert "\x1b" not in text and "\x07" not in text
    assert "first" in text and "second" in text and "\\x1b" in text
    assert max(map(len, text.splitlines())) <= 32


def test_plan_reads_work_but_mutations_are_denied(env, monkeypatch):
    lane = child(env)
    monkeypatch.setattr(
        repl, "_legacy_runtime", SimpleNamespace(_application=lambda: app_for(env))
    )
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)

    def decide(tool, **kwargs):
        return pm.decide_for_caller(
            tool, mode=pm.PLAN, record=False, rule_lookup=lambda tool: None, **kwargs
        )

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    assert lane in repl._lanes_command("list")
    assert "refused" in repl._lanes_command("cancel " + lane)
    assert env[0].inspect(lane, env[-2])["lane"]["status"] == "queued"


def test_confirmation_details_are_exact_safe_and_piped_input_is_not_consumed(
    monkeypatch,
):
    prompts = []
    decision = SimpleNamespace(action=pm.ASK, reason="execute")
    monkeypatch.setattr(
        repl.permission_policy, "decide_for_caller", lambda *a, **k: decision
    )
    monkeypatch.setattr(repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(repl, "_confirm", lambda prompt: prompts.append(prompt) or True)
    command = {
        "action": "message",
        "lane_id": "lane-x",
        "content": "exact\ntext\x1b[2J",
    }
    assert repl._approve_lane_command(command)[0]
    assert "lane-x" in prompts[0] and "exact" in prompts[0] and "text" in prompts[0]
    assert "\x1b" not in prompts[0]
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)
    assert not repl._approve_lane_command(command)[0]
    assert len(prompts) == 1


def test_jsonl_writer_receives_only_sanitized_lines(env):
    lane = child(env, "title\x1b]52;c;payload\x07")
    stream = io.StringIO()
    writer = repl._JsonLinesWriter(stream)
    writer.write(facade(env).run("show " + lane) + "\n")
    writer.close()
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert rows and all(row["schema"] == "sonder.repl-output.v1" for row in rows)
    assert all("\x1b" not in row["text"] and "\x07" not in row["text"] for row in rows)


def test_event_pages_preserve_cursor_without_repeating_transcript(env):
    lane = child(env)
    service = env[0]
    for number in range(23):
        service.send_message(
            lane,
            command_id="m" + str(number),
            content="message-" + str(number),
            author="user",
            context=env[-2],
        )
    page = service.inspect(lane, env[-2], cursor=0, limit=20)
    first = facade(env).run("show " + lane)
    assert "/lanes show " + lane + " " + str(page["next_cursor"]) in first
    second = facade(env).run("show " + lane + " " + str(page["next_cursor"]))
    assert "message-22" in second and "message-0\n" not in second


def test_report_pages_filter_actual_parent_and_ack_cannot_cross_lane(env):
    lane = child(env)
    service = env[0]
    context = env[-2]
    service.run_pending(lane, context)
    (env[-1] / "sibling").mkdir()
    sibling = service.spawn(
        command_id="sibling",
        parent_session_id="actual-parent",
        task="sibling task",
        workspace_root=str(env[-1] / "sibling"),
        context=context,
    )["lane"]["id"]
    service.run_pending(sibling, context)
    reports = service.reports("actual-parent", context)["reports"]
    sibling_report = next(report for report in reports if report["lane_id"] == sibling)
    text = facade(env).run("reports " + lane)
    assert sibling_report["id"] not in text
    assert "filtered to this lane" in text
    assert "not found" in facade(env).run("ack " + lane + " " + sibling_report["id"])
    assert not next(
        report
        for report in service.reports("actual-parent", context)["reports"]
        if report["id"] == sibling_report["id"]
    )["acknowledged"]


def test_foreign_principal_lane_is_not_controllable(env):
    lane = child(env)
    with env[1].transaction() as tx:
        data = tx.lane(lane)
        data["principal_id"] = "other-account"
        tx.save(data)
    ui = facade(env)
    assert lane not in ui.run("list")
    assert "another principal" in ui.run("show " + lane)
    assert "another principal" in ui.run("cancel " + lane)


def test_actual_named_command_dispatch_keeps_legacy_agents(env, monkeypatch, capsys):
    lane = child(env)
    monkeypatch.setattr(repl, "_legacy_runtime", server)
    monkeypatch.setattr(server, "_application", lambda: app_for(env))
    monkeypatch.setattr(server, "master_status", lambda: "legacy activity")
    lines = iter(("/lanes list", "/agents", "/exit"))
    monkeypatch.setattr(repl, "_read_input", lambda *a, **k: next(lines))
    monkeypatch.setattr(repl, "_startup_banner", lambda *a: "")
    monkeypatch.setattr(repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)
    repl.main()
    output = capsys.readouterr().out
    assert lane in output and "legacy activity" in output


def test_read_lane_command_is_in_catalog_and_not_an_execution_gate():
    from sonder_runtime.adapters.command_catalog import command_catalog

    assert command_catalog.by_name("/lanes") is not None
    assert repl._named_command_gate("/lanes", "list")[0]


def test_oversized_escaped_confirmation_is_refused_before_approval(env):
    lane = child(env)
    calls = []
    result = facade(env, lambda command: calls.append(command) or (True, "")).run(
        "message " + lane + " " + ("\x01" * 4000)
    )
    assert "approval detail exceeds" in result
    assert calls == []


def test_lane_target_retargeting_after_approval_is_refused(env, monkeypatch):
    from pathlib import Path

    lane = child(env)
    (env[-1] / "other").mkdir()
    original = Path.resolve
    switched = False

    def resolve(path, *a, **k):
        if switched and path == env[-1] / "child":
            return original(env[-1] / "other", *a, **k)
        return original(path, *a, **k)

    def approve(command):
        nonlocal switched
        switched = True
        return True, ""

    monkeypatch.setattr(Path, "resolve", resolve)
    result = facade(env, approve).run("cancel " + lane)
    assert "outside the current configured" in result
    assert env[0].inspect(lane, env[-2])["lane"]["status"] == "queued"


from tests.test_permission_approvals import ledger


def test_real_ledger_approves_identical_console_retry_once(env, ledger, monkeypatch):
    lane = child(env)
    monkeypatch.setattr(
        repl, "_legacy_runtime", SimpleNamespace(_application=lambda: app_for(env))
    )
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)
    command = "message " + lane + " exact retry content"
    first = repl._lanes_command(command)
    assert "refused" in first
    pending = ledger.pending()
    assert len(pending) == 1
    call_id = pending[0].call_id
    assert call_id in first.replace("\n", "")
    approved = server.control_command(
        "/approve " + call_id + " 120", operator_approved=True
    )
    assert "approved agent_lane" in approved
    assert "Recorded message" in repl._lanes_command(command)
    assert "refused" in repl._lanes_command(command)
    messages = env[0].inspect(lane, env[-2])["messages"]
    assert sum(message["content"] == "exact retry content" for message in messages) == 1


def test_console_reads_and_control_prechecks_do_not_fetch_message_bodies(
    env, monkeypatch
):
    from sonder_runtime.adapters.persistence.agent_lanes import LaneTransaction

    lane = child(env)

    def forbidden(*args, **kwargs):
        pytest.fail("console metadata/transcript path loaded mailbox bodies")

    monkeypatch.setattr(LaneTransaction, "messages", forbidden)
    ui = facade(env)
    assert lane in ui.run("list")
    assert lane in ui.run("show " + lane)
    assert "Recorded message" in ui.run("message " + lane + " hello")
    assert "Recorded interrupt" in ui.run("interrupt " + lane)


def test_unread_report_count_includes_more_than_one_hundred_reports(env):
    lane = child(env)
    with env[1].transaction() as tx:
        row = tx.lane(lane)
        for index in range(105):
            tx.message(row, "report " + str(index), "parent", report=True)
    assert env[0].inspect(lane, env[-2])["lane"]["unread_reports"] == 105


def test_lightweight_metadata_does_not_read_event_pages(env, monkeypatch):
    lane = child(env)
    calls = []
    original = env[1].events

    def events(lane_id, cursor, limit):
        calls.append((lane_id, cursor, limit))
        return original(lane_id, cursor, limit)

    monkeypatch.setattr(env[1], "events", events)
    ui = facade(env)
    assert lane in ui.run("list")
    assert "Recorded message" in ui.run("message " + lane + " hi")
    assert calls == []
    assert lane in ui.run("show " + lane)
    assert calls == [(lane, 0, 20)]


def test_unread_aggregate_decrements_after_ack(env):
    lane = child(env)
    with env[1].transaction() as tx:
        row = tx.lane(lane)
        ids = [tx.message(row, "report", "parent", report=True) for _ in range(105)]
    env[0].ack_report(ids[0], command_id="ack-first", context=env[-2])
    assert env[0].read_view(env[-2], lane_id=lane)["lane"]["unread_reports"] == 104


def test_remote_model_permission_is_bound_and_rechecked(env):
    lane = child(env)
    app = app_for(env)
    app.config.ollama.allow_remote = True
    seen = []

    def revoke(arguments):
        seen.append(arguments)
        app.config.ollama.allow_remote = False
        return True, ""

    result = facade(env, revoke, app).run("cancel " + lane)
    assert seen[0]["remote_ollama_allowed"] is True
    assert "command_id" not in seen[0]
    assert "permission changed" in result
    assert env[0].inspect(lane, env[-2])["lane"]["status"] == "queued"
