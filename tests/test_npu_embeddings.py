"""Typed embedding provenance and acceleration gating: legacy embed() stays
compatible, spaces never mix, and fallback is explicit, never silent."""
import json

import pytest

import embeddings as e
import npu_service


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def legacy_http(monkeypatch):
    """Route legacy embedding calls to a deterministic fake Ollama."""
    sent = []

    def fake_open_url(request, timeout=30, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        sent.append(url)
        if url.endswith("/api/tags"):
            return FakeResponse(b'{"models": []}')
        return FakeResponse(b'{"embedding": [0.25, 0.5, 0.25]}')

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open_url)
    monkeypatch.setattr(e, "current_revision", lambda **k: "rev-current")
    monkeypatch.setattr(e, "refresh_runtime_revision", lambda **k: "rev-current")
    return sent


@pytest.fixture(autouse=True)
def _reset_npu(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "SONDER_NPU_MANIFEST_DIR", str(tmp_path / "npu-manifests"),
    )
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"),
    )
    monkeypatch.setattr(e, "expected_dimension", lambda model=None: 3)
    npu_service.reset_for_tests()


def test_embed_off_policy_uses_legacy_path_with_ollama_provenance(legacy_http):
    vector = e.embed("hello world")
    assert vector == pytest.approx([0.25, 0.5, 0.25])
    meta = e.provenance(vector)
    assert meta["model"] == e.EMBED_IDENTITY
    assert meta["provider"] == "ollama"
    assert meta["accelerated"] is False
    assert any(url.endswith("/api/embeddings") for url in legacy_http)


def test_embed_prefer_uses_accelerator_and_binds_provenance(
    legacy_http, monkeypatch,
):
    calls = []

    def fake_accel(text, model_identity, revision, expected_dimension=None):
        calls.append((text, model_identity, revision, expected_dimension))
        return {
            "vector": [0.6, 0.8, 0.0],
            "provider": "openvino",
            "ep": "OpenVINOExecutionProvider",
            "model": model_identity,
            "revision": revision,
            "dimension": expected_dimension,
            "accelerated": True,
            "simulated": False,
            "manifest_hash": "f" * 64,
        }

    monkeypatch.setattr(npu_service, "embed_for_space", fake_accel)
    vector = e.embed("accelerate me")
    assert vector == pytest.approx([0.6, 0.8, 0.0])
    meta = e.provenance(vector)
    assert meta["model"] == e.EMBED_IDENTITY
    assert meta["revision"] == "rev-current"
    assert meta["provider"] == "npu:openvino"
    assert meta["accelerated"] is True
    assert calls[0][1] == e.EMBED_IDENTITY
    assert calls[0][2] == "rev-current"
    # The accelerator satisfied the request: no legacy embedding HTTP call.
    assert not any(url.endswith("/api/embeddings") for url in legacy_http)


def test_embed_cpu_reference_is_truthful_and_not_npu_accelerated(
    legacy_http, monkeypatch,
):
    monkeypatch.setattr(
        npu_service,
        "embed_for_space",
        lambda *args, **kwargs: {
            "vector": [0.6, 0.8, 0.0],
            "provider": "cpu",
            "ep": "CPUExecutionProvider",
            "model": args[1],
            "revision": args[2],
            "dimension": kwargs["expected_dimension"],
            "accelerated": False,
            "ep_fallback": True,
            "simulated": False,
            "manifest_hash": "f" * 64,
        },
    )
    vector = e.embed("reference me")
    meta = e.provenance(vector)
    assert meta["provider"] == "cpu-reference"
    assert meta["accelerated"] is False
    assert meta["simulated"] is False
    assert not any(url.endswith("/api/embeddings") for url in legacy_http)


def test_embed_rechecks_live_revision_after_accelerator_inference(
        legacy_http, monkeypatch):
    revisions = iter(["rev-before", "rev-after"])
    monkeypatch.setattr(e, "refresh_runtime_revision", lambda **_k: next(revisions))
    monkeypatch.setattr(
        npu_service,
        "embed_for_space",
        lambda _text, model, revision, expected_dimension=None: {
            "vector": [0.6, 0.8, 0.0],
            "provider": "openvino",
            "model": model,
            "revision": revision,
            "dimension": expected_dimension,
            "accelerated": True,
            "simulated": False,
        },
    )
    assert e.embed("retag during inference") is None
    assert not any(url.endswith("/api/embeddings") for url in legacy_http)


