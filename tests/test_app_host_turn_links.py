"""Host turns must enter the retained account authority transaction guard."""

import pytest
from tests.test_app_control_http import control, invoke
from tests.test_app_managed_authority import managed
from sonder_runtime.application.agents.host_turns import advance_host_turn, host_turn_link


def test_app_host_turn_admission_and_link_are_fenced_by_selection(managed):
    authority, selection, lanes, model, context, binding, token, credential = managed
    host = authority.continuation_service(selection)
    parent = host.open_parent(selection.context)
    bound = host.register_parent(
        parent["parent_session_id"], parent["parent_token"],
        selection.host_conversation_id, context=selection.context,
        command_id="registered-host-turn",
    )
    admission = advance_host_turn(bound, "app-host-turn")
    link = host_turn_link(bound, admission)
    assert link.parent_session_id == parent["parent_session_id"]
    assert link.host_conversation_id == selection.host_conversation_id
    assert model.calls == 0
    invoke(binding, token, "clear_selection",
        dict(command_id="clear-host-turn", expected_epoch=1), credential)
    with pytest.raises(PermissionError):
        host_turn_link(bound, admission)
    with pytest.raises(PermissionError):
        advance_host_turn(bound, "replacement-host-turn")
