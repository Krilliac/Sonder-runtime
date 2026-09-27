"""Regression tests: ``doctor`` must inspect what the operator selected.

Two defects found by live CLI testing:

* ``doctor --set ollama.url=...`` (or ``--config``) reported the overridden URL
  in its ``config`` line while the ``ollama``/``ollama_residency`` probes
  re-loaded configuration from scratch and probed the default endpoint, so a
  dead selected endpoint was reported healthy.
* ``storage_models`` resolved the model root from the doctor process's own
  ``OLLAMA_MODELS`` and silently reported ``~/.ollama/models`` even when the
  running daemon stores models elsewhere.
"""
from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest

import sonder_doctor
from sonder_runtime.__main__ import main
from sonder_runtime.adapters.inference import ollama_model_root

_BLOB = "sha256-" + "c" * 64


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _fake_daemon(seen, *, root="/srv/models", reachable=True):
    def open_url(request, timeout=30, *, allow_remote=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        seen.append(url)
        if not reachable:
            raise OSError("connection refused")
        if url.endswith("/api/tags"):
            body = {"models": [{"name": "tiny:latest"}]}
        elif url.endswith("/api/ps"):
            body = {"models": []}
        elif url.endswith("/api/show"):
            body = {"modelfile": "# generated\nFROM %s/blobs/%s\n" % (root, _BLOB)}
        else:  # pragma: no cover - unexpected probe
            raise AssertionError(url)
        return _Response(json.dumps(body).encode("utf-8"))

    return open_url


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    monkeypatch.delenv("SONDER_CONFIG", raising=False)
    monkeypatch.delenv("SONDER_SECRETS", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    return path


@contextmanager
def _patched_endpoint(monkeypatch, opener):
    from sonder_runtime.adapters.inference import ollama_endpoint

    monkeypatch.setattr(ollama_endpoint, "open_url", opener)
    yield


def test_doctor_ollama_probes_use_the_cli_selected_endpoint(
    home, monkeypatch, capsys
):
    seen: list[str] = []
    with _patched_endpoint(monkeypatch, _fake_daemon(seen)):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [
                ("ollama", sonder_doctor._check_ollama),
                ("ollama_residency", sonder_doctor._check_ollama_residency),
            ],
        )
        assert main([
            "doctor", "--json", "--set", "ollama.url=http://127.0.0.1:11999",
        ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["status"] for c in payload["checks"]] == ["ok", "ok"]
    assert seen, "no probe was made"
    assert all(url.startswith("http://127.0.0.1:11999/") for url in seen), seen


def test_doctor_reports_selected_dead_endpoint_as_failing(
    home, monkeypatch, capsys
):
    def opener(request, timeout=30, *, allow_remote=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.startswith("http://127.0.0.1:11999/"):
            raise OSError("connection refused")
        return _fake_daemon([])(request, timeout, allow_remote=allow_remote)

    with _patched_endpoint(monkeypatch, opener):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [("ollama", sonder_doctor._check_ollama)],
        )
        assert main([
            "doctor", "--json", "--set", "ollama.url=http://127.0.0.1:11999",
        ]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["status"] == "fail"


def test_storage_models_uses_the_daemon_reported_root(
    home, tmp_path, monkeypatch, capsys
):
    daemon_root = tmp_path / "daemon-models"
    daemon_root.mkdir()
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    seen: list[str] = []
    with _patched_endpoint(
        monkeypatch, _fake_daemon(seen, root=daemon_root.as_posix())
    ):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [("storage_models", lambda: ("fail", "unbound"))],
        )
        assert main(["doctor", "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)["checks"][0]["detail"]
    assert str(daemon_root) in detail
    assert "reported by the local Ollama daemon" in detail


def test_storage_models_labels_an_undiscovered_default(
    home, monkeypatch, capsys
):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    with _patched_endpoint(monkeypatch, _fake_daemon([], reachable=False)):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [("storage_models", lambda: ("fail", "unbound"))],
        )
        assert main(["doctor", "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)["checks"][0]["detail"]
    assert "assumed" in detail and "OLLAMA_MODELS" in detail


def test_storage_models_skip_ollama_never_probes(home, monkeypatch, capsys):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    seen: list[str] = []
    with _patched_endpoint(monkeypatch, _fake_daemon(seen)):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [("storage_models", lambda: ("fail", "unbound"))],
        )
        assert main(["doctor", "--json", "--skip-ollama"]) == 0
    detail = json.loads(capsys.readouterr().out)["checks"][0]["detail"]
    assert seen == []
    assert "assumed" in detail


def test_storage_models_warns_when_process_env_disagrees_with_daemon(
    home, tmp_path, monkeypatch, capsys
):
    daemon_root = tmp_path / "daemon-models"
    daemon_root.mkdir()
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "elsewhere"))
    with _patched_endpoint(
        monkeypatch, _fake_daemon([], root=daemon_root.as_posix())
    ):
        monkeypatch.setattr(
            sonder_doctor,
            "default_checks",
            lambda: [("storage_models", lambda: ("fail", "unbound"))],
        )
        assert main(["doctor", "--json"]) == 0
    check = json.loads(capsys.readouterr().out)["checks"][0]
    assert check["status"] == "warn"
    assert str(daemon_root) in check["detail"]
    assert "differs" in check["detail"]


@pytest.mark.parametrize(
    "modelfile, expected",
    [
        ("FROM /opt/m/blobs/%s" % _BLOB, "/opt/m"),
        ("# x\nFROM qwen2.5:0.5b\nFROM /a/b/blobs/%s\n" % _BLOB, "/a/b"),
        ("FROM C:\\Users\\u\\.ollama\\models\\blobs\\%s" % _BLOB,
         "C:\\Users\\u\\.ollama\\models"),
        ("FROM relative/blobs/%s" % _BLOB, None),
        ("FROM /opt/m/other/%s" % _BLOB, None),
        ("FROM /opt/m/blobs/not-a-digest", None),
        ("", None),
    ],
)
def test_model_root_from_modelfile(modelfile, expected):
    assert ollama_model_root.model_root_from_modelfile(modelfile) == expected


def test_status_labels_the_model_root_source(home, monkeypatch, capsys):
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    assert main(["status", "--json"]) == 0
    models = json.loads(capsys.readouterr().out)["storage"]["models"]
    assert models and all(
        record["root_source"].startswith("assumed Ollama default")
        for record in models
    )


def test_discovery_refuses_remote_daemons():
    calls = []
    assert ollama_model_root.discover_daemon_model_root(
        "http://192.0.2.10:11434", allow_remote=True,
        opener=lambda *a, **k: calls.append(a),
    ) is None
    assert calls == []
