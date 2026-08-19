import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import engine_bundle
from scripts import assemble_engine_bundle as assembler


def _model(store: Path, name: str, seed: bytes) -> None:
    manifest_rel = assembler._model_manifest_relative(name)
    config = b"config-" + seed
    layer = b"GGUF-" + seed * 2
    config_hash = hashlib.sha256(config).hexdigest()
    layer_hash = hashlib.sha256(layer).hexdigest()
    blobs = store / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / f"sha256-{config_hash}").write_bytes(config)
    (blobs / f"sha256-{layer_hash}").write_bytes(layer)
    manifest = {
        "schemaVersion": 2,
        "config": {
            "digest": f"sha256:{config_hash}",
            "size": len(config),
        },
        "layers": [
            {
                "digest": f"sha256:{layer_hash}",
                "size": len(layer),
            }
        ],
    }
    target = store / manifest_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")


def _inputs(tmp_path: Path):
    python_runtime = tmp_path / "python-runtime"
    python_runtime.mkdir()
    python_name = "python.exe" if os.name == "nt" else "python3"
    (python_runtime / python_name).write_bytes(b"portable python")
    cache = python_runtime / "Lib" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "leaky.cpython-312.pyc").write_bytes(str(Path.home()).encode("utf-8"))
    ollama_runtime = tmp_path / "ollama-runtime"
    ollama_runtime.mkdir()
    ollama_name = "ollama.exe" if os.name == "nt" else "ollama"
    (ollama_runtime / ollama_name).write_bytes(b"portable ollama")
    model_store = tmp_path / "models"
    model_store.mkdir()
    _model(model_store, "qwen2.5-coder:1.5b", b"base")
    _model(model_store, "nomic-embed-text:latest", b"embed")
    return python_runtime, ollama_runtime, model_store


def test_assembles_and_revalidates_platform_bundle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # The assembler stamps the runtime contract it sealed for into the
    # manifest, so a repo it assembles from has to have one -- as every
    # real checkout does.
    (repo / "requirements-runtime.txt").write_text(
        "mcp==2.0.0\ncryptography==50.0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    bundle = assembler.assemble_bundle(
        output,
        python_runtime=python_runtime,
        ollama_runtime=ollama_runtime,
        model_store=model_store,
        base_models=[("qwen2.5-coder:1.5b", 0)],
        embedding_model="nomic-embed-text:latest",
        validate_runtime=False,
    )
    assert bundle.root == output.absolute()
    assert bundle.python_executable.is_file()
    assert bundle.ollama_executable.is_file()
    assert bundle.base_models[0].name == "qwen2.5-coder:1.5b"
    assert not list(output.rglob("*.pyc"))
    assert engine_bundle.load_engine_bundle(output).manifest_sha256 == bundle.manifest_sha256


