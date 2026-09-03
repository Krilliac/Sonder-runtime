"""Session turn claims live in the adapters layer; root names are delegates."""
import server
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.adapters import process_liveness, session_turn_claims


class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _alive(monkeypatch):
    monkeypatch.setattr(process_liveness, "probe_process", lambda _pid: (process_liveness.PROCESS_ALIVE, "owner-1"))
    monkeypatch.setattr(memory_store, "new_id", lambda: "claim-1")


def test_a_free_session_is_claimed_with_the_owner_identity(monkeypatch):
    _alive(monkeypatch)
    seen = []
    monkeypatch.setattr(memory_store, "claim_session_turn", lambda conn, sid, token, **kw: seen.append((sid, token, kw)) or True)
    conn = _Conn()
    claim, error = session_turn_claims.acquire_session_turn("s1", open_db=lambda: conn, claim_wait_seconds=0)
    assert error == ""
    assert claim["conn"] is conn
    assert (claim["session_id"], claim["claim_token"], claim["owner_identity"]) == ("s1", "claim-1", "owner-1")
    assert seen == [("s1", "claim-1", {"owner_pid": claim["owner_pid"], "owner_identity": "owner-1"})]


def test_contended_or_unavailable_coordination_refuses_without_leaking_a_connection(monkeypatch):
    _alive(monkeypatch)
    monkeypatch.setattr(memory_store, "claim_session_turn", lambda *a, **kw: False)
    conn = _Conn()
    claim, error = session_turn_claims.acquire_session_turn("s1\nx", open_db=lambda: conn, claim_wait_seconds=0)
    assert claim is None
    assert error == "ERROR: session 's1 x' already has a turn in progress; retry shortly."
    assert conn.closed

    def broken():
        raise RuntimeError("no db")

    assert session_turn_claims.acquire_session_turn("s1", open_db=broken, claim_wait_seconds=0) == (
        None, "ERROR: session turn coordination is unavailable.",
    )
    monkeypatch.setattr(process_liveness, "probe_process", lambda _pid: ("gone", ""))
    assert session_turn_claims.acquire_session_turn("s1", open_db=lambda: _Conn(), claim_wait_seconds=0) == (
        None, "ERROR: session owner identity is unavailable.",
    )


def test_release_retries_with_a_fresh_connection_then_abandons(monkeypatch):
    released = []
    monkeypatch.setattr(memory_store, "release_session_turn", lambda conn, sid, token: released.append((sid, token)))
    conn = _Conn()
    claim = {"conn": conn, "session_id": "s1", "claim_token": "t", "owner_pid": 7, "owner_identity": "o"}
    session_turn_claims.release_session_turn_claim(claim, open_db=lambda: _Conn())
    assert released == [("s1", "t")]
    assert conn.closed
    abandoned = []

    def failing(_conn, _sid, _token):
        raise RuntimeError("db gone")

    monkeypatch.setattr(memory_store, "release_session_turn", failing)
    monkeypatch.setattr(memory_store, "abandon_session_turn_claim", lambda *args: abandoned.append(args))
    monkeypatch.setattr(session_turn_claims.time, "sleep", lambda _s: None)
    session_turn_claims.release_session_turn_claim(dict(claim, conn=_Conn()), open_db=lambda: _Conn())
    assert abandoned == [("s1", "t", 7, "o")]
    session_turn_claims.release_session_turn_claim(None, open_db=lambda: _Conn())


def test_root_delegates_use_the_server_database_seam(monkeypatch):
    _alive(monkeypatch)
    monkeypatch.setattr(memory_store, "claim_session_turn", lambda *a, **kw: True)
    monkeypatch.setattr(memory_store, "release_session_turn", lambda *a: None)
    conn = _Conn()
    monkeypatch.setattr(server, "_open_db", lambda: conn)
    claim, error = server._acquire_persistent_session_turn("s1")
    assert error == ""
    assert claim["conn"] is conn
    server._release_persistent_session_turn(claim)
    assert conn.closed
