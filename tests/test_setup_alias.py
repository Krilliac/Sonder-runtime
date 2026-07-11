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
    assert "Use tools that the host lists" in content
    assert "Never invent tools" in content
    assert "FROM base:model" in content
