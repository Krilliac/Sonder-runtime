"""A discarded banner prefetch must not publish into a later REPL session."""
import threading

from sonder_runtime.interfaces.repl import repl


def test_late_discarded_prefetch_cannot_overwrite_the_next_sessions_value(monkeypatch):
    release_old = threading.Event()
    old_started = threading.Event()
    calls = []

    def fake_read():
        calls.append(None)
        if len(calls) == 1:
            old_started.set()
            release_old.wait(5)
            return {"session": "old"}
        return {"session": "new"}

    monkeypatch.setattr(repl, "_read_banner_source", fake_read)
    repl.discard_banner_prefetch()
    try:
        old_thread = repl.prefetch_banner_source()
        assert old_started.wait(5)
        repl.discard_banner_prefetch()

        new_thread = repl.prefetch_banner_source()
        new_thread.join(5)
        release_old.set()
        old_thread.join(5)
        assert not old_thread.is_alive()

        assert repl._banner_source() == {"session": "new"}
    finally:
        release_old.set()
        repl.discard_banner_prefetch()


def test_prefetched_value_is_consumed_once(monkeypatch):
    monkeypatch.setattr(repl, "_read_banner_source", lambda: {"n": 1})
    repl.discard_banner_prefetch()
    repl.prefetch_banner_source().join(5)
    assert repl._banner_source() == {"n": 1}
    monkeypatch.setattr(repl, "_read_banner_source", lambda: {"n": 2})
    assert repl._banner_source() == {"n": 2}
