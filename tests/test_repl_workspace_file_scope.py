"""A selected ``/workspace`` scopes the console file commands.

``/workspace <dir>`` used to affect only conversational work requests:
``/write a.txt`` still wrote into the Sonder checkout (the process cwd) and
``/write <dir>/a.txt`` could be refused as outside the roots.  With a
workspace selected, relative paths resolve against it, escapes (``..``,
absolute paths elsewhere, symlinks) are refused, and selection never widens
file authority beyond Sonder's configured file roots.
"""
import os

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def _drive(monkeypatch, lines):
    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_a, **_k: pytest.fail("file commands must not run a model turn"),
    )
    sonder_repl.main()


@pytest.fixture
def granted(tmp_path, monkeypatch):
    root = tmp_path / "granted"
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    return root, workspace


def test_relative_paths_resolve_against_the_selected_workspace(
        monkeypatch, capsys, granted):
    _root, workspace = granted

    _drive(monkeypatch, (
        "/workspace %s" % workspace,
        "/write notes.txt hello there",
        "/append notes.txt !",
        "/edit notes.txt|hello|goodbye",
        "/mkdir sub/deeper",
        "/read notes.txt",
    ))

    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "goodbye there!"
    assert (workspace / "sub" / "deeper").is_dir()
    out = capsys.readouterr().out
    assert "goodbye there!" in out
    assert "ERROR" not in out
    # Nothing landed in the process cwd (the checkout).
    assert not (sonder_repl.file_ops.workspace_root() / "notes.txt").exists()


def test_paths_that_escape_the_workspace_are_refused(
        monkeypatch, capsys, granted):
    root, workspace = granted
    outside = root / "outside"
    outside.mkdir()
    link = workspace / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        link = None

    lines = [
        "/workspace %s" % workspace,
        "/write ../escape.txt no",
        "/write %s no" % (root / "absolute.txt"),
        "/mkdir ../newdir",
        "/read ../outside",
    ]
    if link is not None:
        lines.append("/write link/through-link.txt no")
    _drive(monkeypatch, lines)

    out = capsys.readouterr().out
    assert out.count("is outside the selected workspace") == len(lines) - 1
    assert not (root / "escape.txt").exists()
    assert not (root / "absolute.txt").exists()
    assert not (root / "newdir").exists()
    assert not (outside / "through-link.txt").exists()


def test_selecting_a_workspace_outside_the_file_roots_grants_nothing(
        monkeypatch, capsys, tmp_path, granted):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    _drive(monkeypatch, (
        "/workspace %s" % elsewhere,
        "/write a.txt nope",
        "/write %s nope" % (elsewhere / "b.txt"),
    ))

    out = capsys.readouterr().out
    assert out.count("is outside Sonder's file roots") == 2
    assert list(elsewhere.iterdir()) == []


def test_without_a_workspace_file_commands_keep_the_default_roots(
        monkeypatch, capsys, granted):
    root, _workspace = granted
    target = root / "plain.txt"

    _drive(monkeypatch, ("/write %s plain" % target,))

    assert target.read_text(encoding="utf-8") == "plain"


def test_workspace_file_scope_caps_the_file_layer(granted):
    root, workspace = granted
    (root / "sibling.txt").write_text("x", encoding="utf-8")

    with sonder_repl._workspace_file_scope(str(workspace)):
        refused = server.file_read(path=str(root / "sibling.txt"))
    allowed = server.file_read(path=str(root / "sibling.txt"))

    assert refused.startswith("ERROR") and "outside allowed roots" in refused
    assert not allowed.startswith("ERROR")
