from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_public_guidance_does_not_force_one_gpu_shape():
    readme = _read("README.md")
    import_prompt = _read("integrations/IMPORT_PROMPT.md")
    mcp_config = _read("integrations/claude/mcp-config.md")
    codex_example = _read("integrations/codex/config.toml.example")

    combined = "\n".join((readme, import_prompt, mcp_config, codex_example))
    assert "Defaults to `999`" not in combined
    assert "Set it to `999`" not in combined
    assert "Set SONDER_NUM_GPU=999" not in combined
    assert 'SONDER_NUM_GPU = "999"' not in combined
    assert "6 GB card" not in combined
    assert "capability detection" in combined


def test_runtime_help_does_not_advertise_one_developer_machine():
    server = _read("server.py")

    assert "6 GB 4050" not in server
    assert '"project":"D:\\\\repo"' not in server
    assert "[local GPU]" not in server
    assert "In VRAM now:" not in server


def test_public_agent_guidance_supports_cpu_only_hosts():
    for path in (
        "integrations/claude/CLAUDE.sonder.md",
        "integrations/codex/AGENTS.sonder.md",
    ):
        text = _read(path)
        assert "CPU-only is supported" in text
        assert "your own GPU" not in text


def test_getting_started_names_native_state_paths_and_shells():
    getting_started = _read("docs/wiki/02-getting-started.md")
    tools = _read("docs/wiki/10-tools-and-languages.md")

    assert "%LOCALAPPDATA%\\sonder" in getting_started
    assert "~/Library/Application Support/sonder" in getting_started
    assert "$XDG_DATA_HOME/sonder" in getting_started
    assert "PowerShell on Windows" in tools
    assert "Bash/sh/zsh on POSIX hosts" in tools


def test_default_model_docs_match_runtime_policy_contract():
    tiers = _read("docs/wiki/08-model-tiers-and-gateway.md")
    example = _read("integrations/codex/config.toml.example")
    collection = _read("docs/runbooks/assemble-model-collection.md")

    assert tiers.count("| `sonder:latest` |") >= 3
    assert tiers.count("| unbound |") >= 2
    assert 'SONDER_FAST = "sonder:latest"' in example
    assert 'SONDER_CODE = "sonder:latest"' in example
    assert 'SONDER_GENERAL = "sonder:latest"' in example
    assert "unbound by default" in collection
