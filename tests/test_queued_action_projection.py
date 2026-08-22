from sonder_runtime.adapters.persistence import queued_actions


def test_list_actions_is_bounded_and_payload_free(tmp_path):
    connection = queued_actions.connect(tmp_path / "queued.db")
    try:
        request = queued_actions.ActionRequest.create(
            "approval-1", "file_write", {"path": "secret.txt"},
            proposed_by=queued_actions.Actor.MODEL,
        )
        queued_actions.propose(connection, request)
        records = queued_actions.list_actions(connection, limit=10)
    finally:
        connection.close()

    assert len(records) == 1
    assert records[0].state is queued_actions.ActionState.PROPOSED
    assert not hasattr(records[0], "payload")
