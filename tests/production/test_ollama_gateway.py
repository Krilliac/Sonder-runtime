"""SPEC-3 Phase 3: OllamaGateway port contract, consent, error mapping."""
from __future__ import annotations

import pytest

import model_transport
import server
from sonder_runtime.adapters.ollama.gateway import OllamaGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
)

pytestmark = pytest.mark.unit


def _context(**kwargs):
    return local_owner_context(correlation_id="req_gw", **kwargs)


def _fake_target(monkeypatch, *, model="sonder:latest", cloud=False,
                 tier_label="code"):
    monkeypatch.setattr(
        server, "_serve_target", lambda tier, strict: (model, cloud, True, tier_label),
    )


def _fake_gen(monkeypatch, result="generated text", error=None,
              usage=None):
    def make_generate(model, system, temperature, num_predict, num_ctx,
                      cloud=False, timeout=None, cancel_check=None, **kwargs):
        def gen(prompt, history=None):
            if error is not None:
                raise error
            return result
        gen.last_usage = usage or {"prompt_eval_count": 10, "eval_count": 5}
        return gen
    monkeypatch.setattr(server, "_make_generate", make_generate)


def test_generate_returns_domain_response(monkeypatch):
    _fake_target(monkeypatch)
    _fake_gen(monkeypatch)
    response = OllamaGateway().generate(
        ModelRequest(prompt="hello", tier="code"), _context(),
    )
    assert response.text == "generated text"
    assert response.tier == "code"
    assert response.tokens_in == 10 and response.tokens_out == 5


def test_cloud_tier_requires_context_consent(monkeypatch):
    _fake_target(monkeypatch, cloud=True, tier_label="cloud")
    _fake_gen(monkeypatch)
    with pytest.raises(Forbidden, match="cloud"):
        OllamaGateway().generate(
            ModelRequest(prompt="hello", tier="cloud"), _context(),
        )
    # With explicit consent in the operation context the call proceeds.
    response = OllamaGateway().generate(
        ModelRequest(prompt="hello", tier="cloud"),
        _context(cloud_allowed=True),
    )
    assert response.text == "generated text"


def test_empty_prompt_rejected(monkeypatch):
    _fake_target(monkeypatch)
    with pytest.raises(InvalidInput):
        OllamaGateway().generate(ModelRequest(prompt="  ", tier="code"), _context())


def test_unknown_tier_maps_to_invalid_input(monkeypatch):
    monkeypatch.setattr(
        server, "_serve_target", lambda tier, strict: (None, False, True, None),
    )
    with pytest.raises(InvalidInput, match="tier"):
        OllamaGateway().generate(ModelRequest(prompt="x", tier="nope"), _context())


def test_missing_alias_maps_to_dependency_unavailable(monkeypatch):
    monkeypatch.setattr(
        server, "_serve_target", lambda tier, strict: (None, False, True, "code"),
    )
    with pytest.raises(DependencyUnavailable, match="alias"):
        OllamaGateway().generate(ModelRequest(prompt="x", tier="code"), _context())


@pytest.mark.parametrize("kind,expected", [
    ("timeout", DeadlineExceeded),
    ("cancelled", Cancelled),
    ("request", DependencyUnavailable),
    ("empty_response", DependencyUnavailable),
    ("unknown", InternalFailure),
])
def test_model_errors_map_into_the_taxonomy(monkeypatch, kind, expected):
    _fake_target(monkeypatch)
    _fake_gen(
        monkeypatch,
        error=model_transport.ModelCallError(kind, "detail for %s" % kind),
    )
    with pytest.raises(expected):
        OllamaGateway().generate(ModelRequest(prompt="x", tier="code"), _context())


def test_no_driver_exception_escapes(monkeypatch):
    # The port contract: callers never see ModelCallError itself.
    _fake_target(monkeypatch)
    _fake_gen(
        monkeypatch,
        error=model_transport.ModelCallError("timeout", "slow", transient=True),
    )
    try:
        OllamaGateway().generate(ModelRequest(prompt="x", tier="code"), _context())
    except model_transport.ModelCallError:
        pytest.fail("ModelCallError escaped the port boundary")
    except DeadlineExceeded:
        pass


def test_expired_context_stops_before_model_call(monkeypatch):
    _fake_target(monkeypatch)
    _fake_gen(monkeypatch)

    with pytest.raises(DeadlineExceeded, match="before model call"):
        OllamaGateway().generate(
            ModelRequest(prompt="x", tier="code"),
            _context(timeout_seconds=0.0),
        )


def test_remote_ollama_requires_context_consent(monkeypatch):
    _fake_target(monkeypatch)
    _fake_gen(monkeypatch)
    monkeypatch.setattr(server, "BASE", "http://192.0.2.10:11434")

    with pytest.raises(Forbidden, match="remote Ollama"):
        OllamaGateway().generate(ModelRequest(prompt="x", tier="code"), _context())

    response = OllamaGateway().generate(
        ModelRequest(prompt="x", tier="code"),
        _context(remote_ollama_allowed=True),
    )
    assert response.text == "generated text"


def test_embed_maps_empty_vector_to_dependency_error(monkeypatch):
    import embeddings

    monkeypatch.setattr(embeddings, "embed", lambda text: None)
    with pytest.raises(DependencyUnavailable):
        OllamaGateway().embed(["hello"], _context())


def test_embed_returns_typed_embeddings(monkeypatch):
    import embeddings

    monkeypatch.setattr(embeddings, "embed", lambda text: [0.1, 0.2, 0.3])
    out = OllamaGateway().embed(["a", "b"], _context())
    assert len(out) == 2
    assert out[0].vector == (0.1, 0.2, 0.3)


def test_embed_honours_expired_context(monkeypatch):
    import embeddings

    called = []
    monkeypatch.setattr(embeddings, "embed", lambda text: called.append(text) or [0.1])
    with pytest.raises(DeadlineExceeded):
        OllamaGateway().embed(["never sent"], _context(timeout_seconds=0.0))
    assert called == []


def test_bootstrap_uses_the_gateway():
    from sonder_runtime.bootstrap import app as bootstrap_app

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()
    assert isinstance(application.model_gateway, OllamaGateway)
    bootstrap_app.reset_for_tests()
