"""Chat reference normalisation: id, ``sonder:chat/<id>`` code and ``/app/#/c/<id>`` link."""
import pytest

import memory_store
import server
from sonder_runtime.domain.session_ref import chat_code, is_valid_session_id, parse_session_ref

pytestmark = pytest.mark.unit

SID = "chat-1794840126388000"


@pytest.mark.parametrize("text", [
    SID,
    "  " + SID + "\n",
    "sonder:chat/" + SID,
    "http://127.0.0.1:8000/app/#/c/" + SID,
    "https://example.local:9443/app/#/c/" + SID,
    "http://[::1]:8000/app/#/c/" + SID,
    "/app/#/c/" + SID,
    "#/c/" + SID,
    "http://localhost/app/#/c/" + SID + "/",
    "HTTP://Example.COM:65535/app/#/c/" + SID,
    "http://h/#/c/" + SID,
    "http://h:1/app#/c/" + SID,
])
def test_all_forms_normalise_to_the_id(text):
    assert parse_session_ref(text) == SID


@pytest.mark.parametrize("text", [
    "", "   ", None, 42, b"chat-1",
    "../etc/passwd", "..", ".", "a/b", "a\\b", "chat-1/../x",
    "sonder:chat/../x", "sonder:chat/", "sonder:chat/a/b", "sonder:chat/" + SID + "/x",
    "http://h/app/#/c/", "http://h/app/#/c/a/b", "http://h/app/#/c/..%2f..",
    "http://h/app/#/c/" + SID + "?x=1",
    "ftp://h/app/#/c/" + SID,
    "http://user@h/app/#/c/" + SID, "http://user:pw@h:80/app/#/c/" + SID,
    "http://h:0/app/#/c/" + SID, "http://h:65536/app/#/c/" + SID,
    "http://h:99999/app/#/c/" + SID, "http://h:80x/app/#/c/" + SID,
    "%61bc", "sonder:chat/%61bc", "http://h/app/#/c/%61bc",
    "sonder:session/" + SID, "chat id", "id\x00", "café",
])
def test_junk_and_traversal_are_rejected(text):
    assert parse_session_ref(text) is None


def test_overlong_input_is_rejected():
    assert parse_session_ref("a" * 128) == "a" * 128
    assert parse_session_ref("a" * 129) is None
    assert parse_session_ref("sonder:chat/" + "a" * 129) is None
    assert parse_session_ref("http://h/app/#/c/" + "a" * 129) is None
    assert parse_session_ref("http://h/" + "p" * 3000 + "#/c/x") is None


def test_validity_and_code_helpers():
    assert is_valid_session_id(SID) and not is_valid_session_id("..") and not is_valid_session_id("")
    assert chat_code(SID) == "sonder:chat/" + SID
    with pytest.raises(ValueError):
        chat_code("a/b")


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, SID, "proj")
        memory_store.log_interaction(conn, "I0", "first question", "", "first answer", "code",
                                     session_id=SID)
    finally:
        conn.close()


@pytest.mark.parametrize("ref", [
    SID, "sonder:chat/" + SID, "http://127.0.0.1:8000/app/#/c/" + SID,
])
def test_session_export_accepts_id_code_and_link(monkeypatch, tmp_path, ref):
    _seed(monkeypatch, tmp_path)
    out = server.session_export(ref)
    assert out.startswith("session: " + SID)
    assert "USER: first question" in out and "ASSISTANT: first answer" in out


def test_session_export_still_reports_unknown_reference(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    assert server.session_export("sonder:chat/chat-nope").startswith("ERROR: no session")
