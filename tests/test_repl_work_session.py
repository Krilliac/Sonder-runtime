import pytest

import server
import sonder_runtime.interfaces.repl.repl as repl


@pytest.mark.parametrize('new_thread', [False, True])
def test_work_persists_exact_host_selected_session_before_execution(monkeypatch, new_thread):
    monkeypatch.setattr(repl, '_legacy_runtime', None)
    repl.configure_legacy_runtime(server)
    ids = iter(('1111111111111111', '2222222222222222'))
    monkeypatch.setattr(repl.memory_store, 'new_id', lambda: next(ids))
    lines = iter((('/new',) if new_thread else ()) + ('/work inspect repository', '/exit'))
    monkeypatch.setattr(repl, '_read_input', lambda *_a, **_k: next(lines))
    monkeypatch.setattr(repl, '_startup_banner', lambda *_a: '')
    monkeypatch.setattr(repl, '_maybe_live_reload', lambda: None)
    monkeypatch.setattr(repl, '_named_command_gate', lambda *_a: (True, ''))
    expected = '2222222222222222' if new_thread else '1111111111111111'
    calls = []
    def run(**kwargs):
        conn = server._open_db()
        try:
            row = repl.memory_store.get_session(conn, expected)
        finally:
            conn.close()
        assert row is not None and row['session_id'] == expected
        calls.append(kwargs)
        return 'inspected'
    monkeypatch.setattr(server, 'workbench_agent', run)
    repl.main()
    assert len(calls) == 1
