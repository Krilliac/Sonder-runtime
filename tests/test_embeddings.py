import embeddings as e


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


def test_embed_soft_fails_to_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ollama")

    monkeypatch.setattr(e.urllib.request, "urlopen", boom)
    assert e.embed("anything") is None
