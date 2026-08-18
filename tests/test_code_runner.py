import json
import os
import subprocess
import sys
import time

import pytest

import code_runner


def test_python_run_success():
    out = code_runner.run_code("print('hello from runner')")
    assert out["ok"] is True
    assert out["returncode"] == 0
    assert "hello from runner" in out["stdout"]
    assert out["language"] == "python"


def test_python_run_supports_unicode_stdout():
    out = code_runner.run_code("print('\\U0001f3b2')")
    assert out["ok"] is True
    # The child runs with PYTHONIOENCODING=utf-8, so the character must survive as
    # itself. Asserting the escape text instead would pass only while the runner
    # was failing to decode -- that assertion pinned the bug it should catch.
    assert "\U0001f3b2" in out["stdout"]


def test_child_environment_forces_utf8_output():
    assert code_runner._child_environment()["PYTHONIOENCODING"] == "utf-8"


def test_default_run_uses_ephemeral_cwd_not_runtime_workspace(monkeypatch, tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))

    out = code_runner.run_code(
        "from pathlib import Path\nPath('model-write.txt').write_text('x')"
    )

    assert out["ok"] is True
    assert os.path.commonpath([str(root), out["cwd"]]) != str(root)
    assert not (root / "model-write.txt").exists()
    assert not os.path.exists(out["cwd"])


def test_run_process_scrubs_control_plane_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_APPROVAL_CODE", "approval-fixture")
    monkeypatch.setenv("SONDER_API_KEY", "api-fixture")
    monkeypatch.setenv("SONDER_OPENAI_API_KEY", "openai-fixture")

    out = code_runner._run_process(
        [
            sys.executable,
            "-c",
            "import os; print('|'.join(os.environ.get(k, '') for k in "
            "('SONDER_FILE_APPROVAL_CODE','SONDER_API_KEY','SONDER_OPENAI_API_KEY')))",
        ],
        str(tmp_path),
        "",
        10,
        "python",
    )

    assert out["ok"] is True
    assert out["stdout"].strip() == "||"


def test_run_process_closes_unrelated_inherited_handles(monkeypatch, tmp_path):
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, *, input, timeout):
            seen["input"] = input
            seen["timeout"] = timeout
            return "", ""

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)
    out = code_runner._run_process(
        [sys.executable, "-c", "pass"], str(tmp_path), "", 10, "python"
    )

    assert out["ok"] is True
    assert seen["close_fds"] is True


def test_detached_console_receives_scrubbed_environment(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(code_runner.os, "name", "nt", raising=False)
    monkeypatch.setenv("SONDER_AUTH_SECRET", "auth-fixture")

    class Proc:
        pid = 42

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return Proc()

    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)
    out = code_runner._launch_console("launch.bat", str(tmp_path), "python", 10)

    assert out["ok"] is True
    assert "SONDER_AUTH_SECRET" not in seen["env"]
    assert seen["close_fds"] is True


def test_python_run_failure_captures_stderr():
    out = code_runner.run_code("raise RuntimeError('boom')")
    assert out["ok"] is False
    assert out["returncode"] != 0
    assert "RuntimeError" in out["stderr"]
    assert "boom" in out["stderr"]


def test_stdin_is_passed_to_process():
    out = code_runner.run_code("import sys\nprint(sys.stdin.read().upper())", stdin="abc")
    assert out["ok"] is True
    assert "ABC" in out["stdout"]


def test_language_aliases_normalize():
    assert code_runner.normalize_language("py") == "python"
    assert code_runner.normalize_language("js") == "javascript"
    assert code_runner.normalize_language("ps1") == "powershell"


def test_unknown_language_is_rejected():
    with pytest.raises(ValueError):
        code_runner.normalize_language("brainfuck")


def test_extended_languages_are_supported():
    # Languages added after the live 7B runs; normalization must resolve
    # each canonical name and common alias.
    for alias, canonical in (
        ("rb", "ruby"), ("golang", "go"), ("ts", "typescript"),
        ("rs", "rust"), ("sh", "bash"), ("pl", "perl"),
    ):
        assert code_runner.normalize_language(alias) == canonical


def test_cwd_must_stay_inside_workspace(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    with pytest.raises(ValueError):
        code_runner.resolve_cwd(str(outside))


def test_relative_cwd_inside_workspace(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    child = root / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    assert code_runner.resolve_cwd("child") == os.path.realpath(str(child))


def test_timeout_is_reported():
    out = code_runner.run_code("while True:\n    pass", timeout=1)
    assert out["ok"] is False
    assert out["returncode"] is None
    assert "timed out" in out["error"]


def test_timeout_terminates_background_descendants(tmp_path):
    marker = tmp_path / "survived-timeout.txt"
    child = (
        "import pathlib, time; time.sleep(1.5); "
        "pathlib.Path(%r).write_text('survived')" % str(marker)
    )
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r])\n"
        "while True: time.sleep(.1)\n" % child
    )

    out = code_runner.run_code(code, timeout=1)

    assert out["ok"] is False
    assert "timed out" in out["error"]
    # The child would create this shortly after the parent deadline if the
    # runner only killed the direct interpreter.
    time.sleep(2)
    assert not marker.exists()


