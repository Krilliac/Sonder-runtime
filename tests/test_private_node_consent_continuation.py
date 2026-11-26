"""private_node_allowed survives persistence and resume: narrowing only."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from tests.test_lane_continuation import granted, make_host


@pytest.fixture
def lanes(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)

    class Model:
        calls = 0

        def generate(self, request, context):
            self.calls += 1
            return ModelResponse("Completed", "scripted", "code", tokens_out=1)

    model = Model()
    service = AgentLaneService(store, sessions, model, auto_start=False)
    root = tmp_path / "repo"
    root.mkdir()
    context = local_owner_context(correlation_id="verification", workspace_roots=(root,))
    parent = service.open_model_parent(context)
    return service, store, model, root, context, parent


def _register(lanes, flag):
    host, context, parent, current = make_host(lanes)
    context = replace(context, private_node_allowed=flag)
    bound = host.register_parent(
        parent["parent_session_id"], parent["parent_token"], "host-task",
        context=context, command_id="register",
    )
    return host, context, bound


def test_false_is_persisted_and_survives_resume_with_an_unspecified_context(lanes):
    host, context, bound = _register(lanes, False)
    assert bound.authority_ceiling().private_node_allowed is False
    identity = bound.continuation_id
    bound.close()
    # A fresh surface context that never set the field (None) is narrowed to False.
    fresh = replace(
        context, correlation_id="fresh", private_node_allowed=None,
        deadline_monotonic=time.monotonic() + 600,
    )
    prepared = host.prepare_reattachment(host.select(identity, fresh), fresh, command_id="attach")
    new = host.execute_reattachment(prepared, fresh, approve=granted)
    assert new.authority_ceiling().private_node_allowed is False
    new.close()


def test_resume_cannot_widen_false_to_true(lanes):
    host, context, bound = _register(lanes, False)
    identity = bound.continuation_id
    bound.close()
    wider = replace(
        context, correlation_id="wider", private_node_allowed=True,
        deadline_monotonic=time.monotonic() + 600,
    )
    with pytest.raises(PermissionError):
        host.select(identity, wider)


def test_unspecified_registration_stays_unspecified_and_may_be_narrowed(lanes):
    host, context, bound = _register(lanes, None)
    assert bound.authority_ceiling().private_node_allowed is None
    identity = bound.continuation_id
    bound.close()
    narrower = replace(
        context, correlation_id="narrower", private_node_allowed=False,
        deadline_monotonic=time.monotonic() + 600,
    )
    prepared = host.prepare_reattachment(
        host.select(identity, narrower), narrower, command_id="attach",
    )
    new = host.execute_reattachment(prepared, narrower, approve=granted)
    assert new.authority_ceiling().private_node_allowed is False
    new.close()


def test_standalone_admission_narrows_to_the_ceiling(lanes):
    from sonder_runtime.bootstrap.standalone_continuation import HostContinuationAdmission

    host, context, bound = _register(lanes, False)
    root = lanes[3]
    guard = HostContinuationAdmission(
        bound, replace(context, private_node_allowed=True),
        private_paths=lambda: (root.parent / "approvals.db",),
        model_writable_roots=lambda: (root,),
    )
    assert guard.context.private_node_allowed is False
    bound.close()
