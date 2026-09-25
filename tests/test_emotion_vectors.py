import emotion_vectors


def test_ensure_vectors_creates_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    vectors, path = emotion_vectors.ensure_vectors()
    assert path.endswith("emotion_vectors.json")
    assert "warmth" in vectors
    assert "empathy" in vectors
    assert "precision" in vectors


def test_ensure_vectors_backfills_new_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    emotion_vectors.write_vectors({"warmth": 0.1})
    vectors, _ = emotion_vectors.ensure_vectors()
    assert vectors["warmth"] == 0.1
    assert "empathy" in vectors
    assert "rigor" in vectors


def test_update_vectors_clamps_and_normalizes_names(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
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
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    emotion_vectors.update_vectors({"warmth": 0.1}, mode="replace")
    vectors, _ = emotion_vectors.update_vectors({"calm": 0.2}, mode="merge")
    assert vectors == {"calm": 0.2, "warmth": 0.1}


def test_update_vectors_reset_restores_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    vectors, _ = emotion_vectors.update_vectors({}, mode="reset")
    assert vectors["warmth"] == emotion_vectors.DEFAULT_VECTORS["warmth"]
    assert "transparency" in vectors


def test_parse_assignments_and_tune_from_text(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
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
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
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
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    server.emotion_vectors.update_vectors({"warmth": 0.7}, mode="replace")
    out = server._build_system("Base system", False, "")
    assert "warmth=+0.70" in out
    assert out.index("warmth=+0.70") < out.index("Base system")


def test_update_emotion_vectors_tool(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    out = server.update_emotion_vectors('{"calm": 0.8}', mode="replace")
    assert "calm=+0.80" in out
    assert server.emotion_vectors.read_vectors() == {"calm": 0.8}


def test_tune_emotion_vectors_tool_and_slash(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
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


def test_resolve_path_accepts_windows_drive_case_mismatch(monkeypatch, tmp_path):
    import os
    import pytest

    if os.path.normcase("A") != os.path.normcase("a"):
        pytest.skip("filesystem is case-sensitive")
    root = tmp_path / "Work"
    root.mkdir()
    target = root / "emotion_vectors.json"
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(root))
    mixed = str(target)
    mixed = mixed[0].swapcase() + mixed[1:]
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", mixed)
    resolved = emotion_vectors._resolve_path()
    assert os.path.normcase(resolved) == os.path.normcase(str(target.resolve()))


def test_resolve_path_still_rejects_a_sibling_checkout(monkeypatch, tmp_path):
    import os
    import pytest

    root = tmp_path / "runtime"
    other = tmp_path / "worktree"
    root.mkdir()
    other.mkdir()
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(root))
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(other / "emotion_vectors.json"))
    with pytest.raises(ValueError, match="must stay inside workspace"):
        emotion_vectors._resolve_path()
    assert "emotion_vectors.json" not in os.listdir(root)


def _bundled(monkeypatch, tmp_path, content=None):
    import json

    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(root))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    bundled = root / "emotion_vectors.json"
    if content is not None:
        bundled.write_text(json.dumps(content), encoding="utf-8")
    return bundled


def test_updates_write_the_state_home_copy_not_the_bundled_file(monkeypatch, tmp_path):
    import json

    bundled = _bundled(monkeypatch, tmp_path, {"warmth": 0.9})
    before = bundled.read_bytes()

    vectors, path = emotion_vectors.update_vectors({"calm": 0.4}, mode="merge")

    assert bundled.read_bytes() == before
    assert path == emotion_vectors._resolve_path(emotion_vectors.state_path())
    assert json.loads(open(path, encoding="utf-8").read()) == {"calm": 0.4, "warmth": 0.9}
    assert vectors == {"calm": 0.4, "warmth": 0.9}


def test_read_order_is_state_home_then_bundled_default(monkeypatch, tmp_path):
    _bundled(monkeypatch, tmp_path, {"warmth": 0.9})
    assert emotion_vectors.read_vectors() == {"warmth": 0.9}
    assert "warmth=+0.90" in emotion_vectors.system_prompt()

    emotion_vectors.update_vectors({"warmth": 0.1}, mode="replace")

    assert emotion_vectors.read_vectors() == {"warmth": 0.1}
    assert "warmth=+0.10" in emotion_vectors.system_prompt()


def test_status_reads_without_creating_a_file(monkeypatch, tmp_path):
    import os

    bundled = _bundled(monkeypatch, tmp_path)
    vectors, path = emotion_vectors.ensure_vectors()

    assert vectors == emotion_vectors.DEFAULT_VECTORS
    assert not bundled.exists()
    assert not os.path.exists(emotion_vectors.state_path())
    assert path == emotion_vectors._resolve_path(str(bundled))


def test_bundled_default_is_never_written_without_an_override(monkeypatch, tmp_path):
    import pytest

    bundled = _bundled(monkeypatch, tmp_path, {"warmth": 0.2})
    with pytest.raises(ValueError, match="refusing to write the bundled"):
        emotion_vectors.write_vectors({"warmth": 0.5}, str(bundled))
    assert '"warmth": 0.2' in bundled.read_text(encoding="utf-8")


def test_configured_override_is_still_the_live_file(monkeypatch, tmp_path):
    import os

    bundled = _bundled(monkeypatch, tmp_path, {"warmth": 0.2})
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", "custom_vectors.json")

    _vectors, path = emotion_vectors.update_vectors({"calm": 0.3}, mode="replace")

    assert path == str((bundled.parent / "custom_vectors.json").resolve())
    assert emotion_vectors.read_vectors() == {"calm": 0.3}
    assert not os.path.exists(emotion_vectors.state_path())


def test_emotion_command_leaves_the_tracked_repo_file_untouched():
    import os

    import server

    tracked = os.path.join(os.path.dirname(os.path.abspath(server.__file__)), "emotion_vectors.json")
    with open(tracked, "rb") as handle:
        before = handle.read()

    out = server.emotion_command("joy=5")

    assert "joy=+1.00" in out
    with open(tracked, "rb") as handle:
        assert handle.read() == before
    assert server.emotion_vectors.read_vectors()["joy"] == 1.0
    assert os.path.exists(server.emotion_vectors.state_path())