def test_missing_runtime_is_reported(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(code_runner.subprocess, "Popen", missing)
    out = code_runner.run_code("console.log(1)", language="js")
    assert out["ok"] is False
    assert "node executable not found" in out["error"]


def test_cpp_compiler_finds_visual_studio_vcvars_from_env(monkeypatch):
    monkeypatch.setattr(code_runner.shutil, "which", lambda exe: None)
    monkeypatch.setenv("SONDER_VCVARS64", r"C:\VS\VC\Auxiliary\Build\vcvars64.bat")
    monkeypatch.setattr(code_runner.os.path, "isfile", lambda path: path.endswith("vcvars64.bat"))

    assert code_runner._cpp_compiler() == (
        "msvc-vcvars",
        r"C:\VS\VC\Auxiliary\Build\vcvars64.bat",
    )


def test_cpp_compiler_finds_visual_studio_vcvars_from_vswhere(monkeypatch):
    monkeypatch.delenv("SONDER_VCVARS64", raising=False)
    monkeypatch.setattr(code_runner.shutil, "which", lambda exe: r"C:\VS\Installer\vswhere.exe" if exe == "vswhere" else None)
    monkeypatch.setattr(code_runner.os.path, "isfile", lambda path: path.endswith("vcvars64.bat") or path.endswith("vswhere.exe"))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=r"C:\Program Files\Microsoft Visual Studio\2022\Community" + "\n", stderr="")

    monkeypatch.setattr(code_runner.subprocess, "run", fake_run)

    name, path = code_runner._cpp_compiler()
    assert name == "msvc-vcvars"
    assert path.endswith(r"VC\Auxiliary\Build\vcvars64.bat")


def test_vcvars_fallback_uses_current_system_drive_not_a_data_drive(monkeypatch):
    roots = []
    monkeypatch.delenv("SONDER_VCVARS64", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setenv("SystemDrive", "E:")
    monkeypatch.setattr(code_runner.shutil, "which", lambda _name: None)
    monkeypatch.setattr(code_runner.os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(
        code_runner.glob,
        "glob",
        lambda root: roots.append(root) or [],
    )

    assert code_runner._find_visual_studio_vcvars() is None
    assert roots
    assert all(root.startswith("E:") for root in roots)
    assert not any(root.startswith("D:") for root in roots)


def test_run_cpp_uses_msvc_batch_when_vcvars_available(monkeypatch, tmp_path):
    source = tmp_path / "snippet.cpp"
    source.write_text("int main(){return 0;}", encoding="utf-8")
    seen = []

    monkeypatch.setattr(code_runner, "_cpp_compiler", lambda: ("msvc-vcvars", r"C:\VS\vcvars64.bat"))

    def fake_run_process(cmd, cwd, stdin, timeout, language):
        seen.append(cmd)
        if cmd[:2] == ["cmd", "/c"]:
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "language": language,
                "cwd": cwd,
                "timeout": timeout,
                "error": "",
            }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "ran",
            "stderr": "",
            "language": language,
            "cwd": cwd,
            "timeout": timeout,
            "error": "",
        }

    monkeypatch.setattr(code_runner, "_run_process", fake_run_process)

    out = code_runner._run_cpp(str(source), str(tmp_path), "", 10, str(tmp_path))
    assert out["ok"] is True
    assert seen[0][:2] == ["cmd", "/c"]
    assert seen[1][0].endswith("snippet.exe")
    assert (tmp_path / "sonder_build_msvc.bat").exists()


def test_run_code_window_launches_python_console(monkeypatch, tmp_path):
    seen = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, cwd, creationflags=0, env=None, close_fds=False):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["creationflags"] = creationflags
        seen["env"] = env
        seen["close_fds"] = close_fds
        return FakeProc()

    monkeypatch.setattr(code_runner.os, "name", "nt", raising=False)
    monkeypatch.setenv(code_runner.RUN_WINDOW_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)

    out = code_runner.run_code_window("print('hello window')", language="python")

    assert out["ok"] is True
    assert out["detached"] is True
    assert out["pid"] == 4321
    assert seen["cmd"][:2] == ["cmd", "/k"]
    assert seen["close_fds"] is True
    assert os.path.exists(os.path.join(out["run_dir"], "snippet.py"))
    assert os.path.exists(os.path.join(out["run_dir"], "launch.bat"))


def test_run_code_window_rejects_non_windows(monkeypatch):
    monkeypatch.setattr(code_runner.os, "name", "posix", raising=False)

    out = code_runner.run_code_window("print('x')", language="python")

    assert out["ok"] is False
    assert "/runwindow is only available on Windows" in out["error"]


