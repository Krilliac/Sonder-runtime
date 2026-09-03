"""Boundary tests for WP1 selfmod_test_commands migration."""
import server
from sonder_runtime.domain.automation import selfmod_test_commands


def test_root_helper_is_identity_preserving_alias():
    assert server._selfmod_test_commands is selfmod_test_commands.selfmod_test_commands


def test_basic_syntax_command_for_python_files(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "foo.py").write_text("x = 1")
    run = {
        "workspace_path": str(workspace),
        "files": ["foo.py"],
        "maintenance_authorized": False,
    }
    commands = selfmod_test_commands.selfmod_test_commands(run, [])
    names = [name for name, _ in commands]
    assert "syntax" in names
    assert "targeted" in names
    assert "regression" in names
    assert "security" not in names


def test_maintenance_authorized_adds_security_command():
    run = {
        "workspace_path": "/tmp/fake",
        "files": ["a.py"],
        "maintenance_authorized": True,
    }
    commands = selfmod_test_commands.selfmod_test_commands(run, ["echo test", "echo sec"])
    names = [name for name, _ in commands]
    assert "security" in names


def test_empty_file_list_refuses():
    run = {
        "workspace_path": "/tmp/fake",
        "files": [],
        "maintenance_authorized": False,
    }
    commands = selfmod_test_commands.selfmod_test_commands(run, [])
    syntax_cmd = [cmd for name, cmd in commands if name == "syntax"][0]
    assert "refusal" in " ".join(syntax_cmd).lower() or "empty target" in " ".join(syntax_cmd).lower()
