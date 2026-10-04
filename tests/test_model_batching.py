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


@pytest.mark.parametrize("phase", ["construct", "enter"])
def test_initial_owned_executor_failure_refuses_all_requests_without_leaking(monkeypatch, phase):
    from sonder_runtime.platform.runtime_threads import ThreadOwnershipRefused

    class HostPool:
        def __init__(self, **_kwargs):
            if phase == "construct":
                raise ThreadOwnershipRefused("private capacity details")

        def __enter__(self):
            raise ThreadOwnershipRefused("private owner details")

        def __exit__(self, *_args):
            return False

    class Provider:
        def generate(self, _request, _context):
            pytest.fail("refused pool must not send requests")

    monkeypatch.setattr(model_batching, "owned_runtime_pool", HostPool)
    results = model_batching.generate_batch(
        Provider(), [ModelRequest("one", "code"), ModelRequest("two", "code")],
        local_owner_context(correlation_id="batch"),
    )
    assert len(results) == 2
    assert all(isinstance(item.error, InternalFailure) for item in results)
    assert all("private" not in str(item.error) for item in results)


def test_real_owned_runtime_capacity_refusal_is_a_domain_outcome(monkeypatch):
    from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads

    owner = OwnedRuntimeThreads(cleanup=lambda: True, max_pools=1, max_threads=2)

    class Provider:
        def generate(self, _request, _context):
            pytest.fail("capacity refusal must not send requests")

    with owner.pool(max_workers=1):
        monkeypatch.setattr(model_batching, "owned_runtime_pool", owner.pool)
        results = model_batching.generate_batch(
            Provider(), [ModelRequest("one", "code"), ModelRequest("two", "code")],
            local_owner_context(correlation_id="batch"), max_workers=1,
        )
    assert len(results) == 2
    assert all(isinstance(item.error, InternalFailure) for item in results)
