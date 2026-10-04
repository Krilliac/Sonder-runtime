"""The reusable batch seam preserves outcomes when a provider or host fails."""
from concurrent.futures import Future

import pytest

from sonder_runtime.application import model_batching
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelBatchOutcome, ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import InternalFailure


def test_batch_outcome_requires_exactly_one_domain_result():
    response = ModelResponse("ok", "test/model", "code")
    error = InternalFailure("test failure")
    assert ModelBatchOutcome(response=response).error is None
    assert ModelBatchOutcome(error=error).response is None
    for fields in ({}, {"response": response, "error": error}):
        with pytest.raises(ValueError):
            ModelBatchOutcome(**fields)
    with pytest.raises(TypeError):
        ModelBatchOutcome(error=RuntimeError("non-domain failure"))


def test_unexpected_provider_exception_is_content_free_and_preserves_siblings():
    class Provider:
        def generate(self, request, _context):
            if request.prompt == "bad":
                raise RuntimeError("private provider exception text")
            return ModelResponse(request.prompt, "test/model", "code")

    results = model_batching.generate_batch(
        Provider(), [ModelRequest("good", "code"), ModelRequest("bad", "code")],
        local_owner_context(correlation_id="batch"),
    )
    assert results[0].response.text == "good"
    assert isinstance(results[1].error, InternalFailure)
    assert "private provider exception text" not in str(results[1].error)


def test_owned_executor_admission_failure_preserves_completed_result(monkeypatch):
    class HostPool:
        calls = 0

        def __init__(self, **kwargs):
            assert kwargs["max_workers"] == 2

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, function, *args):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("host worker capacity unavailable")
            future = Future()
            future.set_result(function(*args))
            return future

    class Provider:
        def generate(self, request, _context):
            return ModelResponse(request.prompt, "test/model", "code")

    monkeypatch.setattr(model_batching, "owned_runtime_pool", HostPool)
    results = model_batching.generate_batch(
        Provider(), [ModelRequest(str(index), "code") for index in range(3)],
        local_owner_context(correlation_id="batch"),
    )
    assert len(results) == 3
    assert results[0].response.text == "0"
    assert all(isinstance(item.error, InternalFailure) for item in results[1:])
