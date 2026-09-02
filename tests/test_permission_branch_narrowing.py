"""Read forms of a many-tool slash command are graded as the read they are.

The chain gates grade a command by the strictest tool it can front, which is
right for an action the branch grammar does not name (``/todo frobnicate`` can
only be graded at the delete the command reaches) and wrong for ``/todo list``
-- or a bare ``/todo``, which lists -- once the effect classes fail closed for
unattended callers. ``command_catalog.narrow_branch_tools`` recognises the
read forms from each branch's own argument grammar; the stand-ins it produces
for work that fronts no registered tool are declared in
``permission_modes.READ_BRANCH_WORK`` and pinned here so neither table can
drift into a free-floating exemption. The bare forms it narrows are checked
against the branch source, so the table cannot outlive a grammar change.
"""
from __future__ import annotations

import pathlib

import pytest

import command_catalog
import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as sonder_serve
import sonder_runtime.interfaces.repl.repl as sonder_repl

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _manual_mode():
    before = pm.current_mode()
    pm.set_mode(pm.MANUAL)
    yield
    pm.set_mode(before)


SELFMOD_UNION = ("selfmod", "system_improvement_report")
TODO_UNION = ("task_create", "task_delete", "task_depend", "task_list", "task_plan",
              "task_progress", "task_show", "task_update")


@pytest.mark.parametrize("argument", [
    "", "status", "show", "history", "inspect RUN1", "diff RUN1", "tests RUN1", "backups",
])
def test_selfmod_read_forms_and_the_bare_form_narrow_to_the_read_stand_in(argument):
    assert command_catalog.narrow_branch_tools("/selfmod", argument, SELFMOD_UNION) == (
        "selfmod_status",
    )


@pytest.mark.parametrize("argument", [
    "deploy RUN1", "rollback RUN1", "run", "approve RUN1", "mode auto", "prune-backups",
    "frobnicate",
])
def test_selfmod_writes_and_unnamed_actions_keep_the_strictest_grade(argument):
    assert command_catalog.narrow_branch_tools("/selfmod", argument, SELFMOD_UNION) == SELFMOD_UNION


def test_selfmod_opportunities_is_the_registered_read_it_calls():
    assert command_catalog.narrow_branch_tools("/selfmod", "opportunities", SELFMOD_UNION) == (
        "system_improvement_report",
    )


@pytest.mark.parametrize("argument, expected", [
    ("", ("task_list",)), ("list", ("task_list",)), ("ls", ("task_list",)),
    ("show 12", ("task_show",)), ("progress", ("task_progress",)),
    ("add buy milk", ("task_create",)), ("done 12", ("task_update",)),
    ("delete 12", ("task_delete",)), ("plan", ("task_plan",)),
])
def test_task_actions_and_the_bare_form_narrow_to_the_member_they_reach(argument, expected):
    assert command_catalog.narrow_branch_tools("/todo", argument, TODO_UNION) == expected


def test_an_action_the_task_grammar_does_not_name_keeps_the_strictest_grade():
    assert command_catalog.narrow_branch_tools("/todo", "frobnicate 12", TODO_UNION) == TODO_UNION


def test_a_task_member_the_surface_does_not_carry_keeps_the_union():
    """The HTTP aliases support four operations; a delete there stays the union."""
    http_union = ("task_create", "task_list", "task_show", "task_update")
    assert command_catalog.narrow_branch_tools("/todo", "delete 12", http_union) == http_union
    assert command_catalog.narrow_branch_tools("/todo", "list", http_union) == ("task_list",)
    assert command_catalog.narrow_branch_tools("/todo", "", http_union) == ("task_list",)


def test_fact_forget_and_remember_are_told_apart():
    union = ("sonder_forget_fact", "sonder_remember_fact")
    assert command_catalog.narrow_branch_tools("/fact", "forget 3 confirm", union) == (
        "sonder_forget_fact",
    )
    assert command_catalog.narrow_branch_tools("/fact", "the sky is blue", union) == (
        "sonder_remember_fact",
    )
    assert command_catalog.narrow_branch_tools("/fact", "", union) == union


