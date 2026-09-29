from types import SimpleNamespace
import sqlite3
import time
from pathlib import Path

from sonder_runtime.adapters.persistence.app_control import AppControlTransaction, _encode
from sonder_runtime.application.ports.app_control import (
    AppControlLimits,
    BindingRecord,
    GrantSnapshot,
)
from sonder_runtime.bootstrap.app_control_http import AppControlBinding


def test_list_bindings_pages_are_scoped_before_cursor_is_returned():
    """An unrelated project cannot reveal its position through an empty page."""
    expected_grant = object()
    other = SimpleNamespace(runtime_id="runtime-test-1", grant=object())

    class Transaction:
        def list_bindings(self, *, principal_id, after_position, limit, **scope):
            assert principal_id.startswith("account:")
            return SimpleNamespace(items=(other,), next_position=1)

        def list_grant_bindings(
            self, *, principal_id, runtime_id, grant,
            after_position, limit
        ):
            assert principal_id == "account:owner"
            assert runtime_id == "runtime-test-1"
            assert grant is expected_grant
            return SimpleNamespace(items=(), next_position=None)

    binding = object.__new__(AppControlBinding)
    binding.store = SimpleNamespace(atomic=lambda callback: callback(Transaction()))
    binding._session = lambda account, credential: (
        SimpleNamespace(principal_id="account:owner", runtime_id="runtime-test-1", grant=expected_grant),
        expected_grant,
    )
    binding._current = lambda conn, token, account, current_grant: None
    binding._config = lambda: SimpleNamespace(app_control=SimpleNamespace(page_cap=100))
    account = SimpleNamespace(username="alice")

    (status, body), _ = binding._perform(
        None, account, "account-token", "list_bindings", {}, "control-token"
    )
    assert status == 200
    assert body == {"ok": True, "items": [], "next_position": None}


def test_store_grant_pages_use_scoped_cursors():
    now = time.time()
    owner = "account:" + "a" * 64
    runtime = "runtime-test-1"
    root = str(Path.cwd().resolve())

    def grant(project):
        return GrantSnapshot(
            project, 1, project, (root,), ("read_file",), False, False,
            now + 3600, "b" * 64, "c" * 64, (1, 2, 3, 4)
        )

    first, second = grant("project1"), grant("project2")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE app_host_bindings (position INTEGER PRIMARY KEY, "
        "id TEXT, principal TEXT, runtime TEXT, host TEXT, record TEXT)"
    )
    for position, (bid, policy) in enumerate(
        (("other", second), ("first", first), ("more", first)), start=1
    ):
        record = BindingRecord(
            bid, "app-session:" + bid, owner, runtime, policy, now, now + 1800
        )
        conn.execute(
            "INSERT INTO app_host_bindings VALUES (?,?,?,?,?,?)",
            (position, bid, owner, runtime, record.canonical_host_id, _encode(record)),
        )
    conn.commit()
    conn.execute("BEGIN")
    tx = AppControlTransaction(conn, SimpleNamespace(limits=AppControlLimits()))
    page = tx.list_grant_bindings(
        principal_id=owner, runtime_id=runtime, grant=first, limit=1
    )
    assert [item.binding_id for item in page.items] == ["first"]
    assert page.next_position == 1
    page = tx.list_grant_bindings(
        principal_id=owner, runtime_id=runtime, grant=first,
        after_position=page.next_position, limit=1
    )
    assert [item.binding_id for item in page.items] == ["more"]
    assert page.next_position is None
    page = tx.list_grant_bindings(
        principal_id=owner, runtime_id=runtime, grant=second, limit=1
    )
    assert [item.binding_id for item in page.items] == ["other"]
    assert page.next_position is None
    conn.rollback()
    conn.close()