@pytest.mark.parametrize(
    "patch",
    [
        {"accelerated": None},
        {"vector": [1.0, 0.0], "dimension": 2},
        {"provider": "cpu-sim", "accelerated": False, "simulated": True},
        {"provider": "vitisai", "accelerated": False},
        {"model": "other:latest"},
        {"revision": "other-revision"},
    ],
)
def test_embed_rejects_untrusted_accelerator_provenance(
    legacy_http, monkeypatch, patch,
):
    def fake(text, model, revision, expected_dimension=None):
        result = {
            "vector": [0.6, 0.8, 0.0],
            "provider": "openvino",
            "model": model,
            "revision": revision,
            "dimension": expected_dimension,
            "accelerated": True,
            "simulated": False,
        }
        result.update(patch)
        return result

    monkeypatch.setattr(npu_service, "embed_for_space", fake)
    vector = e.embed("fail closed")
    assert vector == pytest.approx([0.25, 0.5, 0.25])
    assert e.provenance(vector)["provider"] == "ollama"
    assert any(url.endswith("/api/embeddings") for url in legacy_http)


def test_embed_prefer_miss_falls_back_to_legacy_and_is_explicit(
    legacy_http, monkeypatch,
):
    handled = []
    monkeypatch.setattr(npu_service, "embed_for_space", lambda *a: None)
    monkeypatch.setattr(npu_service, "embeddings_active", lambda: "prefer")
    monkeypatch.setattr(
        npu_service, "record_fallback_handler",
        lambda capability, handler, ok: handled.append(
            (capability, handler, ok)
        ),
    )
    result = e.embed_result("fall back please")
    assert result is not None
    assert result["vector"] == pytest.approx([0.25, 0.5, 0.25])
    assert result["provider"] == "ollama"
    assert result["accelerated"] is False
    assert result["fallback_reason"] == "npu_unavailable"
    assert handled == [("embeddings", "ollama", True)]


def test_embed_result_off_policy_has_no_fallback_reason(legacy_http):
    result = e.embed_result("plain")
    assert result["provider"] == "ollama"
    assert result["fallback_reason"] == ""
    assert result["dimension"] == 3
    assert result["revision"] == "rev-current"


def test_embed_accelerator_exception_is_contained(legacy_http, monkeypatch):
    def boom(*_args):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(npu_service, "embed_for_space", boom)
    vector = e.embed("still works")
    assert vector == pytest.approx([0.25, 0.5, 0.25])
    assert e.provenance(vector)["provider"] == "ollama"


def test_embed_shadow_exercises_accelerator_without_substitution(
    legacy_http, monkeypatch,
):
    shadows = []
    monkeypatch.setattr(npu_service, "embed_for_space", lambda *a: None)
    monkeypatch.setattr(
        npu_service, "embed_shadow",
        lambda text, model, revision: shadows.append((text, model, revision)),
    )
    vector = e.embed("shadow me")
    assert vector == pytest.approx([0.25, 0.5, 0.25])
    assert e.provenance(vector)["provider"] == "ollama"
    assert shadows == [("shadow me", e.EMBED_IDENTITY, "rev-current")]


def test_embed_total_failure_still_soft_fails_to_none(monkeypatch):
    handled = []
    def boom(*a, **k):
        raise OSError("no ollama")

    monkeypatch.setattr(e.ollama_endpoint, "open_url", boom)
    monkeypatch.setattr(npu_service, "embed_for_space", lambda *a: None)
    monkeypatch.setattr(npu_service, "embeddings_active", lambda: "prefer")
    monkeypatch.setattr(
        npu_service, "record_fallback_handler",
        lambda capability, handler, ok: handled.append(
            (capability, handler, ok)
        ),
    )
    assert e.embed("anything") is None
    assert e.embed_result("anything") is None
    assert handled == [
        ("embeddings", "ollama", False),
        ("embeddings", "ollama", False),
    ]


def test_space_gating_end_to_end_prevents_mixing(legacy_http, monkeypatch):
    """A manifest pinned to a different revision must never serve vectors."""
    import runtime_policy
    from tests.npu_helpers import embedding_payload

    manifest_dir = npu_service.npu_manifest.manifest_dir()
    manifest_dir.mkdir(parents=True, exist_ok=True)
    payload = embedding_payload(
        manifest_dir,
        space={"model": e.EMBED_IDENTITY, "revision": "rev-other"},
    )
    (manifest_dir / "embed-tiny-v1.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    runtime_policy.update(npu={"mode": "prefer"})

    class NeverBroker:
        def call(self, *_args, **_kwargs):
            raise AssertionError("space mismatch must not reach the broker")

    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: NeverBroker(),
    )
    vector = e.embed("do not mix spaces")
    assert vector == pytest.approx([0.25, 0.5, 0.25])
    assert e.provenance(vector)["provider"] == "ollama"