def test_supplied_runtime_validation_requires_mcpserver_api(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # The assembler stamps the runtime contract it sealed for into the
    # manifest, so a repo it assembles from has to have one -- as every
    # real checkout does.
    (repo / "requirements-runtime.txt").write_text(
        "mcp==2.0.0\ncryptography==50.0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="No module named 'mcp.server.mcpserver'",
        )

    monkeypatch.setattr(assembler.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="MCPServer compatibility API"):
        assembler.assemble_bundle(
            output,
            python_runtime=python_runtime,
            ollama_runtime=ollama_runtime,
            model_store=model_store,
            base_models=[("qwen2.5-coder:1.5b", 0)],
            embedding_model="nomic-embed-text:latest",
        )

    assert calls and calls[0][1:] == [
        "-I",
        "-c",
        assembler._contract_probe_source(assembler._runtime_contract_pins()),
    ]
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.stage-*"))


def test_supplied_runtime_validation_accepts_mcpserver_api(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # The assembler stamps the runtime contract it sealed for into the
    # manifest, so a repo it assembles from has to have one -- as every
    # real checkout does.
    (repo / "requirements-runtime.txt").write_text(
        "mcp==2.0.0\ncryptography==50.0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="MCPServer\n", stderr="")

    monkeypatch.setattr(assembler.subprocess, "run", fake_run)

    bundle = assembler.assemble_bundle(
        output,
        python_runtime=python_runtime,
        ollama_runtime=ollama_runtime,
        model_store=model_store,
        base_models=[("qwen2.5-coder:1.5b", 0)],
        embedding_model="nomic-embed-text:latest",
    )

    assert bundle.root == output.absolute()
    assert calls and calls[0][1:] == [
        "-I",
        "-c",
        assembler._contract_probe_source(assembler._runtime_contract_pins()),
    ]


def test_missing_model_fails_before_replacing_existing_output(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # The assembler stamps the runtime contract it sealed for into the
    # manifest, so a repo it assembles from has to have one -- as every
    # real checkout does.
    (repo / "requirements-runtime.txt").write_text(
        "mcp==2.0.0\ncryptography==50.0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    output.mkdir(parents=True)
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not installed"):
        assembler.assemble_bundle(
            output,
            python_runtime=python_runtime,
            ollama_runtime=ollama_runtime,
            model_store=model_store,
            base_models=[("missing:model", 0)],
            embedding_model="nomic-embed-text:latest",
            validate_runtime=False,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_output_is_restricted_to_exact_staging_roots(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # The assembler stamps the runtime contract it sealed for into the
    # manifest, so a repo it assembles from has to have one -- as every
    # real checkout does.
    (repo / "requirements-runtime.txt").write_text(
        "mcp==2.0.0\ncryptography==50.0.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(assembler, "ROOT", repo)
    with pytest.raises(ValueError, match="must be exactly"):
        assembler.validate_output(repo / "dist" / "engine-bundles" / "wrong-platform")


def test_model_name_maps_to_ollama_manifest_layout():
    assert assembler._model_manifest_relative("qwen2.5-coder:1.5b").as_posix() == (
        "manifests/registry.ollama.ai/library/qwen2.5-coder/1.5b"
    )
    assert assembler._model_manifest_relative("team/custom:latest").as_posix() == (
        "manifests/registry.ollama.ai/team/custom/latest"
    )


def test_the_sealed_closure_reaches_cryptography_through_pyjwt_crypto():
    """The closure must carry extras, not drop them at the first hop.

    `mcp` reaches `cryptography` only via `Requires-Dist: pyjwt[crypto]`, and
    PyJWT gates that edge behind `extra == "crypto"`. A walk that pushes the
    requirement's name without its extras re-enters PyJWT with no extra
    selected, the marker evaluates false, and `cryptography` never joins the
    closure -- while `mcp.server.mcpserver` imports it eagerly at module scope
    (`mcp/server/request_state.py`). The sealed runtime then fails its own
    import probe and NO Windows engine bundle can be assembled at all.

    mcp 1.29.0 imported no cryptography, so this only became reachable with the
    2.x migration, and nothing else in this file executes the closure.
    """
    names = {
        distribution.metadata["Name"].casefold().replace("_", "-")
        for distribution in assembler._distribution_closure("mcp")
    }
    assert "pyjwt" in names, "the closure no longer reaches PyJWT; this test is stale"
    assert "cryptography" in names, sorted(names)


def test_the_runtime_contract_seeds_the_sealed_closure():
    """Distributions Sonder imports directly must not depend on MCP's graph.

    `cryptography` is pinned in `requirements-runtime.txt` for
    `fanout_prompt_vault.py`, which `server.py` imports. Deriving the sealed set
    from `mcp` alone means the bundle keeps it only for as long as MCP happens
    to pull it in.
    """
    roots = assembler._runtime_contract_names()
    assert "mcp" in roots
    assert "cryptography" in roots

    names = {
        distribution.metadata["Name"].casefold().replace("_", "-")
        for distribution in assembler._distribution_closure("mcp", *roots)
    }
    assert {"mcp", "cryptography"} <= names


def test_the_sealed_closure_satisfies_the_import_probe(tmp_path):
    """End to end: the copied packages must actually satisfy the probe.

    `tests/test_assemble_engine_bundle.py` otherwise never executes
    `_distribution_closure`, `_copy_distribution`, or the probe -- every other
    path here supplies a runtime with `validate_runtime=False` or a
    monkeypatched `subprocess.run`. So "the bundle tests pass" has never meant
    "a bundle can be built". This closes that: copy the closure the assembler
    would seal, then run the assembler's own probe source against exactly that
    directory and nothing else.
    """
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    roots = assembler._runtime_contract_names()
    for distribution in assembler._distribution_closure("mcp", *roots):
        assembler._copy_distribution(distribution, site_packages)

    # `-E -s` drops PYTHONPATH and the user site; the prelude then drops every
    # other site-packages, so only the sealed copy can satisfy the imports --
    # the same isolation `_probe_mcpserver` gets from `-I`.
    #
    # `site.addsitedir`, not a raw `sys.path` insert: a sealed runtime's
    # `Lib/site-packages` is a real site directory, so `site` processes the
    # `.pth` files in it at startup. pywin32 ships `pywin32.pth`, and mcp 2.x
    # imports `pywintypes` unguarded (`mcp/os/win32/utilities.py`) where 1.x
    # wrapped it in try/except -- so the 2.x import path now depends on that
    # `.pth` being honoured. A raw path insert skips `.pth` processing and
    # would fail here for a reason the real sealed runtime does not have.
    prelude = (
        "import sys, site\n"
        "sys.path = [p for p in sys.path if 'site-packages' not in p]\n"
        "site.addsitedir(r%r)\n"
        % (str(site_packages),)
    )
    probe = subprocess.run(
        [sys.executable, "-E", "-s", "-c", prelude + assembler._MCPSERVER_IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert probe.returncode == 0, (
        "the sealed package set cannot import the MCP server API:\n"
        + (probe.stderr or probe.stdout)
    )
    assert "MCPServer" in probe.stdout
