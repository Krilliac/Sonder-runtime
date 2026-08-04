"""GGUF/USB import helpers in setup_alias (facts. portable-model path)."""
from __future__ import annotations

import os

import pytest

import setup_alias


def _touch(path):
    open(path, "w").close()
    return path


def test_find_gguf_top_level_and_one_deep(tmp_path):
    _touch(tmp_path / "top.gguf")
    (tmp_path / "models").mkdir()
    _touch(tmp_path / "models" / "nested.gguf")
    _touch(tmp_path / "models" / "readme.txt")
    found = setup_alias.find_gguf_files([str(tmp_path)])
    names = sorted(os.path.basename(f) for f in found)
    assert names == ["nested.gguf", "top.gguf"]


def test_find_gguf_accepts_direct_file(tmp_path):
    target = _touch(tmp_path / "model.gguf")
    assert setup_alias.find_gguf_files([str(target)]) == [str(target)]


def test_find_gguf_ignores_non_gguf_and_missing(tmp_path):
    _touch(tmp_path / "weights.bin")
    assert setup_alias.find_gguf_files([str(tmp_path)]) == []
    assert setup_alias.find_gguf_files(["/does/not/exist"]) == []


def test_find_gguf_dedupes_realpath(tmp_path):
    real = _touch(tmp_path / "m.gguf")
    link = tmp_path / "link.gguf"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    # Passing both the file and its dir must not double-count the same file.
    found = setup_alias.find_gguf_files([str(real), str(tmp_path)])
    reals = {os.path.realpath(f) for f in found}
    assert reals == {os.path.realpath(str(real))}


def test_import_gguf_rejects_missing_file(tmp_path):
    ok, message = setup_alias.import_gguf(
        "ollama", str(tmp_path / "nope.gguf"), "sonder:latest", env={},
    )
    assert not ok and "not found" in message


def test_import_gguf_rejects_non_gguf(tmp_path):
    other = _touch(tmp_path / "model.bin")
    ok, message = setup_alias.import_gguf(
        "ollama", str(other), "sonder:latest", env={},
    )
    assert not ok and "not a .gguf" in message


def test_import_gguf_writes_from_modelfile(tmp_path, monkeypatch):
    gguf = _touch(tmp_path / "facts.gguf")
    captured = {}

    def fake_run(ollama, argv, *, env):
        # Capture the Modelfile contents the create call references.
        assert argv[0] == "create"
        modelfile = argv[argv.index("-f") + 1]
        captured["contents"] = open(modelfile, encoding="utf-8").read()
        captured["model_name"] = argv[1]

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(setup_alias, "_run", fake_run)
    ok, message = setup_alias.import_gguf(
        "ollama", str(gguf), "sonder:latest", env={},
    )
    assert ok, message
    assert captured["model_name"] == "sonder:latest"
    assert captured["contents"].startswith("FROM ")
    assert os.path.abspath(str(gguf)) in captured["contents"]


def test_import_gguf_reports_create_failure(tmp_path, monkeypatch):
    gguf = _touch(tmp_path / "facts.gguf")

    def fake_run(ollama, argv, *, env):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "disk full"

        return _R()

    monkeypatch.setattr(setup_alias, "_run", fake_run)
    ok, message = setup_alias.import_gguf(
        "ollama", str(gguf), "sonder:latest", env={},
    )
    assert not ok and "disk full" in message


def test_discover_usb_scans_extra_roots(tmp_path):
    _touch(tmp_path / "facts.gguf")
    found = setup_alias.discover_usb_gguf([str(tmp_path)])
    assert any(f.endswith("facts.gguf") for f in found)


def test_cli_gguf_flag_imports_and_aliases(tmp_path, monkeypatch):
    gguf = _touch(tmp_path / "facts.gguf")
    calls = []

    def fake_run(ollama, argv, *, env):
        calls.append(argv[0])

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(setup_alias, "_run", fake_run)
    monkeypatch.setattr(
        setup_alias.ollama_endpoint, "client_environment", lambda env: {},
    )
    monkeypatch.setattr(setup_alias, "ollama_executable", lambda x: "ollama")
    rc = setup_alias.main(["--gguf", str(gguf)])
    assert rc == 0
    # create was called for the alias import (and possibly embedding pull).
    assert "create" in calls
