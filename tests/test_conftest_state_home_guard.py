"""The suite-wide emotion-vector cleanup never touches a real state home.

`tests/conftest.py` deletes ``<state home>/emotion_vectors.json`` before and
after every test. If a test leaked a real state home (SONDER_HOME dropped, a
configure_home override left behind), that delete hit the operator's live
tuning file on every later test of a local run.
"""
import tempfile
import uuid
from pathlib import Path

from sonder_runtime.platform import paths
from tests import conftest as suite


def test_the_session_state_home_is_test_owned():
    assert suite._test_owned(paths.default_home() / "emotion_vectors.json")


def test_cleanup_deletes_a_copy_in_a_test_home(tmp_path):
    copy = tmp_path / "emotion_vectors.json"
    copy.write_text('{"warmth": 0.3}', encoding="utf-8")
    owned, remove = suite.emotion_vector_cleanup(copy)
    remove()
    assert owned
    assert not copy.exists()


def test_cleanup_never_deletes_a_copy_outside_test_homes():
    # A directory beside the checkout stands in for %LOCALAPPDATA%\sonder:
    # outside the system temp directory, like every real state home.
    home = Path(__file__).resolve().parent.parent / (".not-a-test-home-" + uuid.uuid4().hex[:8])
    assert not suite._test_owned(home)
    home.mkdir()
    try:
        live = home / "emotion_vectors.json"
        live.write_text('{"warmth": 0.35}', encoding="utf-8")
        owned, remove = suite.emotion_vector_cleanup(live)
        remove()
        assert not owned
        assert live.read_text(encoding="utf-8") == '{"warmth": 0.35}'
    finally:
        for child in home.iterdir():
            child.unlink()
        home.rmdir()


def test_temp_directory_itself_is_the_ownership_root():
    assert suite._test_owned(Path(tempfile.gettempdir()) / "sonder-pytest-x" / "emotion_vectors.json")
