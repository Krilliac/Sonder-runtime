import sonder_runtime.adapters.embeddings as e


class FakeResponse:
    """Fake response object for mocking urllib.request.urlopen."""
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_blob_roundtrip():
    v = [0.5, -1.25, 3.0]
    back = e.from_blob(e.to_blob(v))
    assert len(back) == 3
    assert abs(back[0] - 0.5) < 1e-6
    assert abs(back[1] + 1.25) < 1e-6


def test_cosine_identical_is_one():
    assert abs(e.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6


def test_cosine_orthogonal_is_zero():
    assert abs(e.cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6


def test_cosine_handles_empty():
    assert e.cosine([], [1.0]) == 0.0
    assert e.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_embedding_provenance_canonicalizes_equivalent_ollama_names():
    assert e.canonical_model_name("NOMIC-EMBED-TEXT") == "nomic-embed-text:latest"
    assert e.canonical_model_name(
        "registry.ollama.ai/library/nomic-embed-text:latest"
    ) == "nomic-embed-text:latest"
    assert e.provenance([1.0, 2.0])["dimension"] == 2
    assert e.valid_vector([1.0, 2.0]) is True
    assert e.valid_vector([float("nan")]) is False
    assert e.valid_vector([True]) is False
    assert e.valid_vector([0.0, -0.0]) is False
    assert e.endpoint_is_loopback("http://127.0.0.1:11434") is True
    assert e.endpoint_is_loopback("http://[::1]:11434") is True
    assert e.endpoint_is_loopback("http://example.com:11434") is False


def test_expected_dimension_supports_known_models_and_safe_override(monkeypatch):
    monkeypatch.delenv("SONDER_EMBED_DIM", raising=False)
    assert e.expected_dimension("nomic-embed-text") == 768
    assert e.expected_dimension("unknown-local-model") is None

    monkeypatch.setenv("SONDER_EMBED_DIM", "1024")
    assert e.expected_dimension("unknown-local-model") == 1024
    monkeypatch.setenv("SONDER_EMBED_DIM", "invalid")
    assert e.expected_dimension("nomic-embed-text") is None


def test_configure_model_changes_future_embedding_provenance_without_network(
    monkeypatch, tmp_path,
):
    manifest = (
        tmp_path / "manifests" / "registry.ollama.ai" / "library"
        / "bge-m3" / "latest"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'{"config":"sha256:bge"}')
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(
        e.ollama_endpoint, "open_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )

    original = (e.EMBED_MODEL, e.EMBED_IDENTITY, e.EMBED_REVISION, e.EXPECTED_DIMENSION)
    try:
        state = e.configure_model("BGE-M3")

        assert state["model"] == "bge-m3:latest"
        assert state["revision"] == ""  # local Ollama manifest hashing retired; SI identity owns digests
        assert state["expected_dimension"] is None
    finally:
        e.EMBED_MODEL, e.EMBED_IDENTITY, e.EMBED_REVISION, e.EXPECTED_DIMENSION = original
    monkeypatch.setenv("SONDER_EMBED_DIM", "0")
    assert e.expected_dimension("nomic-embed-text") is None


def test_local_manifest_revision_is_stable_and_changes_with_model_bytes(tmp_path):
    # Under RET-05 local Ollama manifest hashing is retired; SI /v1/sonder/identity owns digests.
    assert e.local_manifest_revision("nomic-embed-text", tmp_path) == ""


def test_local_manifest_revision_rejects_unsafe_model_path(tmp_path):
    assert e.local_manifest_revision("../outside:latest", tmp_path) == ""


def test_runtime_revision_refresh_observes_local_manifest_retag(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(e, "EMBED_MODEL", "nomic-embed-text")
    assert e.refresh_runtime_revision(models_root=tmp_path) == ""


def test_manifest_style_env_revision_does_not_hide_live_store_drift(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_EMBED_REVISION", "ollama-manifest-sha256:" + "a" * 64)
    assert e.current_revision(models_root=tmp_path) == "ollama-manifest-sha256:" + "a" * 64


def test_serving_digest_is_authoritative_over_loopback_filesystem(
    monkeypatch, tmp_path,
):
    served_digest = "b" * 64

    def fake_urlopen(_request, timeout=0.5):
        return FakeResponse(json.dumps({
            "backend_identity": {"model_digest": served_digest},
        }).encode("utf-8"))

    import json
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_urlopen)

    assert e.current_revision(base="http://127.0.0.1:11437") == (
        "si-model-sha256:" + served_digest
    )


_REMOTE_EMBED_BASE = "https://example.test:11437"
# One fenced embed: read the serving digest, request the vector, read it again.
_FENCED_EMBED = [
    _REMOTE_EMBED_BASE + "/v1/sonder/identity?model=nomic-embed-text%3Alatest",
    _REMOTE_EMBED_BASE + "/v1/embeddings",
    _REMOTE_EMBED_BASE + "/v1/sonder/identity?model=nomic-embed-text%3Alatest",
]


def _remote_embedder(monkeypatch, state):
    import json

    calls = []

    def fake_urlopen(request, timeout=30):
        url = request if isinstance(request, str) else request.full_url
        calls.append(url)
        if "/v1/sonder/identity" in url:
            return FakeResponse(json.dumps({
                "backend_identity": {"model_digest": state["digest"]},
            }).encode("utf-8"))
        state["digest"] = state.get("digest_after_embed", state["digest"])
        return FakeResponse(b'{"data": [{"index": 0, "embedding": [1.0, 0.0]}]}')

    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setattr(e, "BASE", _REMOTE_EMBED_BASE)
    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_urlopen)
    return calls


def test_embed_returns_the_vector_when_the_serving_digest_holds(monkeypatch):
    calls = _remote_embedder(monkeypatch, {"digest": "a" * 64})

    vector = e.embed("task")

    assert vector == [1.0, 0.0]
    assert e.provenance(vector)["revision"] == "si-model-sha256:" + "a" * 64
    assert calls == _FENCED_EMBED


def test_embed_fails_closed_if_serving_digest_changes_mid_request(monkeypatch):
    calls = _remote_embedder(
        monkeypatch, {"digest": "a" * 64, "digest_after_embed": "b" * 64},
    )

    assert e.embed("task") is None
    # Same requests as the passing control above: only the digest differs.
    assert calls == _FENCED_EMBED


def test_provenance_stays_bound_after_another_revision_refresh(monkeypatch):
    state = {"digest": "a" * 64}
    _remote_embedder(monkeypatch, state)
    vector = e.embed("task")
    bound = e.provenance(vector)["revision"]
    assert bound == "si-model-sha256:" + "a" * 64
    state["digest"] = "b" * 64
    e.refresh_runtime_revision()

    assert e.provenance(vector)["revision"] == bound
    assert e.EMBED_REVISION == "si-model-sha256:" + "b" * 64


def test_valid_vector_soft_rejects_values_outside_float32():
    assert e.valid_vector([10 ** 400]) is False
    assert e.valid_vector([1e308]) is False


def test_embed_soft_fails_to_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ollama")

    monkeypatch.setattr(e.ollama_endpoint, "open_url", boom)
    assert e.embed("anything") is None


def test_remote_embedding_endpoint_is_blocked_before_network(monkeypatch):
    calls = []
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.setattr(
        e.ollama_endpoint,
        "open_url",
        lambda *args, **kwargs: calls.append(1),
    )
    # HTTPS, so the missing opt-in is the only thing that can block it: plain
    # HTTP is refused for its scheme even with the opt-in set.
    remote = "https://models.example.test:11434"

    assert e.serving_model_revision(base=remote) == ""
    assert e.embed("private task", base=remote) is None
    assert calls == []


def test_explicit_bind_all_embedding_base_is_dialed_as_loopback(monkeypatch):
    requested = []

    def fake_open(request, timeout=30):
        requested.append(request.full_url if hasattr(request, "full_url") else request)
        if "/v1/sonder/identity" in requested[-1]:
            return FakeResponse(b'{}')
        return FakeResponse(b'{"data": [{"index": 0, "embedding": [0.1, 0.2]}]}')

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open)
    vector = e.embed("task", base="http://0.0.0.0:11437")

    assert vector is not None
    assert requested
    assert all(url.startswith("http://127.0.0.1:11437/") for url in requested)


def test_embed_success_path(monkeypatch):
    def mock_urlopen(*a, **k):
        return FakeResponse(b'{"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}')

    monkeypatch.setattr(e.ollama_endpoint, "open_url", mock_urlopen)
    result = e.embed("hi")
    assert result is not None
    assert len(result) == 3
    assert abs(result[0] - 0.1) < 1e-6
    assert abs(result[1] - 0.2) < 1e-6
    assert abs(result[2] - 0.3) < 1e-6


def test_embed_non_dict_json_returns_none(monkeypatch):
    def mock_urlopen(*a, **k):
        return FakeResponse(b'[1, 2, 3]')

    monkeypatch.setattr(e.ollama_endpoint, "open_url", mock_urlopen)
    result = e.embed("hi")
    assert result is None


def test_embed_truncates_overlength_prompt_to_context_budget(monkeypatch):
    import json

    sent = {}

    def fake_open_url(request, timeout=30, **kwargs):
        sent["body"] = json.loads(request.data.decode("utf-8"))
        dim = e.expected_dimension()
        return FakeResponse(json.dumps({"data": [{"index": 0, "embedding": [0.1] * dim}]}).encode("utf-8"))

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open_url)
    monkeypatch.setattr(e.ollama_endpoint, "configured_origin", lambda *a, **k: "http://127.0.0.1:11437")
    monkeypatch.setattr(e, "current_revision", lambda **k: "rev-test")
    monkeypatch.setattr(e, "refresh_runtime_revision", lambda **k: "rev-test")
    monkeypatch.setattr(e, "EMBED_MAX_CHARS", 4000)

    huge = "A" * 9000
    vec = e.embed(huge, base="http://127.0.0.1:11437", model="nomic-embed-text")

    assert vec is not None  # would have been None (HTTP 500) without truncation
    assert len(sent["body"]["input"]) == 4000  # capped, not the full 9000


def test_embed_leaves_short_prompt_untouched(monkeypatch):
    import json

    sent = {}

    def fake_open_url(request, timeout=30, **kwargs):
        sent["body"] = json.loads(request.data.decode("utf-8"))
        dim = e.expected_dimension()
        return FakeResponse(json.dumps({"data": [{"index": 0, "embedding": [0.1] * dim}]}).encode("utf-8"))

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open_url)
    monkeypatch.setattr(e.ollama_endpoint, "configured_origin", lambda *a, **k: "http://127.0.0.1:11437")
    monkeypatch.setattr(e, "current_revision", lambda **k: "rev-test")
    monkeypatch.setattr(e, "refresh_runtime_revision", lambda **k: "rev-test")
    monkeypatch.setattr(e, "EMBED_MAX_CHARS", 4000)

    e.embed("short prompt", base="http://127.0.0.1:11437", model="nomic-embed-text")

    assert sent["body"]["input"] == "short prompt"


def test_local_manifest_revision_without_user_home_uses_only_configured_models(monkeypatch, tmp_path):
    from pathlib import Path

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    assert e.local_manifest_revision("nomic-embed-text") == ""
    assert e.local_manifest_revision("nomic-embed-text", tmp_path) == ""
