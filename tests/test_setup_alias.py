from types import SimpleNamespace

import setup_alias


def test_offline_missing_model_never_pulls(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_alias, "default_base_model", lambda: "sized:model")

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)
    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 2
    assert calls == []  # local `ollama show`/`pull` retired under RET-05


def test_default_base_model_uses_live_ram_selection(monkeypatch):
    monkeypatch.delenv("SONDER_BASE_MODEL", raising=False)
    monkeypatch.setattr(
        setup_alias.bootstrap_engine, "choose_model", lambda: "ram-sized:model",
    )
    assert setup_alias.default_base_model() == "ram-sized:model"

    monkeypatch.setenv("SONDER_BASE_MODEL", "operator:model")
    assert setup_alias.default_base_model() == "operator:model"


def test_online_pulls_only_missing_models_and_creates_alias(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))
    rc = setup_alias.main(["--model", "base:model", "--embed-model", "embed:model", "--ollama", "ollama-test"])
    assert rc == 2
    assert calls == []
    assert "retired" in capsys.readouterr().out.lower()


def test_optional_embedding_failure_does_not_block_base_alias(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))
    rc = setup_alias.main(["--model", "base:model", "--embed-model", "embed:model", "--ollama", "ollama-test"])
    assert rc == 2 and calls == [] and "retired" in capsys.readouterr().out.lower()


def test_optional_embedding_failure_does_not_claim_chat_ready_before_alias(monkeypatch, capsys):
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert setup_alias.main(["--model", "base:model", "--embed-model", "embed:model", "--ollama", "ollama-test"]) == 2
    assert "core chat is ready" not in capsys.readouterr().out


def test_offline_missing_optional_embedding_still_creates_base_alias(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))
    assert setup_alias.main(["--offline", "--model", "base:model", "--embed-model", "embed:model", "--ollama", "ollama-test"]) == 2
    assert calls == []


def test_no_embedding_skips_optional_model_without_discarding_base_alias(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))
    assert setup_alias.main(["--model", "base:model", "--embed-model", "embed:model", "--no-embedding", "--ollama", "ollama-test"]) == 2
    assert calls == []


def test_failed_alias_creation_is_reported(monkeypatch, capsys):
    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 2
    assert "retired" in capsys.readouterr().out.lower()


def test_system_prompt_uses_exposed_tools_without_inventing_them():
    content = setup_alias.model_file("base:model")
    assert "inside Sonder Runtime" in content
    assert "not a foundation model" in content
    assert "Use tools that the host lists" in content
    assert "Never invent tools" in content
    assert "FROM base:model" in content


def test_remote_ollama_is_blocked_before_cli_without_explicit_opt_in(monkeypatch):
    calls = []
    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.setattr(
        setup_alias.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 4
    assert calls == []


def test_remote_ollama_opt_in_allows_cli(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "https://models.example.test:11437")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))

    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 2
    assert not any(c and "ollama" in str(c[0]).lower() for c in calls)
    assert "retired" in capsys.readouterr().out.lower()


def test_bind_all_ollama_cli_uses_numeric_loopback(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://[::]:11437")
    monkeypatch.setattr(setup_alias.subprocess, "run", lambda command, **kw: calls.append(command))

    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 2
    assert not any(c and "ollama" in str(c[0]).lower() for c in calls)
    assert "retired" in capsys.readouterr().out.lower()
