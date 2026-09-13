from scripts import nightly_self_improve


def test_nightly_rehomes_absolute_paths_from_another_checkout(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(other / "emotion_vectors.json"))
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(other / "system_profile.md"))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ("SONDER_EMOTION_VECTORS", "SONDER_SYSTEM_PROFILE")
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(root / "system_profile.md")


def test_nightly_preserves_an_in_checkout_override(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    custom = root / "custom-profile.md"
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", "emotion_vectors.json")
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(custom))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ()
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(custom)
