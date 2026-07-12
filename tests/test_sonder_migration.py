import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _tracked_paths():
    output = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached"]
    )
    return tuple(raw for raw in output.split(b"\0") if raw)


def _working_tree_bytes(relative: bytes) -> bytes:
    path = ROOT / Path(os.fsdecode(relative))
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    assert path.is_file(), "tracked path is not a readable file: %s" % os.fsdecode(
        relative
    )
    return path.read_bytes()


def _deprecated_identifiers():
    former_name = b"tri" + b"lobite"
    titled_name = former_name[:1].upper() + former_name[1:]
    return {
        "former product name": former_name,
        "deprecated environment prefix": b"LOCAL" + b"_LLM_",
        "former REST namespace": b"/v1/" + former_name,
        "former HTTP header namespace": b"X-" + titled_name + b"-",
        "former repository slug": b"Krilliac/" + titled_name + b"-model",
        "former MCP tool namespace": b"mcp__local" + b"-llm__",
        "former MCP server path": b"mcp-servers/local" + b"-llm",
    }


def test_tracked_tree_has_no_deprecated_migration_identifiers():
    tracked = _tracked_paths()
    assert tracked, "git returned no tracked files"

    matches = []
    forbidden = _deprecated_identifiers()
    for relative in tracked:
        lowered_path = relative.lower()
        data = _working_tree_bytes(relative)
        lowered_data = data.lower()
        for label, needle in forbidden.items():
            lowered_needle = needle.lower()
            if lowered_needle in lowered_path:
                matches.append("%s in path %s" % (label, os.fsdecode(relative)))
            if lowered_needle in lowered_data:
                matches.append("%s in content %s" % (label, os.fsdecode(relative)))

    assert not matches, "deprecated migration residue:\n" + "\n".join(matches)


def test_documentation_keeps_runtime_and_model_server_boundaries_explicit():
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    readme = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", readme)
    architecture = " ".join(
        (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8").split()
    )

    assert "Sonder is a runtime, not a foundation model" in readme
    assert "Ollama is the local model server" in readme
    assert "Sonder is an AI runtime and orchestration system" in architecture
    assert "It is not a foundation model" in architecture
    assert "Ollama is the local model server used by Sonder" in architecture


def test_user_facing_terminology_never_presents_runtime_as_model_weights():
    paths = (
        "deploy_sonder.sh",
        "cloud_train.sh",
        "command_registry.py",
        "endless_train.py",
        "sonder_repl.py",
        "sonder_serve.py",
        "curriculum_run.py",
        "docs/superpowers/plans/2026-07-02-sonder-memory-loop.md",
    )
    text = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8") for relative in paths
    ).lower()
    brand = "son" + "der"
    forbidden = (
        "stand up the " + brand + " model",
        "self-aware " + brand + " alias",
        "you are " + brand + ", a self-improving coding assistant",
        brand + " is a self-improving local coding model",
        "ask '" + brand + "', the local self-improving coding model",
        "fine-tune of " + brand,
        "trains " + brand + " runtime",
        "private self-improving coder",
        "the model/tool/alias is `" + brand + "`",
        "the `" + brand + "` ollama model",
        "grounded self-training",
        "training complete",
        "trained on %d tasks",
        "endless training",
        "harvested=%d trained=%d",
    )
    assert not [phrase for phrase in forbidden if phrase in text]

    deploy = (ROOT / "deploy_sonder.sh").read_text(encoding="utf-8")
    plan = (
        ROOT / "docs/superpowers/plans/2026-07-02-sonder-memory-loop.md"
    ).read_text(encoding="utf-8")
    assert "Sonder Runtime is the host orchestration software" in deploy
    assert "bypasses runtime memory/tools" in deploy
    assert "Sonder Runtime is the orchestration product" in plan
    assert "model-store entry rather than the runtime itself" in plan


def test_cloud_training_uses_supervised_controller_launch():
    script = (ROOT / "cloud_train.sh").read_text(encoding="utf-8")
    assert "python adaptive_training.py start --confirm" in script
    assert "python qlora_train.py" not in script
    assert "SONDER_TRAINING_STATE" in script
    assert 'state.get("adapter_dir")' in script


def test_checked_in_peft_cards_identify_adapter_runtime_boundary():
    cards = (
        "sonder-personal-lora/README.md",
        "sonder-personal-lora/checkpoints/checkpoint-58/README.md",
        "sonder-personal-lora/checkpoints/checkpoint-116/README.md",
    )
    for relative in cards:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "It is **not Sonder Runtime**, a standalone" in text
        assert "Qwen/Qwen2.5-Coder-1.5B-Instruct" in text
        assert "The matching base weights remain separate" in text
        assert "Sonder Runtime is the orchestration software" in text
        assert "`sonder:latest` Ollama alias remains the rollback entry" in text


def test_canonical_entrypoints_are_tracked_and_present():
    tracked = {os.fsdecode(relative) for relative in _tracked_paths()}
    entrypoints = {
        "sonder.cmd",
        "sonder-runtime.cmd",
        "sonder-runtime.sh",
        "sonder-serve.cmd",
        "sonder-serve.sh",
        "sonder-headless.cmd",
        "sonder-headless.sh",
        "sonder-launcher.cmd",
        "sonder-launcher.sh",
        "sonder_client.py",
        "sonder_headless.py",
        "sonder_health.py",
        "sonder_launcher.py",
        "sonder_repl.py",
        "sonder_serve.py",
    }

    assert entrypoints <= tracked
    for relative in entrypoints:
        assert (ROOT / relative).is_file(), "missing canonical entrypoint: %s" % relative
