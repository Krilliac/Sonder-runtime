from threading import Event, Thread
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections


def test_owned_context_exit_is_not_handle_cleanup(tmp_path):
    owner = OwnedSQLiteConnections((tmp_path,))
    with owner.connect(tmp_path / "state.db") as connection:
        connection.execute("CREATE TABLE example(id INTEGER)")
    assert owner.snapshot().open_handles == 1
    assert owner.close_current_thread().clean
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert owner.snapshot().open_handles == 0


def test_other_thread_handle_never_closed_by_requesting_owner(tmp_path):
    owner = OwnedSQLiteConnections((tmp_path,))
    opened, release = Event(), Event()
    errors = []
    def work():
        try:
            connection = owner.connect(tmp_path / "state.db")
            opened.set()
            assert release.wait(5)
            connection.execute("SELECT 1")
            assert owner.close_current_thread().clean
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    worker.start()
    try:
        assert opened.wait(5)
        owner.stop_admissions()
        assert not owner.close_current_thread().clean
        assert owner.snapshot().open_handles == 1
        with pytest.raises(RuntimeError):
            owner.connect(tmp_path / "other.db")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert owner.snapshot().clean


def test_uncommitted_transaction_cleanup_is_not_clean(tmp_path):
    owner = OwnedSQLiteConnections((tmp_path,))
    connection = owner.connect(tmp_path / "state.db")
    connection.execute("CREATE TABLE example(id INTEGER)")
    connection.execute("INSERT INTO example VALUES(1)")
    receipt = owner.close_current_thread()
    assert not receipt.clean
    assert receipt.open_handles == 0
    assert not owner.snapshot().clean


def test_unowned_path_and_capacity_refuse_before_file_creation(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    owner = OwnedSQLiteConnections((private,), max_connections=1)
    with pytest.raises(RuntimeError):
        owner.connect(tmp_path / "outside.db")
    assert not (tmp_path / "outside.db").exists()
    connection = owner.connect(private / "one.db")
    with pytest.raises(RuntimeError):
        owner.connect(private / "two.db")
    assert not (private / "two.db").exists()
    connection.close()
    assert owner.snapshot().clean


def test_missing_native_handle_proof_remains_occupied(tmp_path):
    owner = OwnedSQLiteConnections((tmp_path,))
    connection = owner.connect(tmp_path / "state.db")
    sqlite3.Connection.close(connection)  # simulate lost external close receipt
    assert not owner.close_current_thread().clean
    assert owner.snapshot().open_handles == 1


def test_runtime_sqlite_construction_uses_explicit_owned_factory():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "sonder_runtime"
    bypasses = []
    for source in root.rglob("*.py"):
        if source.name == "owned_sqlite.py":
            continue
        text = source.read_text(encoding="utf-8-sig")
        if "sqlite3" not in text:
            continue
        tree = ast.parse(text)
        aliases = {alias.asname or alias.name for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names if alias.name == "sqlite3"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases and node.attr == "connect":
                bypasses.append((str(source.relative_to(root)), node.lineno))
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3" and any(alias.name in {"connect", "*"} for alias in node.names):
                bypasses.append((str(source.relative_to(root)), node.lineno))
    assert bypasses == []


def test_connect_initialization_race_closes_without_publishing(tmp_path, monkeypatch):
    owner = OwnedSQLiteConnections((tmp_path,))
    entered, release = Event(), Event()
    native = sqlite3.connect
    def constructing(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return native(*args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", constructing)
    failures = []
    def work():
        try:
            owner.connect(tmp_path / "state.db")
        except RuntimeError:
            failures.append("stopped")
    thread = Thread(target=work)
    thread.start()
    try:
        assert entered.wait(5)
        owner.stop_admissions()
        assert owner.snapshot().constructing == 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert failures == ["stopped"]
    assert owner.snapshot().clean


def test_real_repository_and_cached_factory_in_owned_child(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = r"""
import json, sys
from pathlib import Path
from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections, install_disposable_owner
root = Path(sys.argv[1])
owner = OwnedSQLiteConnections((root,))
install_disposable_owner(owner)
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.adapters.persistence.sqlite_factory import cached_connection
repository = SQLiteSessionRepository(root / 'sessions.db')
repository.append('one', 'message.user', {'text': 'fixture'})
assert repository.read_range('one')[0].payload == {'text': 'fixture'}
assert owner.snapshot().open_handles == 0
connection = cached_connection('fixture', root / 'cache.db')
connection.execute('CREATE TABLE example(id INTEGER)')
assert owner.snapshot().open_handles == 1
owner.stop_admissions()
assert repository.close(timeout=0)
assert owner.close_current_thread().clean
print(json.dumps({'closed': owner.snapshot().clean, 'rows': 1}))
"""
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment["SONDER_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout) == {"closed": True, "rows": 1}
