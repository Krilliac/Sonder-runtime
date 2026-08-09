from types import SimpleNamespace

import project_scaffold
from scripts import scaffold_verify


def test_all_toolchains_missing_yields_all_skip_and_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(scaffold_verify.shutil, "which", lambda _tool: None)

    assert scaffold_verify.main() == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(project_scaffold.kinds())
    assert all("SKIP (toolchain missing)" in line for line in lines)


def test_failing_build_yields_failed_and_exit_one(monkeypatch, capsys):
    monkeypatch.setattr(
        scaffold_verify.shutil, "which",
        lambda tool: tool if tool == "cargo" else None,
    )
    monkeypatch.setattr(
        scaffold_verify.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="compiler detail\nlast failure", stdout="",
        ),
    )

    assert scaffold_verify.main() == 1
    assert "rust: FAILED - compiler detail last failure" in capsys.readouterr().out


def test_passing_cargo_build_yields_verified(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        scaffold_verify.shutil, "which",
        lambda tool: tool if tool == "cargo" else None,
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert (kwargs["cwd"] / "Cargo.toml").is_file()
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(scaffold_verify.subprocess, "run", fake_run)

    assert scaffold_verify.main() == 0
    assert calls[0][0] == ["cargo", "build"]
    assert calls[0][1]["timeout"] == 120
    assert "rust: VERIFIED" in capsys.readouterr().out.splitlines()


def test_cmake_scaffold_is_configured_and_built(monkeypatch, capsys):
    commands = []
    monkeypatch.setattr(
        scaffold_verify.shutil, "which",
        lambda tool: tool if tool == "cmake" else None,
    )

    def fake_run(command, **kwargs):
        commands.append(command)
        assert (kwargs["cwd"] / "CMakeLists.txt").is_file()
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(scaffold_verify.subprocess, "run", fake_run)

    assert scaffold_verify.main() == 0
    assert commands == [
        ["cmake", "-S", ".", "-B", "build"],
        ["cmake", "--build", "build", "--config", "Debug"],
    ]
    assert "cpp-cmake: VERIFIED" in capsys.readouterr().out.splitlines()
