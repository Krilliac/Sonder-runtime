"""Ownership and compatibility checks for the packaged code runner."""

from pathlib import Path


def test_code_runner_implementation_is_packaged_adapter():
    import code_runner
    from sonder_runtime.adapters.execution_tools import code_runner as packaged

    assert Path(packaged.__file__).parts[-2:] == (
        "execution_tools", "code_runner.py"
    )
    assert code_runner is packaged


def test_execution_tools_provider_targets_packaged_implementation(monkeypatch):
    from sonder_runtime.adapters.execution_tools import CodeRunnerProvider
    from sonder_runtime.adapters.execution_tools import code_runner

    marker = object()
    monkeypatch.setattr(code_runner, "run_code", lambda *_args, **_kwargs: marker)
    assert CodeRunnerProvider().run_code("print(1)") is marker


def test_root_alias_keeps_private_runner_patch_surface():
    import code_runner
    from sonder_runtime.adapters.execution_tools import code_runner as packaged

    assert code_runner is packaged
    assert hasattr(code_runner, "_run_process")
