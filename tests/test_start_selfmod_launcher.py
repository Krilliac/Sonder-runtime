"""Focused process-identity and serialized-launch tests for start-selfmod.ps1."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "start-selfmod.ps1"


def _powershell(launcher: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = ["powershell", "-NoProfile", "-File", str(launcher), *args]
    return subprocess.run(command, cwd=launcher.parents[1], env=env, text=True,
                          capture_output=True, timeout=30, check=False)


def _fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo with spaces"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LAUNCHER, repo / "scripts" / "start-selfmod.ps1")
    (repo / "scripts" / "selfmod_forever.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "launcher-test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _env(state_root: Path) -> dict[str, str]:
    result = os.environ.copy()
    result["SONDER_SELFMOD_STATE_ROOT"] = str(state_root)
    return result


# A cold Windows PowerShell start plus the launcher's mutex, git and
# Start-Process work routinely takes several seconds on a loaded hosted
# runner. The wait below returns as soon as the state file appears (or every
# launcher has exited), so this bound only matters when something is wrong.
STATE_TIMEOUT = 90.0


def _wait_for_state(state_path: Path, children: list[subprocess.Popen[str]]) -> bool:
    deadline = time.monotonic() + STATE_TIMEOUT
    while time.monotonic() < deadline:
        if state_path.exists():
            return True
        if all(child.poll() is not None for child in children):
            # Every launcher exited; a final check covers a publish that
            # landed just before the last one returned.
            return state_path.exists()
        time.sleep(0.1)
    return state_path.exists()


def _start_async(launcher: Path, repo: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], Path]:
    state_path = Path(env["SONDER_SELFMOD_STATE_ROOT"]) / "sonder" / "selfmod-continuous.json"
    child = subprocess.Popen(
        ["powershell", "-NoProfile", "-File", str(launcher), "-Python", sys.executable],
        cwd=repo, env=env, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if _wait_for_state(state_path, [child]):
        return child, state_path
    exit_code = child.poll()
    child.kill()
    raise AssertionError(
        f"launcher did not publish state within {STATE_TIMEOUT:.0f}s "
        f"(launcher exit code {exit_code}): {child.stderr.read() if child.stderr else ''}"
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is required")
def test_start_status_stop_uses_recorded_identity(tmp_path):
    repo = _fixture(tmp_path)
    state_root = tmp_path / "state"
    launcher = repo / "scripts" / "start-selfmod.ps1"
    env = _env(state_root)
    started, state_path = _start_async(launcher, repo, env)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert {
        "pid", "creation_time", "executable_path", "repo_path", "script_path", "script_sha256"
    } <= set(state)
    status = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(launcher), "-Status"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert status.returncode == 0 and f"pid={state['pid']}" in status.stdout
    stopped = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(launcher), "-Stop"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert stopped.returncode == 0 and f"stopped pid={state['pid']}" in stopped.stdout
    started.kill()
    assert not state_path.exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is required")
def test_stop_does_not_kill_pid_with_wrong_script_identity(tmp_path):
    repo = _fixture(tmp_path)
    state_root = tmp_path / "state"
    env = _env(state_root)
    launcher = repo / "scripts" / "start-selfmod.ps1"
    started, state_path = _start_async(launcher, repo, env)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    original_state = json.dumps(state)
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state["pid"] = foreign.pid
        state_path.write_text(json.dumps(state), encoding="utf-8")
        stopped = _powershell(launcher, "-Stop", env=env)
        assert stopped.returncode == 0
        assert foreign.poll() is None
        started.kill()
    finally:
        foreign.terminate()
        foreign.wait(timeout=10)
        state_path.write_text(original_state, encoding="utf-8")
        _powershell(launcher, "-Stop", env=env)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is required")
def test_unknown_state_is_retained(tmp_path):
    repo = _fixture(tmp_path)
    state_root = tmp_path / "state"
    state_dir = state_root / "sonder"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "selfmod-continuous.json"
    state_path.write_text(json.dumps({"pid": "not-a-pid"}), encoding="utf-8")
    launcher = repo / "scripts" / "start-selfmod.ps1"
    status = _powershell(launcher, "-Status", env=_env(state_root))
    assert status.returncode == 0 and "unknown/refused" in status.stdout
    assert state_path.exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is required")
def test_parallel_start_has_one_owner(tmp_path):
    repo = _fixture(tmp_path)
    state_root = tmp_path / "state"
    env = _env(state_root)
    launcher = repo / "scripts" / "start-selfmod.ps1"
    commands = [
        ["powershell", "-NoProfile", "-File", str(launcher), "-Python", sys.executable],
        ["powershell", "-NoProfile", "-File", str(launcher), "-Python", sys.executable],
    ]
    children = [subprocess.Popen(command, cwd=repo, env=env, text=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                for command in commands]
    state_path = state_root / "sonder" / "selfmod-continuous.json"
    assert _wait_for_state(state_path, children)
    _powershell(launcher, "-Stop", env=env)
    for child in children:
        if child.poll() is None:
            child.kill()
