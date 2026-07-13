from types import SimpleNamespace

import setup_alias


def test_offline_missing_model_never_pulls(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)
    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 2
    assert calls == [["ollama-test", "show", setup_alias.DEFAULT_BASE_MODEL]]


def test_online_pulls_only_missing_models_and_creates_alias(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["show", "base:model"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)
    assert setup_alias.main(
        [
            "--model",
            "base:model",
            "--embed-model",
            "embed:model",
            "--ollama",
            "ollama-test",
        ]
    ) == 0
    verbs = [command[1] for command in calls]
    assert verbs == ["show", "pull", "show", "create"]
    assert calls[-1][2] == setup_alias.STABLE_ALIAS == "sonder:latest"
    assert not any(command[1:3] == ["pull", "embed:model"] for command in calls)


def test_failed_alias_creation_is_reported(monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=1 if command[1] == "create" else 0,
            stdout="",
            stderr="create failed",
        )

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)
    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 3


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


def test_remote_ollama_opt_in_allows_cli(monkeypatch):
    calls = []
    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)

    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 0
    assert calls


def test_bind_all_ollama_cli_uses_numeric_loopback(monkeypatch):
    environments = []
    monkeypatch.setenv("OLLAMA_HOST", "[::]:11434")

    def fake_run(command, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_alias.subprocess, "run", fake_run)

    assert setup_alias.main(["--offline", "--ollama", "ollama-test"]) == 0
    assert environments
    assert all(env["OLLAMA_HOST"] == "http://[::1]:11434" for env in environments)
