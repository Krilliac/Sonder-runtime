"""REPL ``/build`` and ``/fix-build``: routing, the console gate, the grant, the catalog.

The commands are wired in ``repl.main``'s slash chain and run one typed tool
call each through the composed gateway (``_build_execute_tool``), after the
console's own permission gate answered. These tests drive the real gate and
a real ``ToolApplicationFacade`` over the fake build services the build
executor tests use, so an approved ``/fix-build`` is shown to mint the fix's
scoped ``BuildFixGrant`` and an unapproved one to run nothing.
"""
from __future__ import annotations

import ast
import inspect

import pytest

import permission_modes as pm
import sonder_runtime.interfaces.repl.repl as repl
from sonder_runtime.adapters import command_catalog
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.interfaces.repl import command_router as cr
from sonder_runtime.interfaces.repl import style as S
from sonder_runtime.interfaces.repl.facades import build_tools as facade
from tests.test_build_executor import (
    compose_facade,
    fake_services,
)

pytestmark = pytest.mark.unit

JOB = "build-job-" + "ab" * 16
FIX = "build-fix-" + "cd" * 16


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    pm.reset_unattended_for_tests()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


@pytest.fixture
def manual(monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
    return ledger


@pytest.fixture
def console(monkeypatch):
    """An operator at the console whose answers the test scripts."""
    asked = []
    answers = []

    def confirm(question):
        asked.append(question)
        return answers.pop(0) if answers else False

    monkeypatch.setattr(repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(repl, "_confirm", confirm)
    return asked, answers


@pytest.fixture
def composed(tmp_path, monkeypatch):
    services = fake_services(tmp_path, tmp_path / "build")
    tools, audit, grants = compose_facade(tmp_path, services)
    monkeypatch.setattr(repl, "_typed_tools", lambda: tools)
    return services, tools, audit, grants


def _run_line(line, workspace):
    """What ``repl.main`` does with one slash line: usage, gate, branch."""
    cmd, _, arg = line.partition(" ")
    usage = repl._branch_usage_error(cmd, arg)
    if usage:
        return "usage", usage
    may_run, refusal = repl._named_command_gate(cmd, arg)
    if not may_run:
        return "gate", refusal
    repl._build_command(cmd, arg, workspace)
    return "ran", ""


# --- routing -------------------------------------------------------------------------


def test_the_slash_chain_routes_both_commands_to_the_build_facade():
    source = inspect.getsource(repl.main)
    assert 'elif cmd == "/build":' in source and 'elif cmd == "/fix-build":' in source
    tree = ast.parse(inspect.getsource(repl._build_command))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "dispatch" in called, "the branch runs the facade, not a build service"


def test_the_console_forwards_a_surface_decided_repl_request(tmp_path, monkeypatch):
    seen = []

    class Gateway:
        class graph:  # noqa: N801 - mimics the facade attribute path
            registry = compose_facade.__globals__["typed_tool_registry"]()

        def execute(self, request):
            seen.append(request)
            return type("Receipt", (), {"success": True, "output": '{"object": "build_model"}',
                                        "error_code": "", "error": ""})()

    monkeypatch.setattr(repl, "_typed_tools", lambda: Gateway())
    payload = repl._build_execute_tool("build_model", {"detail": "targets"}, str(tmp_path))
    assert payload == {"object": "build_model", "ok": True}
    request = seen[0]
    assert request.tool_name == "build_model" and request.arguments == {"detail": "targets"}
    assert request.scope.source == "repl" and request.scope.gate == "surface"
    assert request.scope.workspace_roots == (str(tmp_path),)
    assert request.request_id.startswith("repl-build-")


def test_not_composed_is_a_notice_and_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(repl, "_typed_tools", lambda: None)
    repl._build_command("/build", "model")
    assert facade.NOT_COMPOSED in capsys.readouterr().out


@pytest.mark.parametrize("line", [
    "/build run a b", "/build bogus", "/build status nope", "/fix-build",
    "/fix-build a b", "/fix-build game --preset p", "/fix-build restore " + FIX + " --force",
])
def test_a_malformed_line_prints_usage_before_the_gate(line, console):
    asked, _ = console
    cmd, _, arg = line.partition(" ")
    usage = repl._branch_usage_error(cmd, arg)
    assert "usage: /" in usage
    assert asked == [], "nobody is asked to approve a line that only prints usage"


# --- the console gate and the grant ---------------------------------------------------


def test_read_forms_run_without_a_prompt(tmp_path, manual, console, composed, capsys):
    asked, _ = console
    services, _tools, _audit, _grants = composed
    for line in ("/build", "/build model --detail targets", "/build status " + JOB,
                 "/fix-build status " + FIX):
        assert _run_line(line, str(tmp_path))[0] == "ran", line
    assert asked == []
    assert services.jobs.runs == [] and services.fix.started == []
    out = capsys.readouterr().out
    # each read reached its tool (an unknown job is the tool's own answer),
    # none was refused by a permission decision
    assert "build_model" in out and "JOB_NOT_FOUND" in out
    assert "PERMISSION_DENIED" not in out and "permission gate" not in out


def test_build_run_asks_as_execution_and_a_no_runs_nothing(tmp_path, manual, console, composed):
    asked, answers = console
    services, _tools, _audit, _grants = composed
    answers.append(False)
    outcome, refusal = _run_line("/build run game --config Debug", str(tmp_path))
    assert (outcome, refusal) == ("gate", "skipped /build")
    question = asked[0]
    assert question.command == "/build run game --config Debug"
    assert question.risk == "execution" and question.word == "[runs]"
    assert "C/C++" in question.summary
    assert services.jobs.planned == [] and services.jobs.runs == []


def test_an_approved_fix_mints_its_scoped_grant(tmp_path, manual, console, composed, capsys):
    asked, answers = console
    services, _tools, audit, grants = composed
    answers.append(True)
    assert _run_line("/fix-build game --config Debug", str(tmp_path)) == ("ran", "")
    assert len(asked) == 1 and asked[0].risk == "execution"
    assert "in-scope sources" in asked[0].summary
    assert len(services.fix.planned) == 1, "start() runs exactly the plan that was approved"
    assert services.fix.started[0][2].startswith("build_fix_grant:"), \
        "the console's approval became the fix's grant"
    assert audit.read()[-1]["policy_match"].endswith("permission:surface")
    assert manual.pending() == [], "no unattended call was recorded for approval"
    assert "refused" not in capsys.readouterr().out


def test_a_declined_fix_plans_nothing_and_mints_nothing(tmp_path, manual, console, composed):
    _asked, answers = console
    services, _tools, _audit, grants = composed
    answers.append(False)
    assert _run_line("/fix-build game", str(tmp_path)) == ("gate", "skipped /fix-build")
    assert services.fix.planned == [] and services.fix.started == [] and len(grants) == 0


def test_plan_mode_refuses_runs_and_fixes_but_not_reads(monkeypatch, ledger, console):
    asked, _ = console
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    for cmd, arg in (("/build", "run game"), ("/build", "trace src/a.cpp"),
                     ("/fix-build", "game"), ("/fix-build", "restore " + FIX)):
        may_run, refusal = repl._named_command_gate(cmd, arg)
        assert not may_run and refusal.startswith("refused %s:" % cmd), (cmd, arg)
    assert repl._named_command_gate("/build", "model") == (True, "")
    assert repl._named_command_gate("/fix-build", "status " + FIX) == (True, "")
    assert asked == []


def test_restore_asks_as_a_mutation(manual, console):
    asked, _ = console
    repl._named_command_gate("/fix-build", "restore " + FIX + " src/a.cpp")
    assert asked[0].risk == "mutation" and asked[0].word == "[writes]"


def test_a_piped_console_refuses_a_build_without_reading_input(monkeypatch, manual):
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(repl, "_confirm", lambda _q: pytest.fail("nobody to ask"))
    may_run, refusal = repl._named_command_gate("/fix-build", "game")
    assert not may_run and refusal.startswith("refused /fix-build:")


# --- rendering ------------------------------------------------------------------------


@pytest.fixture
def plain_caps():
    previous = S.caps()
    S.set_caps(S.detect_caps(stream=None, env={"NO_COLOR": "1", "TERM": "dumb"}))
    yield
    S.set_caps(previous)


def test_a_result_is_a_head_table_sections_and_footer(plain_caps):
    outcome = facade.BuildOutcome("result", "build_fix", {
        "ok": True, "object": "build_fix_status", "job_id": FIX, "status": "running",
        "target": "game", "config": "Debug", "display_command": ["cmake", "--build", "[BUILD]"],
        "files": [{"rel": "src/a.cpp", "before_sha256": "a"}],
        "next": "call build_fix_result with this job_id",
    }, "")
    lines = [S.strip_ansi(line) for line in
             repl._build_outcome_lines(outcome, "/fix-build game", 60, 1500)]
    assert lines[0] == "build_fix  running"
    assert any(line.split() == ["job_id", FIX] for line in lines)
    assert any(line.split() == ["target", "game"] for line in lines)
    assert any(line.split()[:2] == ["command", "cmake"] for line in lines)
    assert "  files (1)" in lines and "    rel=src/a.cpp, before_sha256=a" in lines
    assert any(line.strip().startswith("next: call build_fix_result") for line in lines)
    assert lines[-1].strip().startswith("done") and "1 tool" in lines[-1]
    assert all(len(line) <= 59 for line in lines)


def test_refusals_are_notices_and_tool_text_is_made_inert(plain_caps):
    outcome = facade.BuildOutcome("refused", "build_job", {
        "ok": False, "error_code": "UTILITY_TARGET_REFUSED",
        "message": "deploy\x1b]52;c;AAAA\x07 is a utility target"}, "")
    text = "\n".join(repl._build_outcome_lines(outcome, "/build run deploy", 80))
    assert "refused" in text and "UTILITY_TARGET_REFUSED" in text
    assert "\x1b]52" not in text and "\x07" not in text and "\\x1b]52" in text
    evil = facade.BuildOutcome("result", "build_model", {
        "object": "build_model", "target": "g\x1b[2Jame", "notes": ["‮evil"]}, "")
    text = "\n".join(repl._build_outcome_lines(evil, "/build", 80))
    assert "\x1b[2J" not in text and "‮" not in text


def test_piped_output_is_the_facade_text(monkeypatch, capsys):
    monkeypatch.setattr(repl, "_typed_tools", lambda: object())
    monkeypatch.setattr(repl, "_build_execute_tool", lambda tool, arguments, workspace="": {
        "ok": True, "object": "build_model", "system": "cmake"})
    repl._build_command("/build", "model")
    out = capsys.readouterr().out
    assert out.splitlines() == ["build_model: build_model", "  system: cmake"]


# --- catalog and help -----------------------------------------------------------------


def test_catalog_entries_grade_and_describe_the_commands():
    build = command_catalog.by_name("/build")
    fix = command_catalog.by_name("/fix-build")
    assert build.native and fix.native
    assert build.category == fix.category == "dev"
    assert build.risk == fix.risk == "execution"
    assert command_catalog.risk_word(build.risk) == "[runs]"
    assert "C/C++" in build.summary and "C/C++" in fix.summary
    assert "in-scope sources" in fix.summary
    help_text = command_catalog.help_text("dev")
    assert "/build" in help_text and "/fix-build" in help_text and "[runs]" in help_text


def test_console_map_matches_the_facade_specs():
    tools = command_catalog.console_tools()
    for spec in facade.BUILD_COMMAND_SPECS:
        assert set(tools[spec.name]) == set(spec.tools), spec.name
    assert command_catalog._NATIVE_TYPED_BRANCH_WORK == {
        spec.name: tuple(sorted(spec.tools)) for spec in facade.BUILD_COMMAND_SPECS}


@pytest.mark.parametrize("cmd, arg, expected", [
    ("/build", "", ("build_model",)),
    ("/build", "model --refresh", ("build_model",)),
    ("/build", "status " + JOB, ("build_job_result",)),
    ("/build", "cancel " + JOB, ("build_job_result",)),
    ("/build", "run game", ("build_job",)),
    ("/build", "trace src/a.cpp", ("build_job",)),
    ("/build", "status nope", ("build_job", "build_job_result", "build_model")),
    ("/fix-build", "game", ("build_fix",)),
    ("/fix-build", "status", ("build_fix",)),  # a target named "status"
    ("/fix-build", "status " + FIX, ("build_fix_result",)),
    ("/fix-build", "result " + FIX, ("build_fix_result",)),
    ("/fix-build", "cancel " + FIX, ("build_fix_result",)),
    ("/fix-build", "restore " + FIX, ("build_fix_restore",)),
    ("/fix-build", "", ("build_fix", "build_fix_restore", "build_fix_result")),
])
def test_narrowing_mirrors_the_facade_grammar(cmd, arg, expected):
    union = command_catalog.console_tools()[cmd]
    assert command_catalog.narrow_branch_tools(cmd, arg, union) == expected
    if len(expected) == 1:
        parser = facade.parse_build if cmd == "/build" else facade.parse_fix
        assert parser(arg)[0] == expected[0]


@pytest.mark.parametrize("sep", ["\xa0", "\x1c", "\x85", "\u2003", "\u3000"])
@pytest.mark.parametrize("verb", ["status", "result", "cancel", "restore"])
def test_unicode_separators_cannot_turn_a_fix_into_a_graded_read(verb, sep):
    """``str.split`` breaks on separators the facade keeps inside a word, so a
    read-looking line would have been run by the facade as ``build_fix``."""
    arg = verb + sep + FIX
    assert facade.parse_fix(arg)[0] == "build_fix"
    union = command_catalog.console_tools()["/fix-build"]
    narrowed = command_catalog.narrow_branch_tools("/fix-build", arg, union)
    assert "build_fix" in narrowed


def test_a_unicode_separated_fix_is_asked_as_execution(manual, console):
    asked, _ = console
    may_run, _refusal = repl._named_command_gate("/fix-build", "status\xa0" + FIX)
    assert not may_run and len(asked) == 1 and asked[0].risk == "execution"


def test_the_repl_refuses_a_tool_the_gate_did_not_grade(tmp_path, monkeypatch, capsys):
    """Defence in depth: if narrowing and parsing ever disagree again, the
    call the facade makes is refused rather than run under the approval."""
    calls = []
    monkeypatch.setattr(repl, "_typed_tools", lambda: object())
    monkeypatch.setattr(repl, "_build_execute_tool",
                        lambda tool, arguments, workspace="": calls.append(tool) or {"ok": True})
    monkeypatch.setattr(command_catalog, "narrow_branch_tools",
                        lambda cmd, arg, tools: ("build_fix_result",))
    monkeypatch.setattr(repl, "_stdout_is_interactive", lambda: False)
    repl._build_command("/fix-build", "game", str(tmp_path))
    assert calls == []
    assert "BUILD_GATE_MISMATCH" in capsys.readouterr().out


def test_usage_text_quoting_the_line_is_made_inert():
    usage = repl._branch_usage_error("/build", "run --\x1b]52;c;x\x07")
    assert usage and "\x1b" not in usage and "\x07" not in usage


def test_help_block_lists_the_commands_and_their_follow_ups():
    for text in ("/build [model|run|trace]", "/build status|result|cancel <build-job-id>",
                 "/fix-build <target>", "/fix-build status|result|cancel|restore <build-fix-id>"):
        assert text in repl.HELP


def test_fix_build_restore_verb_routes_to_the_restore_tool():
    assert facade.parse_fix("restore " + FIX) == ("build_fix_restore", {"job_id": FIX})
    assert facade.parse_fix("restore %s src/a.cpp 'src/b c.cpp'" % FIX) == (
        "build_fix_restore", {"job_id": FIX, "files": ["src/a.cpp", "src/b c.cpp"]})
    # "restore" without a fix id is a target, like "status"
    assert facade.parse_fix("restore") == ("build_fix", {"target": "restore"})


# --- plain-language routes ------------------------------------------------------------


@pytest.mark.parametrize("phrase, expected", [
    ("show the build model", "/build model"),
    ("describe the cmake build targets", "/build model"),
    ("build target game", "/build run game"),
    ("build the target engine_core.", "/build run engine_core"),
    ("fix the build for target game", "/fix-build game"),
    ("fix the failing build of target engine_core", "/fix-build engine_core"),
])
def test_whole_turn_build_phrases_route(phrase, expected):
    assert cr.resolve(phrase) == expected
    assert cr.explain(phrase)["source"] == "rule"


@pytest.mark.parametrize("phrase", [
    "fix the build",
    "fix the build so the tests pass",
    "build a game",
    "build target game and deploy it",
    "fix the build for target game; rm -rf /",
])
def test_broader_build_requests_are_not_hijacked(phrase):
    resolved = cr.resolve(phrase)
    assert resolved is None or not resolved.startswith(("/build", "/fix-build"))
