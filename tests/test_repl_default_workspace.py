"""Work with no folder selected gets a default one instead of a question.

The console used to answer "can you make a cool webpage" with "which folder
should I use?" and hold the task.  It now creates a dated folder for the
session in the app-owned default workspace root (``~/Sonder/workspaces``, or
``[state].default_workspace_root``), selects it through the same permission
gate as ``/workspace-create``, prints where it is, and runs the task there.
Managed console work grants that root by design, without it being listed in
``[state].workspace_roots``; the tests at the end run the real managed
admission to pin the grant, its narrowness, and that configured roots
overlapping private control state are still refused.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import re
from pathlib import Path

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.adapters import creation_workspace
from sonder_runtime.adapters.creation_workspace import CreationWorkspaceError
from sonder_runtime.platform import paths


REPO_ROOT = Path(sonder_repl.__file__).resolve().parents[3]
CREATED = "(created because none was selected — /workspace <path> to use another folder)"
NAME = r"\d{4}-\d{2}-\d{2}-%s-[0-9a-f]{4}"


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    monkeypatch.delenv("SONDER_AUTO_WORKSPACE", raising=False)
    # The default root never depends on configured roots.
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)


@pytest.fixture
def default_root(monkeypatch, tmp_path):
    """A per-test stand-in for %USERPROFILE%\\Sonder\\workspaces."""
    root = tmp_path / "home" / "Sonder" / "workspaces"
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(root))
    return root


def _stub_managed_work(monkeypatch):
    # The managed admission itself is exercised for real further down.
    def managed_work(session_id, *, memory_database, **arguments):
        assert session_id and memory_database
        return server.workbench_agent(**arguments)

    monkeypatch.setattr(server, "_run_managed_repl_work", managed_work)


def _drive(monkeypatch, lines, *, gate=None, workbench=None):
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
    sonder_repl.main()
    return work, [call for call in gates if call[0] == "/workspace-create"]


def test_first_work_request_creates_announces_and_uses_a_default_workspace(
        monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)
    # The old fallback was the process cwd; make that the source checkout.
    monkeypatch.chdir(REPO_ROOT)

    work, gates = _drive(monkeypatch, ["can you make a cool webpage"])

    output = capsys.readouterr().out
    assert len(work) == 1, output
    created = Path(work[0]["project"])
    assert created.parent == default_root.resolve()
    assert re.fullmatch(NAME % "cool-webpage", created.name)
    assert created.is_dir()
    assert work[0]["prompt"] == "can you make a cool webpage"
    # Exactly the gate /workspace-create passes, for exactly this path.
    assert [(command, Path(path)) for command, path in gates] == [
        ("/workspace-create", created),
    ]
    assert "workspace: %s %s" % (created, CREATED) in output.splitlines()
    assert "which folder should I use" not in output


def test_follow_up_work_reuses_the_session_default_workspace(
        monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(monkeypatch, ["can you make a cool webpage", "now add dark mode"])

    output = capsys.readouterr().out
    assert [call["prompt"] for call in work] == ["can you make a cool webpage", "now add dark mode"]
    assert work[0]["project"] == work[1]["project"]
    assert len(list(default_root.iterdir())) == 1
    assert len(gates) == 1
    assert output.count(CREATED) == 1


def test_work_command_uses_the_default_workspace(monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(monkeypatch, ["/work build a todo app"])

    output = capsys.readouterr().out
    created = Path(work[0]["project"])
    assert re.fullmatch(NAME % "todo-app", created.name)
    assert created.parent == default_root.resolve()
    assert work[0]["prompt"] == "build a todo app"
    assert len(gates) == 1
    assert "workspace: %s %s" % (created, CREATED) in output.splitlines()


def test_a_new_session_gets_a_new_folder(monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)

    first, _gates = _drive(monkeypatch, ["make a cool webpage"])
    second, _gates = _drive(monkeypatch, ["make a cool webpage"])

    assert first[0]["project"] != second[0]["project"]
    assert len(list(default_root.iterdir())) == 2
    capsys.readouterr()


def test_gate_refusal_falls_back_to_the_ask_verbatim(monkeypatch, default_root, tmp_path, capsys):
    _stub_managed_work(monkeypatch)
    picked = tmp_path / "picked"
    picked.mkdir()

    def refuse_creation(command, _argument):
        if command == "/workspace-create":
            return False, "refused /workspace-create: plan mode blocks commands that change files"
        return True, ""

    work, gates = _drive(
        monkeypatch, ["can you make a cool webpage", "/workspace %s" % picked],
        gate=refuse_creation,
    )

    output = capsys.readouterr().out
    assert len(gates) == 1
    assert not default_root.exists()
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
        monkeypatch, default_root, capsys, permission_mode, mode, creates):
    _stub_managed_work(monkeypatch)
    permission_mode(mode)
    real_gate = sonder_repl._named_command_gate

    work, gates = _drive(monkeypatch, ["can you make a cool webpage"], gate=real_gate)

    output = capsys.readouterr().out
    assert len(gates) == 1
    assert default_root.exists() is creates
    assert bool(work) is creates
    assert (sonder_repl._WORKSPACE_ASK in output) is not creates


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_auto_workspace_off_keeps_the_ask(monkeypatch, default_root, capsys, value):
    _stub_managed_work(monkeypatch)
    monkeypatch.setenv("SONDER_AUTO_WORKSPACE", value)

    work, gates = _drive(monkeypatch, ["can you make a cool webpage"])

    output = capsys.readouterr().out
    assert output == sonder_repl._WORKSPACE_ASK + "\n"
    assert not work and not gates
    assert not default_root.exists()


def test_default_workspace_never_lands_in_the_source_checkout(monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)
    monkeypatch.chdir(REPO_ROOT)

    work, _gates = _drive(monkeypatch, ["make a cool webpage"])

    created = Path(work[0]["project"])
    assert not created.is_relative_to(REPO_ROOT)
    assert created.is_relative_to(default_root.resolve())
    capsys.readouterr()


@pytest.mark.parametrize("place, reason", [
    ("checkout", "default workspace root cannot be inside a Sonder source checkout"),
    ("state-home", "default workspace root overlaps Sonder's private control state"),
    ("relative", "default workspace root must be an absolute folder path"),
])
def test_an_unusable_default_root_says_why_and_asks(
        monkeypatch, tmp_path, capsys, place, reason):
    _stub_managed_work(monkeypatch)
    root = {
        "checkout": REPO_ROOT / ("never-created-" + tmp_path.name),
        "state-home": paths.default_home() / ("never-created-" + tmp_path.name),
        "relative": Path("never-created-" + tmp_path.name),
    }[place]
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(root))

    work, gates = _drive(monkeypatch, ["make a cool webpage"])

    output = capsys.readouterr().out
    assert not work and not gates
    assert not root.exists()
    assert "(no default folder: %s" % reason in output
    assert output.endswith(sonder_repl._WORKSPACE_ASK + "\n")


def test_no_user_home_says_why_and_asks(monkeypatch, capsys):
    _stub_managed_work(monkeypatch)
    monkeypatch.delenv("SONDER_DEFAULT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(paths, "default_workspace_root", lambda **_kwargs: None)

    work, gates = _drive(monkeypatch, ["make a cool webpage"])

    output = capsys.readouterr().out
    assert not work and not gates
    assert (
        "(no default folder: no user home is known to hold it; set"
        " [state].default_workspace_root)"
    ) in output
    assert output.endswith(sonder_repl._WORKSPACE_ASK + "\n")


def test_workspace_clear_returns_to_asking(monkeypatch, default_root, capsys):
    _stub_managed_work(monkeypatch)

    work, gates = _drive(
        monkeypatch, ["make a cool webpage", "/workspace clear", "now add dark mode"],
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


@pytest.mark.parametrize("platform_name, env, expected", [
    ("nt", {"USERPROFILE": r"C:\Users\ada"}, Path(r"C:\Users\ada") / "Sonder" / "workspaces"),
    ("posix", {"HOME": "/home/ada"}, Path("/home/ada") / "Sonder" / "workspaces"),
    ("nt", {"USERPROFILE": r"C:\Users\ada", "SONDER_DEFAULT_WORKSPACE_ROOT": r"D:\Made"},
     Path(r"D:\Made")),
    ("nt", {}, None),
    ("posix", {}, None),
])
def test_default_root_is_per_user_and_os(platform_name, env, expected):
    assert paths.default_workspace_root(env=env, platform_name=platform_name) == expected


def test_plan_creates_nothing_and_never_reuses_a_name(monkeypatch, default_root):
    tokens = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(creation_workspace.secrets, "token_hex", lambda _n: next(tokens))
    (default_root / "2026-10-01-cool-webpage-aaaa").mkdir(parents=True)

    target = creation_workspace.plan_session_workspace(
        "make a cool webpage", today=datetime.date(2026, 10, 1),
    )

    assert target == default_root.resolve() / "2026-10-01-cool-webpage-bbbb"
    assert not target.exists()


def test_create_refuses_a_link_planted_as_the_root(tmp_path, default_root):
    target = creation_workspace.plan_session_workspace("make a site")
    outside = tmp_path / "outside"
    outside.mkdir()
    default_root.parent.mkdir(parents=True)
    try:
        default_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(CreationWorkspaceError, match="link"):
        creation_workspace.create_session_workspace(target)
    assert not any(outside.iterdir())


# --- the real managed admission ------------------------------------------------


@pytest.fixture
def managed(monkeypatch, tmp_path):
    """Build a real application from a sonder.toml; return a work runner."""
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import load_config

    built = []

    def configure(workspace_roots=()):
        # The config file is private control state: keep it out of every
        # workspace root's ancestry, or admission refuses that root.
        source = tmp_path / "config" / "sonder.toml"
        source.parent.mkdir(exist_ok=True)
        source.write_text(
            "[state]\nworkspace_roots = %s\n" % json.dumps([str(root) for root in workspace_roots]),
            encoding="utf-8",
        )
        application = build_application(config=load_config(source))
        built.append(application)
        monkeypatch.setattr(server, "_application", lambda: application)
        return source

    yield configure
    for application in built:
        application.close_providers(timeout=5)


def _admitted_in_managed_session(**arguments):
    from sonder_runtime.interfaces.standalone_agent_lanes import controller_scope

    with controller_scope(server._application, project=arguments["project"]) as controller:
        controller.require_current()
        return controller._managed_session.report_metadata()


def test_default_workspace_is_granted_by_real_managed_repl_work(
        monkeypatch, managed, default_root, capsys):
    """No [state].workspace_roots at all: the default root alone is granted."""
    managed()
    # The per-conversation durable turn needs a real agent's host final, which
    # this fake does not produce; it is independent of which folder is admitted.
    monkeypatch.setattr(server, "_managed_repl_conversation_scope", contextlib.nullcontext)
    admitted = []

    def run_in_managed_session(**arguments):
        admitted.append((arguments["project"], _admitted_in_managed_session(**arguments)))
        return "work done"

    _drive(monkeypatch, ["make a cool webpage"], workbench=run_in_managed_session)

    output = capsys.readouterr().out
    assert "work refused" not in output, output
    assert len(admitted) == 1, output
    project, metadata = admitted[0]
    assert Path(project).parent == default_root.resolve()
    assert metadata["continuation_id"]


def test_the_default_root_grant_is_narrow(managed, default_root, tmp_path):
    managed()
    default_root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(PermissionError, match="no bounded current workspace grant"):
        sonder_repl._run_session_work(
            "4444444444444444", host_project="default", project=str(elsewhere),
            prompt="inspect",
        )


def test_configured_roots_overlapping_private_state_are_still_refused(
        monkeypatch, managed, default_root):
    """The default-root grant must not relax the overlap rule for configured roots."""
    state_home = paths.default_home().resolve()
    source = managed(workspace_roots=[state_home])
    folder = default_root / "2026-10-01-site-0000"
    folder.mkdir(parents=True)
    ran = []
    monkeypatch.setattr(server, "workbench_agent", lambda **arguments: ran.append(arguments))

    with pytest.raises(PermissionError) as refused:
        sonder_repl._run_session_work(
            "5555555555555555", host_project="default", project=str(folder), prompt="inspect",
        )

    message = str(refused.value)
    assert "configured workspace root %s overlaps Sonder's private control state" % state_home in message
    assert "remove it from [state].workspace_roots in %s" % source in message
    assert not ran


def test_an_unsafe_default_root_is_left_out_without_refusing_configured_work(
        monkeypatch, managed, tmp_path):
    configured = tmp_path / "projects"
    configured.mkdir()
    managed(workspace_roots=[configured])
    unsafe = paths.default_home() / ("unsafe-" + tmp_path.name)
    unsafe.mkdir(parents=True)
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(unsafe))
    monkeypatch.setattr(server, "workbench_agent", _admitted_in_managed_session)

    metadata = sonder_repl._run_session_work(
        "6666666666666666", host_project="default", project=str(configured), prompt="inspect",
    )
    assert metadata["continuation_id"]
    with pytest.raises(PermissionError, match="no bounded current workspace grant"):
        sonder_repl._run_session_work(
            "7777777777777777", host_project="default", project=str(unsafe), prompt="inspect",
        )