@pytest.mark.parametrize("cmd, argument, expected", [
    ("/mcp", "", ("mcp_runtime_status",)),
    ("/mcp", "status", ("mcp_runtime_status",)),
    ("/convergence", "", ("mcp_runtime_status",)),
    ("/mcp", "refresh", ("mcp",)),
    ("/goal", "", ("goal_status",)),
    ("/goal", "show", ("goal_status",)),
    ("/goal", "set ship it", ("goal",)),
    ("/goal", "note shipped the parser", ("goal",)),
    ("/training", "", ("training_status",)),
    ("/training", "status", ("training_status",)),
    ("/training", "start --confirm", ("training",)),
    ("/runtime", "status", ("runtime_policy_status",)),
    ("/runtime", "set fast=x", ("runtime_policy_status", "runtime_policy_update")),
    ("/stash", "", ("runtime_source_stash_status",)),
    ("/stash", "save", ("runtime_source_stash", "runtime_source_stash_status")),
    ("/emotion", "status", ("emotion_vector_status",)),
    ("/emotion", "joy=0.8", ("update_emotion_vectors",)),
])
def test_other_read_forms_narrow_and_writes_do_not(cmd, argument, expected):
    unions = {
        "/mcp": ("mcp",), "/convergence": ("mcp",), "/goal": ("goal",),
        "/training": ("training",),
        "/runtime": ("runtime_policy_status", "runtime_policy_update"),
        "/stash": ("runtime_source_stash", "runtime_source_stash_status"),
        "/emotion": ("update_emotion_vectors",),
    }
    assert command_catalog.narrow_branch_tools(cmd, argument, unions[cmd]) == expected


def test_an_unrecognised_command_keeps_its_union_unchanged():
    union = ("file_write",)
    assert command_catalog.narrow_branch_tools("/write", "notes.txt hello", union) == union
    assert command_catalog.narrow_branch_tools("", "", union) == union


# --- the bare forms are the reads the branches run ------------------------


def test_the_bare_forms_narrowed_here_are_the_reads_their_branches_run():
    """Each bare form the table treats as a read is one in the branch source.

    The table is a claim about five hand-written branches; this reads the
    claim back from them, so changing what a bare command does without
    changing the table fails here rather than in a piped script.
    """
    import inspect

    assert 'str(arg or "status")' in inspect.getsource(server._selfmod_command)
    assert 'str(arg or "status")' in inspect.getsource(server._mcp_command)
    assert 'str(arg or "show")' in inspect.getsource(server._goal_command)
    assert 'command_text(text or "plan")' in inspect.getsource(server._training_command)
    bare_lists = 'if not text or text.lower() in ("list", "ls"):'
    for module in (sonder_repl, sonder_serve):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert bare_lists in source, module.__name__


# --- the stand-ins cannot drift -------------------------------------------


def test_read_stand_ins_are_declared_once_and_each_is_produced_by_a_rule():
    assert set(pm.READ_BRANCH_WORK) == set(command_catalog.READ_STAND_INS)
    produced = set()
    for cmd, argument in (
        ("/selfmod", "status"), ("/goal", "show"), ("/training", "status"),
    ):
        produced |= set(command_catalog.narrow_branch_tools(cmd, argument, ("x",)))
    for name, grade in pm.READ_BRANCH_WORK.items():
        assert grade == "safe", name
        assert name in produced, "%s is declared but no narrowing rule produces it" % name
        assert command_catalog.by_name("/" + name) is None, (
            "%s is a registered tool now; grade it in the catalog instead" % name
        )
        assert pm.risk_of(name) == "safe", name


def test_a_read_stand_in_is_a_read_at_the_gate_in_every_mode():
    for mode in pm.MODES:
        for name in pm.READ_BRANCH_WORK:
            assert pm.decide(name, mode=mode, interactive=False, rule_lookup=lambda _t: None).action == pm.ALLOW, (name, mode)


# --- and the surfaces use it -----------------------------------------------


def test_a_status_read_is_not_refused_unattended_on_the_control_chain():
    assert not server.control_command("/selfmod status").startswith("refused")
    assert not server.control_command("/selfmod").startswith("refused")
    assert "status:" in server.control_command("/mcp status")
    assert not server.control_command("/goal").startswith("refused")


def test_a_selfmod_deploy_is_still_refused_unattended_on_the_control_chain():
    assert server.control_command("/selfmod deploy RUN1").startswith("refused /selfmod")


def test_the_console_named_gate_narrows_by_argument(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    assert sonder_repl._named_command_gate("/todo") == (True, "")
    assert sonder_repl._named_command_gate("/todo", "list") == (True, "")
    may_run, refusal = sonder_repl._named_command_gate("/todo", "delete 12")
    assert not may_run
    assert refusal.startswith("refused /todo:")
    assert "nobody is here" in refusal


def test_the_console_named_gate_keeps_an_unnamed_action_strict(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    may_run, refusal = sonder_repl._named_command_gate("/todo", "frobnicate 12")
    assert not may_run
    assert refusal.startswith("refused /todo:")
