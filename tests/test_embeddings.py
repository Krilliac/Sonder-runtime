import embeddings as e


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
    monkeypatch.setenv("SONDER_EMBED_DIM", "0")
    assert e.expected_dimension("nomic-embed-text") is None


def test_local_manifest_revision_is_stable_and_changes_with_model_bytes(tmp_path):
    manifest = (
        tmp_path / "manifests" / "registry.ollama.ai" / "library"
        / "nomic-embed-text" / "latest"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'{"config":"sha256:first"}')

    first = e.local_manifest_revision("nomic-embed-text", tmp_path)
    same = e.local_manifest_revision("nomic-embed-text:latest", tmp_path)
    manifest.write_bytes(b'{"config":"sha256:second"}')
    second = e.local_manifest_revision("nomic-embed-text", tmp_path)

    assert first.startswith("ollama-manifest-sha256:")
    assert same == first
    assert second != first


def test_local_manifest_revision_rejects_unsafe_model_path(tmp_path):
    assert e.local_manifest_revision("../outside:latest", tmp_path) == ""


def test_runtime_revision_refresh_observes_local_manifest_retag(
    monkeypatch, tmp_path,
):
    manifest = (
        tmp_path / "manifests" / "registry.ollama.ai" / "library"
        / "nomic-embed-text" / "latest"
    )
    manifest.parent.mkdir(parents=True)
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(e, "EMBED_MODEL", "nomic-embed-text")
    manifest.write_bytes(b"first")
    first = e.refresh_runtime_revision(models_root=tmp_path)
    manifest.write_bytes(b"second")
    second = e.refresh_runtime_revision(models_root=tmp_path)

    assert second != first


def test_manifest_style_env_revision_does_not_hide_live_store_drift(
    monkeypatch, tmp_path,
):
    manifest = (
        tmp_path / "manifests" / "registry.ollama.ai" / "library"
        / "nomic-embed-text" / "latest"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"first")
    first = e.local_manifest_revision("nomic-embed-text", tmp_path)
    monkeypatch.setenv("SONDER_EMBED_REVISION", first)
    manifest.write_bytes(b"second")

    assert e.current_revision(models_root=tmp_path) != first


def test_serving_digest_is_authoritative_over_loopback_filesystem(
    monkeypatch, tmp_path,
):
    manifest = (
        tmp_path / "manifests" / "registry.ollama.ai" / "library"
        / "nomic-embed-text" / "latest"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"filesystem-a")
    served_digest = "b" * 64

    def fake_urlopen(_request, timeout=0.5):
        return FakeResponse(json.dumps({
            "models": [{
                "name": e.EMBED_IDENTITY,
                "digest": served_digest,
            }],
        }).encode("utf-8"))

    import json
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(e.urllib.request, "urlopen", fake_urlopen)

    assert e.current_revision(base="http://127.0.0.1:11434") == (
        "ollama-manifest-sha256:" + served_digest
    )


def test_embed_fails_closed_if_serving_digest_changes_mid_request(monkeypatch):
    state = {"digest": "a" * 64}

    def fake_urlopen(request, timeout=30):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/api/tags"):
            return FakeResponse(json.dumps({
                "models": [{
                    "name": e.EMBED_IDENTITY,
                    "digest": state["digest"],
                }],
            }).encode("utf-8"))
        state["digest"] = "b" * 64
        return FakeResponse(b'{"embedding": [1.0, 0.0]}')

    import json
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(e, "BASE", "http://example.test:11434")
    monkeypatch.setattr(e.urllib.request, "urlopen", fake_urlopen)

    assert e.embed("task") is None


def test_provenance_stays_bound_after_another_revision_refresh(monkeypatch):
    state = {"digest": "a" * 64}

    def fake_urlopen(request, timeout=30):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/api/tags"):
            return FakeResponse(json.dumps({
                "models": [{
                    "name": e.EMBED_IDENTITY,
                    "digest": state["digest"],
                }],
            }).encode("utf-8"))
        return FakeResponse(b'{"embedding": [1.0, 0.0]}')

    import json
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(e, "BASE", "http://example.test:11434")
    monkeypatch.setattr(e.urllib.request, "urlopen", fake_urlopen)
    vector = e.embed("task")
    bound = e.provenance(vector)["revision"]
    state["digest"] = "b" * 64
    e.refresh_runtime_revision()

    assert e.provenance(vector)["revision"] == bound
    assert e.EMBED_REVISION != bound


def test_valid_vector_soft_rejects_values_outside_float32():
    assert e.valid_vector([10 ** 400]) is False
    assert e.valid_vector([1e308]) is False


def test_embed_soft_fails_to_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ollama")

    monkeypatch.setattr(e.urllib.request, "urlopen", boom)
    assert e.embed("anything") is None


def test_embed_success_path(monkeypatch):
    def mock_urlopen(*a, **k):
        return FakeResponse(b'{"embedding": [0.1, 0.2, 0.3]}')

    monkeypatch.setattr(e.urllib.request, "urlopen", mock_urlopen)
    result = e.embed("hi")
    assert result is not None
    assert len(result) == 3
    assert abs(result[0] - 0.1) < 1e-6
    assert abs(result[1] - 0.2) < 1e-6
    assert abs(result[2] - 0.3) < 1e-6


def test_embed_non_dict_json_returns_none(monkeypatch):
    def mock_urlopen(*a, **k):
        return FakeResponse(b'[1, 2, 3]')

    monkeypatch.setattr(e.urllib.request, "urlopen", mock_urlopen)
    result = e.embed("hi")
    assert result is None
