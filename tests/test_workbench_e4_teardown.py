"""E4: workbench.run_program tears down leftover descendants on normal and abnormal exit."""
from __future__ import annotations

import io
import json
import sys
import time

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench


def test_run_program_kills_surviving_descendants_on_normal_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")

    marker = tmp_path / "descendant-survived.txt"
    descendant_code = (
        "import pathlib, time; time.sleep(1.5); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    script = tmp_path / "spawn_and_exit.py"
    script.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}])\n"
        "print('parent-done', flush=True)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = workbench.run_program(
        sys.executable,
        args_json=json.dumps([str(script)]),
        cwd=str(tmp_path),
        timeout=10,
    )
    elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert "parent-done" in result["stdout"]
    assert elapsed < 3.0
    time.sleep(2.0)
    assert not marker.exists()


def test_run_program_abandons_child_on_abnormal_wait_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")

    abandoned = []

    class FakeProc:
        pid = 12345
        returncode = None
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise KeyboardInterrupt("simulated cancel")

        def kill(self):
            abandoned.append("kill")

    monkeypatch.setattr(
        workbench, "_resolve_program", lambda program, **kwargs: str(program),
    )
    monkeypatch.setattr(workbench.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(
        workbench,
        "_abandon_child",
        lambda proc, threads, tree=None: abandoned.append(("abandon", proc.pid)),
    )

    with pytest.raises(KeyboardInterrupt):
        workbench.run_program("fake-prog", timeout=5)

    assert abandoned == [("abandon", 12345)]
