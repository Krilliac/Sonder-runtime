import emotion_vectors


def test_ensure_vectors_creates_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    vectors, path = emotion_vectors.ensure_vectors()
    assert path.endswith("emotion_vectors.json")
    assert "warmth" in vectors
    assert "empathy" in vectors
    assert "precision" in vectors


def test_ensure_vectors_backfills_new_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    emotion_vectors.write_vectors({"warmth": 0.1})
    vectors, _ = emotion_vectors.ensure_vectors()
    assert vectors["warmth"] == 0.1
    assert "empathy" in vectors
    assert "rigor" in vectors


def test_update_vectors_clamps_and_normalizes_names(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    vectors, _ = emotion_vectors.update_vectors({
        "Warmth": 2,
        "playfulness": -2,
        "steady-focus": 0.33339,
    }, mode="replace")
    assert vectors["warmth"] == 1.0
    assert vectors["playfulness"] == -1.0
    assert vectors["steady_focus"] == 0.333


def test_update_vectors_merge_preserves_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    emotion_vectors.update_vectors({"warmth": 0.1}, mode="replace")
    vectors, _ = emotion_vectors.update_vectors({"calm": 0.2}, mode="merge")
    assert vectors == {"calm": 0.2, "warmth": 0.1}


def test_update_vectors_reset_restores_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    vectors, _ = emotion_vectors.update_vectors({}, mode="reset")
    assert vectors["warmth"] == emotion_vectors.DEFAULT_VECTORS["warmth"]
    assert "transparency" in vectors


def test_parse_assignments_and_tune_from_text(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    vectors, _path, deltas, explicit, matched = emotion_vectors.tune_from_text(
        "be warmer and more concise but rigor=0.7"
    )
    assert vectors["warmth"] > emotion_vectors.DEFAULT_VECTORS["warmth"]
    assert vectors["brevity"] > emotion_vectors.DEFAULT_VECTORS["brevity"]
    assert vectors["rigor"] == 0.7
    assert deltas["warmth"] > 0
    assert explicit == {"rigor": 0.7}
    assert matched


def test_system_prompt_describes_active_vectors(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    emotion_vectors.update_vectors({"warmth": 0.5, "urgency": -0.25}, mode="replace")
    prompt = emotion_vectors.system_prompt()
    assert "Emotion/tone vectors" in prompt
    assert "warmth=+0.50" in prompt
    assert "urgency=-0.25" in prompt
    assert "not internal feelings" in prompt


def test_invalid_vector_name_rejected():
    try:
        emotion_vectors.normalize_vectors({"X": 0.2})
    except ValueError as e:
        assert "invalid emotion vector name" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_build_system_includes_emotion_vectors(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    server.emotion_vectors.update_vectors({"warmth": 0.7}, mode="replace")
    out = server._build_system("Base system", False, "")
    assert "warmth=+0.70" in out
    assert out.index("warmth=+0.70") < out.index("Base system")


def test_update_emotion_vectors_tool(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    out = server.update_emotion_vectors('{"calm": 0.8}', mode="replace")
    assert "calm=+0.80" in out
    assert server.emotion_vectors.read_vectors() == {"calm": 0.8}


def test_tune_emotion_vectors_tool_and_slash(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)
    out = server.tune_emotion_vectors("be warmer and more concise")
    assert "Tuned emotion vectors" in out
    vectors = server.emotion_vectors.read_vectors()
    assert vectors["warmth"] > server.emotion_vectors.DEFAULT_VECTORS["warmth"]
    assert vectors["brevity"] > server.emotion_vectors.DEFAULT_VECTORS["brevity"]

    out = server.emotion_command("set warmth=0.1 directness=0.6")
    assert "warmth=+0.10" in out
    assert "directness=+0.60" in out


def test_update_emotion_vectors_bad_json():
    import server

    assert server.update_emotion_vectors("{bad").startswith("ERROR: vectors_json")
