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
