from sonder_runtime.adapters.execution_tools import CodeRunnerProvider, GroundingProvider


def test_execution_providers_resolve_root_helpers_dynamically(monkeypatch):
    import code_runner
    import grounding

    code_marker = object()
    ground_marker = object()
    monkeypatch.setattr(code_runner, "run_code", lambda *args, **kwargs: code_marker)
    monkeypatch.setattr(grounding, "extract_code_block", lambda value: ground_marker)
    assert CodeRunnerProvider().run_code("x") is code_marker
    assert GroundingProvider().extract_code_block("x") is ground_marker


def test_server_binds_the_dynamic_code_runner_provider_and_keeps_patch_surface(monkeypatch):
    import server
    from sonder_runtime.adapters.execution_tools import code_runner

    marker = object()
    monkeypatch.setattr(code_runner, "run_code", lambda *args, **kwargs: marker)

    assert server.code_runner is code_runner
    assert server.code_runner.run_code("x") is marker


def test_server_binds_the_dynamic_grounding_provider_and_keeps_patch_surface(monkeypatch):
    import server
    from sonder_runtime.adapters.execution_tools import grounding

    marker = object()
    monkeypatch.setattr(grounding, "extract_code_block", lambda *args, **kwargs: marker)

    assert server.grounding is grounding
    assert server.grounding.extract_code_block("x") is marker
