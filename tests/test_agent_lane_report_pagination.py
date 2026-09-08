"""Durable report pages retain all history while bounding each body query."""

from dataclasses import replace
from contextlib import contextmanager

import pytest

from tests.test_interactive_agent_lanes import env
from sonder_runtime.adapters.persistence.agent_lanes import LaneTransaction


def seed(env, *, parent="parent", count=105):
    service, store, _, _, context, root = env
    lane = service.spawn(
        command_id="spawn-" + parent,
        parent_session_id=parent,
        task="report task",
        workspace_root=str(root / "child"),
        context=context,
    )["lane"]["id"]
    with store.transaction() as tx:
        row = tx.lane(lane)
        ids = [
            tx.message(
                row,
                "report " + str(index),
                "parent",
                report=True,
                source_sequence=index + 1,
            )
            for index in range(count)
        ]
    return lane, ids


def test_more_than_100_reports_traverse_exactly_once_and_oldest_can_be_acked(env):
    lane, expected = seed(env)
    service, _, _, _, context, _ = env
    actual = []
    cursor = 0
    while True:
        page = service.reports("parent", context, cursor=cursor, limit=20)
        assert len(page["reports"]) <= 20
        actual.extend(report["id"] for report in page["reports"])
        previous = cursor
        cursor = page["next_cursor"]
        assert cursor > previous
        if not page["has_more"]:
            break
    assert actual == expected and len(set(actual)) == 105
    service.ack_report(
        expected[0],
        command_id="ack-oldest",
        context=context,
        parent_session_id="parent",
    )
    assert service.reports("parent", context, limit=1)["reports"][0]["acknowledged"]
    empty = service.reports("parent", context, cursor=cursor, limit=20)
    assert empty == {"reports": [], "next_cursor": cursor, "has_more": False}


@pytest.mark.parametrize(
    "count,more", [(0, False), (20, False), (21, True), (100, True), (105, True)]
)
def test_report_has_more_uses_limit_plus_one(env, count, more):
    _, ids = seed(env, count=count)
    page = env[0].reports("parent", env[-2], limit=20)
    assert [r["id"] for r in page["reports"]] == ids[:20]
    assert page["has_more"] is more


def test_report_pages_preserve_parent_principal_scope(env):
    lane, ids = seed(env)
    service = env[0]
    context = env[-2]
    assert service.reports("other-parent", context)["reports"] == []
    assert (
        service.reports("parent", replace(context, principal_id="other-owner"))[
            "reports"
        ]
        == []
    )
    with pytest.raises(PermissionError):
        service.ack_report(
            ids[0],
            command_id="wrong-parent",
            context=context,
            parent_session_id="other-parent",
        )
    with pytest.raises(PermissionError):
        service.ack_report(
            ids[0],
            command_id="wrong-owner",
            context=replace(context, principal_id="other-owner"),
        )
    assert [r["id"] for r in service.reports(None, context, limit=1)["reports"]] == ids[
        :1
    ]


def test_report_pages_do_not_scan_mailboxes_or_load_history_bodies(env, monkeypatch):
    lane, ids = seed(env)

    def forbidden(*args, **kwargs):
        pytest.fail("legacy mailbox or all-lane body scan called")

    monkeypatch.setattr(LaneTransaction, "messages", forbidden)
    monkeypatch.setattr(LaneTransaction, "lanes", forbidden)
    page = env[0].reports("parent", env[-2], limit=7)
    assert [r["id"] for r in page["reports"]] == ids[:7]


def test_report_body_query_fetches_only_limit_plus_one_rows(env, monkeypatch):
    lane, ids = seed(env, count=205)
    original = env[1].transaction
    fetched = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            fetched.append(len(rows))
            return rows

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, args=()):
            cursor = self.connection.execute(sql, args)
            if "f.body" in sql:
                assert sql.index("LIMIT ?") < sql.index("JOIN fleet_messages")
                return Cursor(cursor)
            return cursor

    @contextmanager
    def transaction():
        with original() as tx:
            tx.conn = Connection(tx.conn)
            yield tx

    monkeypatch.setattr(env[1], "transaction", transaction)
    page = env[0].reports("parent", env[-2], limit=7)
    assert [r["id"] for r in page["reports"]] == ids[:7]
    assert fetched == [8]


def test_reports_added_after_first_page_follow_monotonic_cursor(env):
    lane, ids = seed(env, count=105)
    first = env[0].reports("parent", env[-2], limit=100)
    with env[1].transaction() as tx:
        row = tx.lane(lane)
        ids.extend(
            tx.message(row, "new report", "parent", report=True) for _ in range(3)
        )
    second = env[0].reports("parent", env[-2], cursor=first["next_cursor"], limit=100)
    assert [r["id"] for r in first["reports"] + second["reports"]] == ids
    assert not second["has_more"]


def test_console_can_ack_oldest_report_in_history_over_100(env):
    from tests.test_repl_agent_lanes import facade

    lane, ids = seed(env, count=105)
    ui = facade(env)
    assert ids[0] in ui.run("reports " + lane)
    assert "Recorded ack" in ui.run("ack " + lane + " " + ids[0])
    assert env[0].reports("parent", env[-2], limit=1)["reports"][0]["acknowledged"]


def test_reports_sequence_index_installs_idempotently_on_existing_history(env):
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore

    lane, ids = seed(env, count=105)
    with env[1].connect() as connection:
        connection.execute("DROP INDEX agent_lane_reports_sequence")
    SQLiteAgentLaneStore(env[1].path, env[2])
    SQLiteAgentLaneStore(env[1].path, env[2])
    with env[1].connect() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='agent_lane_reports_sequence'"
        ).fetchone()
    assert env[0].reports("parent", env[-2], limit=1)["reports"][0]["id"] == ids[0]

def test_empty_parent_scope_does_not_mean_all_parents(env):
    lane, ids = seed(env, parent="valid-parent", count=3)
    service, _, _, _, context, _ = env
    assert service.reports("", context) == {
        "reports": [], "next_cursor": 0, "has_more": False,
    }
    assert [row["id"] for row in service.reports(None, context)["reports"]] == ids
    assert [row["id"] for row in service.reports("valid-parent", context)["reports"]] == ids
