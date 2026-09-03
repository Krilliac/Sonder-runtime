"""Verified code-repair persistence lives in the adapters layer; the root name is a delegate."""
import server
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.adapters import code_repair_persistence as persistence


class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _expected(**overrides):
    row = {"tokens_in": 10, "tokens_out": 20, "token_source": "ollama"}
    row.update(overrides)
    return row


def test_invalid_inputs_are_refused_before_touching_the_database():
    def never():
        raise AssertionError("database must not be opened")

    persist = persistence.persist_verified_code_repair
    assert persist("", _expected(), "fixed", {"tokens_in": 1, "tokens_out": 1}, open_db=never) is False
    assert persist("i1", None, "fixed", {"tokens_in": 1, "tokens_out": 1}, open_db=never) is False
    assert persist("i1", _expected(), "fixed", "usage", open_db=never) is False
    assert persist("i1", _expected(), "fixed", {"tokens_in": 1}, open_db=never) is False
    assert persist("i1", _expected(), "fixed", {"tokens_in": -1, "tokens_out": 1}, open_db=never) is False
    assert persist("i1", _expected(tokens_in="x"), "fixed", {"tokens_in": 1, "tokens_out": 1}, open_db=never) is False


def test_the_repair_is_swapped_in_with_summed_usage_and_provenance(monkeypatch):
    seen = []

    def cas(conn, interaction_id, **kwargs):
        seen.append((interaction_id, kwargs))
        return True

    monkeypatch.setattr(memory_store, "replace_interaction_response_cas", cas)
    conn = _Conn()
    usage = {"tokens_in": 5, "tokens_out": 7, "token_source": "ollama"}
    assert persistence.persist_verified_code_repair("i1", _expected(), "fixed", usage, open_db=lambda: conn) is True
    assert conn.closed
    interaction_id, kwargs = seen[0]
    assert interaction_id == "i1"
    assert kwargs["response"] == "fixed"
    assert (kwargs["tokens_in"], kwargs["tokens_out"]) == (15, 27)
    assert kwargs["token_source"] == "ollama+code-repair"
    mixed = {"tokens_in": 1, "tokens_out": 1, "token_source": "estimated"}
    persistence.persist_verified_code_repair("i2", _expected(), "fixed", mixed, open_db=lambda: _Conn())
    assert seen[1][1]["token_source"] == "mixed+code-repair"
    both = {"tokens_in": 1, "tokens_out": 1, "token_source": "estimated"}
    persistence.persist_verified_code_repair("i3", _expected(token_source="estimated"), "fixed", both, open_db=lambda: _Conn())
    assert seen[2][1]["token_source"] == "estimated+code-repair"


def test_database_failures_are_reported_as_not_persisted(monkeypatch):
    def broken():
        raise RuntimeError("no db")

    usage = {"tokens_in": 1, "tokens_out": 1}
    assert persistence.persist_verified_code_repair("i1", _expected(), "fixed", usage, open_db=broken) is False

    def failing_cas(*_args, **_kwargs):
        raise RuntimeError("cas failed")

    monkeypatch.setattr(memory_store, "replace_interaction_response_cas", failing_cas)
    conn = _Conn()
    assert persistence.persist_verified_code_repair("i1", _expected(), "fixed", usage, open_db=lambda: conn) is False
    assert conn.closed


def test_root_delegate_uses_the_server_database_seam(monkeypatch):
    monkeypatch.setattr(memory_store, "replace_interaction_response_cas", lambda *a, **kw: True)
    conn = _Conn()
    monkeypatch.setattr(server, "_open_db", lambda: conn)
    assert server._persist_verified_code_repair("i1", _expected(), "fixed", {"tokens_in": 1, "tokens_out": 1}) is True
    assert conn.closed