def test_timeout_is_clamped(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0

        def communicate(self, *, input, timeout):
            seen["timeout"] = timeout
            return "", ""

    monkeypatch.setattr(code_runner.subprocess, "Popen", lambda *args, **kwargs: Proc())
    out = code_runner.run_code("print(1)", timeout=999)
    assert out["ok"] is True
    assert seen["timeout"] == code_runner.MAX_TIMEOUT


def test_format_result_includes_stdout_and_stderr():
    text = code_runner.format_result({
        "ok": False,
        "returncode": 2,
        "stdout": "out",
        "stderr": "err",
        "language": "python",
        "cwd": "C:/repo",
        "timeout": 3,
        "error": "",
    })
    assert "status: failed" in text
    assert "stdout:\nout" in text
    assert "stderr:\nerr" in text


def test_loop_repeats_until_success():
    calls = []

    def dispatch(action):
        calls.append(action)
        return {
            "ok": len(calls) == 3,
            "type": "probe",
            "summary": "try %d" % len(calls),
            "output": "",
        }

    result = code_runner.run_loop(
        [{"type": "probe"}],
        dispatch,
        max_iterations=5,
        stop_on_failure=False,
        stop_on_success=True,
    )
    assert len(calls) == 3
    assert result["ok"] is True
    assert result["stop_reason"] == "iteration 3 succeeded"


def test_loop_stops_on_failure_by_default():
    calls = []

    def dispatch(action):
        calls.append(action)
        return {"ok": False, "type": "probe", "summary": "nope", "output": ""}

    result = code_runner.run_loop([{"type": "probe"}], dispatch, max_iterations=5)
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["stop_reason"] == "action 1 failed in iteration 1"


def test_loop_rejects_empty_actions():
    with pytest.raises(ValueError):
        code_runner.run_loop([], lambda action: {"ok": True})


def test_format_loop_result_includes_iteration_output():
    text = code_runner.format_loop_result({
        "ok": False,
        "max_iterations": 2,
        "stop_reason": "action 1 failed in iteration 1",
        "iterations": [{
            "iteration": 1,
            "ok": False,
            "actions": [{
                "index": 1,
                "result": {
                    "ok": False,
                    "type": "code",
                    "summary": "returncode 1",
                    "output": "stderr:\nboom",
                },
            }],
        }],
    })
    assert "loop status: failed" in text
    assert "iteration 1: failed" in text
    assert "stderr:" in text


def test_server_run_code_returns_error_for_bad_request():
    import server

    assert server.run_code("", language="python").startswith("ERROR: code is empty")


def test_server_loop_rejects_bad_json():
    import server

    assert server.loop("{not json").startswith("ERROR: actions_json is not valid JSON")


def test_server_loop_runs_code_action():
    import server

    actions = json.dumps([{
        "type": "code",
        "language": "python",
        "code": "print('loop hello')",
    }])
    out = server.loop(actions, max_iterations=1)
    assert "loop status: ok" in out
    assert "loop hello" in out


def test_decode_child_output_prefers_utf8_but_keeps_legacy_bytes():
    """Neither codec is right for every child, so the fallback order matters.

    Python children are forced to utf-8 and node emits it regardless, so utf-8
    has to win when the bytes are valid utf-8. But PHP, Perl and native binaries
    follow the host code page, and 0xE9 alone is not valid utf-8 -- decoding it
    as utf-8 with errors="replace" would destroy output the old locale-based
    decoding preserved. Only bytes invalid in both may be replaced.
    """
    import locale

    # Valid utf-8 decodes as utf-8, not as the host code page.
    assert code_runner._decode_child_output(
        chr(0x1F3B2).encode("utf-8")) == chr(0x1F3B2)
    # A lone 0xE9 is not valid utf-8; it must survive via the host encoding
    # rather than becoming U+FFFD.
    host = locale.getpreferredencoding(False)
    recovered = code_runner._decode_child_output(b"caf" + bytes([0xE9]))
    assert recovered == (b"caf" + bytes([0xE9])).decode(host, errors="replace")
    if host.lower() in ("cp1252", "latin-1", "iso-8859-1"):
        assert recovered == "café"
        assert "�" not in recovered
    # Degenerate inputs stay harmless.
    assert code_runner._decode_child_output(b"") == ""
    assert code_runner._decode_child_output("already text") == "already text"


def test_python_run_still_returns_text_not_bytes():
    """The lane switched to binary pipes; callers must still receive str."""
    out = code_runner.run_code("print('plain ascii')")
    assert isinstance(out["stdout"], str)
    assert isinstance(out["stderr"], str)
    assert "plain ascii" in out["stdout"]


def test_python_run_forwards_stdin_as_utf8():
    """stdin is encoded on the way in now, so a non-ascii prompt must survive."""
    out = code_runner.run_code(
        "import sys; sys.stdout.write(sys.stdin.read().strip())",
        stdin="café " + chr(0x1F3B2),
    )
    assert out["ok"] is True, out["stderr"][:300]
    assert "café" in out["stdout"]
    assert chr(0x1F3B2) in out["stdout"]
