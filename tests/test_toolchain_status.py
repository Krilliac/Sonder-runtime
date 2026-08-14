import subprocess
import io

import environment_probe
import server
import toolchain_status


def test_status_uses_only_discovered_path_and_fixed_arguments(monkeypatch):
    monkeypatch.setattr(
        environment_probe,
        "probe",
        lambda refresh=False: {"toolchains": {"cargo": "C:\\tools\\cargo.exe"}, "specialist_tools": {}},
    )
    seen = {}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            self.stdout = io.StringIO("cargo 9.9.9\n")
            self.stderr = io.StringIO("")
            self._returncode = 0

        def poll(self): return self._returncode
        def wait(self, timeout=None): return self._returncode
        def kill(self): self._returncode = -9
        @property
        def returncode(self): return self._returncode

    monkeypatch.setattr(toolchain_status.subprocess, "Popen", lambda argv, **kwargs: FakeProcess(argv, **kwargs))
    assert toolchain_status.status("cargo") == {
        "ok": True, "tool": "cargo", "output": "cargo 9.9.9"
    }
    assert seen["argv"] == ["C:\\tools\\cargo.exe", "--version"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["close_fds"] is True
    if toolchain_status.os.name == "nt":
        assert "creationflags" in seen["kwargs"]
    else:
        assert seen["kwargs"]["start_new_session"] is True
    assert "timeout" not in seen["kwargs"]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


def test_status_refuses_unknown_or_missing_tools(monkeypatch):
    monkeypatch.setattr(
        environment_probe,
        "probe",
        lambda refresh=False: {"toolchains": {}, "specialist_tools": {}},
    )
    assert toolchain_status.status("made-up") ["error"].startswith("unsupported tool")
    assert toolchain_status.status("cargo") == {
        "ok": False, "tool": "cargo", "error": "tool is not available on this host"
    }


def test_status_omits_untrusted_failure_output(monkeypatch):
    monkeypatch.setattr(
        environment_probe,
        "probe",
        lambda refresh=False: {"toolchains": {"cargo": "C:\\tools\\cargo.exe"}, "specialist_tools": {}},
    )
    monkeypatch.setattr(toolchain_status, "_run_bounded", lambda argv: ("error", "SECRET=not-returned"))
    assert toolchain_status.status("cargo") == {
        "ok": False, "tool": "cargo", "error": "status probe failed"
    }


def test_bounded_capture_terminates_overproducing_probe(monkeypatch):
    class LoudProcess:
        def __init__(self):
            self.stdout = io.StringIO("x" * (toolchain_status.MAX_OUTPUT_CHARS + 1))
            self.stderr = io.StringIO("")
            self._returncode = None
            self.killed = False

        def poll(self): return self._returncode
        def wait(self, timeout=None): return self._returncode
        def kill(self):
            self.killed = True
            self._returncode = -9
        @property
        def returncode(self): return self._returncode

    proc = LoudProcess()
    monkeypatch.setattr(toolchain_status.subprocess, "Popen", lambda *args, **kwargs: proc)
    outcome, output = toolchain_status._run_bounded(["cargo", "--version"])
    assert outcome == "output_limit"
    assert len(output) == toolchain_status.MAX_OUTPUT_CHARS
    assert proc.killed


def test_tree_termination_uses_taskkill_for_windows_probe(monkeypatch):
    seen = {}

    class RunningProcess:
        pid = 4242

        def poll(self):
            return None

        def kill(self):
            seen["fallback"] = True

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(toolchain_status.os, "name", "nt", raising=False)
    monkeypatch.setattr(toolchain_status.subprocess, "run", fake_run)
    toolchain_status._terminate_process_tree(RunningProcess())

    assert seen["argv"] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert seen["kwargs"]["shell"] is False
    assert "fallback" not in seen


def test_server_toolchain_status_records_only_safe_result(monkeypatch):
    records = []
    monkeypatch.setattr(server.toolchain_status_module, "status", lambda name, refresh=False: {
        "ok": True, "tool": name, "output": "cargo 9.9.9"
    })
    monkeypatch.setattr(server, "_record_direct_tool", lambda *args, **kwargs: records.append((args, kwargs)))
    assert server.toolchain_status("cargo") == '{"ok":true,"output":"cargo 9.9.9","tool":"cargo"}'
    assert records[0][0][0] == "toolchain_status"
    assert records[0][1]["ok"] is True


def test_local_agent_can_turn_discovery_into_a_fixed_status_probe(monkeypatch):
    responses = iter([
        '{"tool":"toolchain_status","args":{"name":"cargo"}}',
        '{"final":"Cargo is installed."}',
    ])
    called = []

    def generate(prompt, history=None):
        return next(responses)

    generate.last_usage = {}
    generate.last_response_meta = {}
    generate.num_predict_override = None
    monkeypatch.setattr(server, "_make_generate", lambda *args, **kwargs: generate)
    monkeypatch.setattr(
        server,
        "toolchain_status",
        lambda name, refresh=False: called.append((name, refresh)) or '{"ok":true,"tool":"cargo","output":"cargo 9.9.9"}',
    )
    assert server._agent_impl("verify the discovered cargo tool", max_steps=2).endswith("Cargo is installed.")
    assert called == [("cargo", False)]


def test_agent_help_distinguishes_source_runner_from_toolchain_probe():
    help_text = server._agent_tool_help()
    assert "source snippet only; never pass a shell command" in help_text
    assert "toolchain_status:" in help_text
