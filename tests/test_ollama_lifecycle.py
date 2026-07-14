import json
from pathlib import Path
from types import SimpleNamespace

import ollama_lifecycle
import process_liveness


def test_resident_models_accepts_name_and_model_fields():
    assert ollama_lifecycle.resident_models({
        "models": [{"name": "Sonder:latest"}, {"model": "qwen2.5:3b"}],
    }) == {"sonder:latest", "qwen2.5:3b"}
    assert ollama_lifecycle.resident_models({"models": "invalid"}) == set()


def test_windows_process_inspector_parses_single_json_object(monkeypatch):
    monkeypatch.setattr(ollama_lifecycle, "_is_windows", lambda: True)
    payload = {
        "ProcessId": 42,
        "ParentProcessId": 7,
        "ParentAlive": False,
        "ExecutablePath": r"C:\Ollama\llama-server.exe",
        "CommandLine": "llama-server.exe --list-devices --offline --verbose",
    }
    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    assert ollama_lifecycle._windows_llama_processes(runner) == [payload]
    assert seen[0][0][:2] == ["powershell", "-NoProfile"]
    assert seen[0][1]["timeout"] == 5


def test_cleanup_terminates_only_stable_trusted_orphan_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(ollama_lifecycle, "_is_windows", lambda: True)
    trusted = tmp_path / "Ollama"
    trusted.mkdir()
    monkeypatch.setattr(ollama_lifecycle, "_trusted_ollama_roots", lambda: (trusted,))
    executable = str(trusted / "lib" / "ollama" / "llama-server.exe")
    rows = [{
        "ProcessId": 42,
        "ParentProcessId": 7,
        "ParentAlive": False,
        "ExecutablePath": executable,
        "CommandLine": (
            'llama-server.exe --port 62001 --host 127.0.0.1 '
            '--no-webui --offline --verbose'
        ),
    }]
    identities = []
    terminated = []
    sleeps = []
    monkeypatch.setattr(
        ollama_lifecycle.process_liveness,
        "probe_process",
        lambda pid: (
            identities.append(pid) or process_liveness.PROCESS_ALIVE,
            "windows:123",
        ),
    )

    result = ollama_lifecycle.cleanup_orphaned_discovery_probes(
        grace_seconds=0.15,
        poll_seconds=0.1,
        inspector=lambda: list(rows),
        terminator=lambda pid, identity: terminated.append((pid, identity)) or True,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert result["terminated"] == [42]
    assert terminated == [(42, "windows:123")]
    assert identities == [42, 42]
    assert sleeps == [0.15, 0.1]


def test_cleanup_protects_model_runner_and_untrusted_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(ollama_lifecycle, "_is_windows", lambda: True)
    trusted = tmp_path / "Ollama"
    trusted.mkdir()
    monkeypatch.setattr(ollama_lifecycle, "_trusted_ollama_roots", lambda: (trusted,))
    model_runner = {
        "ProcessId": 50,
        "ParentAlive": False,
        "ExecutablePath": str(trusted / "llama-server.exe"),
        "CommandLine": "llama-server.exe --model blob --port 61000 --offline",
    }
    untrusted_probe = {
        "ProcessId": 51,
        "ParentAlive": False,
        "ExecutablePath": str(tmp_path / "Other" / "llama-server.exe"),
        "CommandLine": "llama-server.exe --list-devices --offline --verbose",
    }
    untrusted_model = dict(untrusted_probe)
    untrusted_model.update({
        "ProcessId": 52,
        "CommandLine": "llama-server.exe --model unrelated --port 61000",
    })
    result = ollama_lifecycle.cleanup_orphaned_discovery_probes(
        grace_seconds=0.0,
        inspector=lambda: [model_runner, untrusted_probe, untrusted_model],
        terminator=lambda *args: (_ for _ in ()).throw(AssertionError("must not kill")),
        sleeper=lambda seconds: None,
    )

    assert result["terminated"] == []
    assert result["protected_model_runners"] == [50]


def test_cleanup_model_runner_requires_explicit_opt_in_and_trusted_blob(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(ollama_lifecycle, "_is_windows", lambda: True)
    trusted_binary = tmp_path / "Ollama"
    trusted_models = tmp_path / "models"
    trusted_binary.mkdir()
    (trusted_models / "blobs").mkdir(parents=True)
    monkeypatch.setattr(
        ollama_lifecycle, "_trusted_ollama_roots", lambda: (trusted_binary,),
    )
    monkeypatch.setattr(
        ollama_lifecycle, "_trusted_model_roots", lambda: (trusted_models,),
    )
    blob = trusted_models / "blobs" / ("sha256-" + "a" * 64)
    row = {
        "ProcessId": 77,
        "ParentAlive": False,
        "ExecutablePath": str(
            trusted_binary / "lib" / "ollama" / "llama-server.exe"
        ),
        "CommandLine": (
            'llama-server.exe --model "%s" --port 61234 --host 127.0.0.1 '
            "--no-webui --offline"
        ) % blob,
    }
    monkeypatch.setattr(
        ollama_lifecycle.process_liveness,
        "probe_process",
        lambda pid: (process_liveness.PROCESS_ALIVE, "windows:456"),
    )

    protected = ollama_lifecycle.cleanup_orphaned_discovery_probes(
        grace_seconds=0.0,
        inspector=lambda: [row],
        terminator=lambda *args: True,
        sleeper=lambda seconds: None,
    )
    assert protected["terminated_model_runners"] == []
    assert protected["protected_model_runners"] == [77]

    terminated = []
    cleaned = ollama_lifecycle.cleanup_orphaned_discovery_probes(
        grace_seconds=0.0,
        allow_model_runners=True,
        inspector=lambda: [row],
        terminator=lambda pid, identity: terminated.append((pid, identity)) or True,
        sleeper=lambda seconds: None,
    )
    assert cleaned["terminated_model_runners"] == [77]
    assert cleaned["protected_model_runners"] == []
    assert terminated == [(77, "windows:456")]
