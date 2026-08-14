import subprocess

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

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="cargo 9.9.9\n", stderr="")

    monkeypatch.setattr(toolchain_status.subprocess, "run", fake_run)
    assert toolchain_status.status("cargo") == {
        "ok": True, "tool": "cargo", "output": "cargo 9.9.9"
    }
    assert seen["argv"] == ["C:\\tools\\cargo.exe", "--version"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == toolchain_status.TIMEOUT_SECONDS
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
    monkeypatch.setattr(
        toolchain_status.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="SECRET=not-returned"),
    )
    assert toolchain_status.status("cargo") == {
        "ok": False, "tool": "cargo", "error": "status probe failed"
    }


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
