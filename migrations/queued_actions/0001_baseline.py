"""Adoption baseline for the append-only queued-actions ledger."""

manages_own_transaction = True


def apply(conn) -> None:
    import queued_actions

    conn.executescript(queued_actions._SCHEMA)


def verify(conn) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {"queued_actions", "queued_action_transitions"}
    if not required <= tables:
        raise RuntimeError("queued-actions baseline verify failed")

