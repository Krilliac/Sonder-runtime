"""Work with no folder selected gets a default one instead of a question.

The console used to answer "can you make a cool webpage" with "which folder
should I use?" and hold the task.  It now creates a dated folder for the
session, selects it through the same permission gate as
``/workspace-create``, prints where it is, and runs the task there.  The
folder sits under a configured ``[state].workspace_roots`` entry, not under
the state home: managed REPL work refuses every root that overlaps private
control state, so a state-home folder could never be worked in (the last
test here runs the real managed admission to pin that the chosen folder is
accepted).
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
from pathlib import Path

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.adapters import creation_workspace
from sonder_runtime.adapters.creation_workspace import CreationWorkspaceError


REPO_ROOT = Path(sonder_repl.__file__).resolve().parents[3]
CREATED = "(created because none was selected — /workspace <path> to use another folder)"


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    monkeypatch.delenv("SONDER_AUTO_WORKSPACE", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)


def _stub_managed_work(monkeypatch):
    # Host selection and admission are exercised for real by the last test.
    def managed_work(session_id, *, memory_database, **arguments):
        assert session_id and memory_database
        return server.workbench_agent(**arguments)

    monkeypatch.setattr(server, "_run_managed_repl_work", managed_work)


def _drive(monkeypatch, lines, *, roots=(), gate=None, workbench=None):
    """Run one console session over ``lines``; return (work calls, gate calls)."""
    work, gates = [], []
    feed = iter(tuple(lines) + ("/exit",))

    def named_gate(command, argument=""):
        gates.append((command, argument))
        return gate(command, argument) if gate else (True, "")

    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", named_gate)
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "containment_egress_refusal", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda _line: True)
    monkeypatch.setattr(sonder_repl.web_intents, "explicit_search", lambda _line: False)
    monkeypatch.setattr(server, "route_computer_use", lambda _line: None)
    monkeypatch.setattr(
        server, "workbench_agent",
        workbench or (lambda **kwargs: work.append(kwargs) or "work done"),
    )
    monkeypatch.setattr(server, "sonder", lambda *_a, **_k: pytest.fail("plain chat must not run"))
    if roots:
        monkeypatch.setenv("SONDER_FILE_ROOTS", os.pathsep.join(str(root) for root in roots))
    sonder_repl.main()
    return work, [call for call in gates if call[0] == "/workspace-create"]


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "projects"
    folder.mkdir()
    return folder


def test_first_work_request_creates_announces_and_uses_a_default_workspace(
        monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)
    # The old fallback was the process cwd; make that the source checkout.
    monkeypatch.chdir(REPO_ROOT)

    work, gates = _drive(monkeypatch, ["can you make a cool webpage"], roots=[root])

    output = capsys.readouterr().out
    assert len(work) == 1, output
    created = Path(work[0]["project"])
    assert created.parent == (root / "creations").resolve()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-cool-webpage-[0-9a-f]{4}", created.name)
    assert created.is_dir()
    assert work[0]["prompt"] == "can you make a cool webpage"
    # Exactly the gate /workspace-create passes, for exactly this path.
    assert [(command, Path(path)) for command, path in gates] == [
        ("/workspace-create", created),
    ]
    assert "workspace: %s %s" % (created, CREATED) in output.splitlines()
    assert "which folder should I use" not in output


def test_follow_up_work_reuses_the_session_default_workspace(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(
        monkeypatch, ["can you make a cool webpage", "now add dark mode"], roots=[root],
    )

    output = capsys.readouterr().out
    assert [call["prompt"] for call in work] == ["can you make a cool webpage", "now add dark mode"]
    assert work[0]["project"] == work[1]["project"]
    assert len(list((root / "creations").iterdir())) == 1
    assert len(gates) == 1
    assert output.count(CREATED) == 1


def test_work_command_uses_the_default_workspace(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(monkeypatch, ["/work build a todo app"], roots=[root])

    output = capsys.readouterr().out
    created = Path(work[0]["project"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-todo-app-[0-9a-f]{4}", created.name)
    assert created.parent == (root / "creations").resolve()
    assert work[0]["prompt"] == "build a todo app"
    assert len(gates) == 1
    assert "workspace: %s %s" % (created, CREATED) in output.splitlines()


def test_a_new_session_gets_a_new_folder(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)

    first, _gates = _drive(monkeypatch, ["make a cool webpage"], roots=[root])
    second, _gates = _drive(monkeypatch, ["make a cool webpage"], roots=[root])

    assert first[0]["project"] != second[0]["project"]
    assert len(list((root / "creations").iterdir())) == 2
    capsys.readouterr()


def test_gate_refusal_falls_back_to_the_ask_verbatim(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)
    picked = root / "picked"
    picked.mkdir()

    def refuse_creation(command, _argument):
        if command == "/workspace-create":
            return False, "refused /workspace-create: plan mode blocks commands that change files"
        return True, ""

    work, gates = _drive(
        monkeypatch, ["can you make a cool webpage", "/workspace %s" % picked],
        roots=[root], gate=refuse_creation,
    )

    output = capsys.readouterr().out
    assert len(gates) == 1
    assert not (root / "creations").exists()
    assert CREATED not in output
    # Today's question, unchanged, and the held task still resumes.
    assert output.startswith(sonder_repl._WORKSPACE_ASK + "\n")
    assert [(call["prompt"], call["project"]) for call in work] == [
        ("can you make a cool webpage", str(picked.resolve())),
    ]


@pytest.fixture
def permission_mode(tmp_path, monkeypatch):
    """Drive the real console gate in a chosen mode without persisting it."""
    import permission_modes as pm

    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved, saved_loaded = dict(pm._STATE), pm._LOADED

    def choose(mode):
        with pm._LOCK:
            pm._STATE.update(mode=mode, elevated=False, elevation_reason="")
        pm._LOADED = True

    try:
        yield choose
    finally:
        with pm._LOCK:
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.mark.parametrize("mode, creates", [
    ("plan", False),
    # Piped stdin: nobody is there to answer manual's approval prompt.
    ("manual", False),
    ("acceptEdits", True),
])
def test_the_real_workspace_create_gate_decides(
        monkeypatch, root, capsys, permission_mode, mode, creates):
    _stub_managed_work(monkeypatch)
    permission_mode(mode)
    real_gate = sonder_repl._named_command_gate

    work, gates = _drive(
        monkeypatch, ["can you make a cool webpage"], roots=[root], gate=real_gate,
    )

    output = capsys.readouterr().out
    assert len(gates) == 1
    assert (root / "creations").exists() is creates
    assert bool(work) is creates
    assert (sonder_repl._WORKSPACE_ASK in output) is not creates


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_auto_workspace_off_keeps_the_ask(monkeypatch, root, capsys, value):
    _stub_managed_work(monkeypatch)
    monkeypatch.setenv("SONDER_AUTO_WORKSPACE", value)

    work, gates = _drive(monkeypatch, ["can you make a cool webpage"], roots=[root])

    output = capsys.readouterr().out
    assert output == sonder_repl._WORKSPACE_ASK + "\n"
    assert not work and not gates
    assert not (root / "creations").exists()


def test_default_workspace_never_lands_in_the_source_checkout(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)
    monkeypatch.chdir(REPO_ROOT)

    # The running checkout is listed first; it is skipped, not used.
    work, _gates = _drive(monkeypatch, ["make a cool webpage"], roots=[REPO_ROOT, root])

    created = Path(work[0]["project"])
    assert not created.is_relative_to(REPO_ROOT)
    assert created.is_relative_to(root.resolve())
    assert not (REPO_ROOT / "creations").exists()
    capsys.readouterr()


def test_only_a_checkout_root_means_no_default_and_the_ask(monkeypatch, tmp_path, capsys):
    _stub_managed_work(monkeypatch)
    checkout = tmp_path / "sonder-checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "sonder_runtime").mkdir()

    work, gates = _drive(monkeypatch, ["make a cool webpage"], roots=[checkout])

    output = capsys.readouterr().out
    assert not work and not gates
    assert not (checkout / "creations").exists()
    assert "Sonder source checkout" in output
    assert sonder_repl._WORKSPACE_ASK in output


def test_no_configured_root_says_why_and_asks(monkeypatch, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(monkeypatch, ["make a cool webpage"])

    output = capsys.readouterr().out
    assert not work and not gates
    assert (
        "(no default folder: no workspace root is configured; add one to"
        " [state].workspace_roots in sonder.toml)"
    ) in output
    assert output.endswith(sonder_repl._WORKSPACE_ASK + "\n")


def test_workspace_clear_returns_to_asking(monkeypatch, root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(
        monkeypatch,
        ["make a cool webpage", "/workspace clear", "now add dark mode"],
        roots=[root],
    )

    output = capsys.readouterr().out
    assert [call["prompt"] for call in work] == ["make a cool webpage"]
    assert len(gates) == 1
    assert "workspace cleared; the next work request will ask for a directory" in output
    assert output.endswith(sonder_repl._WORKSPACE_ASK + "\n")


@pytest.mark.parametrize("task, slug", [
    ("can you make a cool webpage", "cool-webpage"),
    ("Build me a Flask API with auth and tests", "flask-api-auth-tests"),
    ("please write a snake game in python for my kid", "snake-game-python-kid"),
    ("café menu site", "cafe-menu-site"),
    ("做一个网页", "work"),
    ("", "work"),
    ("../../etc/passwd", "etc-passwd"),
    ("x" * 300, "x" * 32),
])
def test_session_workspace_name(task, slug):
    name = creation_workspace.session_workspace_name(
        task, today=datetime.date(2026, 10, 1), token="3f9a",
    )
    assert name == "2026-10-01-%s-3f9a" % slug
    assert creation_workspace._validate_run_id(name) == name


def test_plan_skips_unusable_roots_and_creates_nothing(tmp_path, root):
    missing = tmp_path / "missing"
    target = creation_workspace.plan_session_workspace(
        "make a cool webpage", [str(missing), str(REPO_ROOT), str(root)],
        today=datetime.date(2026, 10, 1),
    )
    assert target.parent == root.resolve() / "creations"
    assert target.name.startswith("2026-10-01-cool-webpage-")
    assert not (root / "creations").exists()
    assert not missing.exists()


def test_plan_reports_every_unusable_root(tmp_path):
    with pytest.raises(CreationWorkspaceError) as caught:
        creation_workspace.plan_session_workspace("x", [str(tmp_path / "missing"), str(REPO_ROOT)])
    message = str(caught.value)
    assert "not an existing directory" in message
    assert "workspace root cannot be inside a Sonder source checkout" in message


def test_plan_never_reuses_an_existing_folder_name(monkeypatch, root):
    tokens = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(creation_workspace.secrets, "token_hex", lambda _n: next(tokens))
    (root / "creations" / "2026-10-01-cool-webpage-aaaa").mkdir(parents=True)

    target = creation_workspace.plan_session_workspace(
        "make a cool webpage", [str(root)], today=datetime.date(2026, 10, 1),
    )

    assert target.name == "2026-10-01-cool-webpage-bbbb"


def test_create_refuses_a_link_planted_after_planning(tmp_path, root):
    target = creation_workspace.plan_session_workspace("make a site", [str(root)])
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "creations").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(CreationWorkspaceError, match="symlink"):
        creation_workspace.create_session_workspace(target)
    assert not any(outside.iterdir())


def test_default_workspace_is_accepted_by_real_managed_repl_work(
        monkeypatch, tmp_path, root, capsys):
    """The chosen folder must pass the real managed host selection and grant."""
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.interfaces.standalone_agent_lanes import controller_scope
    from sonder_runtime.platform.config import load_config

    # The config file is private control state: keep it out of the root's
    # ancestry, or admission refuses the root as overlapping it.
    source = tmp_path / "config" / "sonder.toml"
    source.parent.mkdir()
    source.write_text(
        "[state]\nworkspace_roots = %s\n" % json.dumps([str(root)]), encoding="utf-8",
    )
    application = build_application(config=load_config(source))
    monkeypatch.setattr(server, "_application", lambda: application)
    # The per-conversation durable turn needs a real agent's host final, which
    # this fake does not produce; it is independent of which folder is admitted.
    monkeypatch.setattr(server, "_managed_repl_conversation_scope", contextlib.nullcontext)
    admitted = []

    def run_in_managed_session(**arguments):
        with controller_scope(server._application, project=arguments["project"]) as controller:
            controller.require_current()
            admitted.append((arguments["project"], controller._managed_session.report_metadata()))
        return "work done"

    try:
        # No managed-work stub: the real host selection and grant run here.
        _drive(monkeypatch, ["make a cool webpage"], roots=[root], workbench=run_in_managed_session)
    finally:
        application.close_providers(timeout=5)

    output = capsys.readouterr().out
    assert "work refused" not in output, output
    assert len(admitted) == 1, output
    project, metadata = admitted[0]
    assert Path(project).parent == (root / "creations").resolve()
    assert metadata["continuation_id"]
