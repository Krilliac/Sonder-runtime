from concurrent.futures import ThreadPoolExecutor

import pytest
import server
from sonder_runtime.interfaces import standalone_agent_lanes as lanes
from tests.test_delegated_verification import lanes as lane_env


class Controller:
    _managed_factory = object()
    revoked = False
    calls = 0

    def require_current(self):
        self.calls += 1
        if self.revoked:
            raise PermissionError('host revoked')


def test_model_callback_checks_live_host_before_disclosing_prompt():
    controller = Controller()
    token = lanes._CURRENT.set(controller)
    prompts = []
    try:
        generate = server._guard_managed_agent_call(lambda prompt: prompts.append(prompt))
        generate('first')
        controller.revoked = True
        with pytest.raises(PermissionError):
            generate('private next prompt')
        assert prompts == ['first']
    finally:
        lanes._CURRENT.reset(token)


def test_speculation_carries_host_scope_and_checks_revocation_on_worker():
    controller = Controller()
    token = lanes._CURRENT.set(controller)
    try:
        callback = server._guard_managed_agent_call(lambda: lanes.current(), inherit_context=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(callback).result() is controller
            controller.revoked = True
            with pytest.raises(PermissionError):
                executor.submit(callback).result()
    finally:
        lanes._CURRENT.reset(token)


def test_direct_tool_dispatch_refuses_revoked_host_before_effect(monkeypatch):
    controller = Controller()
    controller.revoked = True
    token = lanes._CURRENT.set(controller)
    monkeypatch.setattr(server, 'file_read', lambda **kwargs: pytest.fail('read was dispatched'))
    try:
        with pytest.raises(PermissionError):
            server._agent_dispatch('file_read', {'path': 'private.txt'})
    finally:
        lanes._CURRENT.reset(token)


def test_unmanaged_callback_preserves_existing_callable():
    callback = lambda value: value
    assert server._guard_managed_agent_call(callback) is callback


def test_managed_model_wrapper_preserves_live_response_metadata():
    controller = Controller()
    token = lanes._CURRENT.set(controller)
    try:
        def callback(prompt):
            callback.last_response_meta = {'done_reason': 'length'}
            return prompt
        callback.last_response_meta = {}
        guarded = server._guard_managed_agent_call(callback)
        guarded('prompt')
        assert guarded.last_response_meta == {'done_reason': 'length'}
    finally:
        lanes._CURRENT.reset(token)


def test_managed_budget_reaches_provider_and_resets_override(monkeypatch):
    limits = []

    def post(path, payload, timeout=None):
        limit = payload['options']['num_predict']
        limits.append(limit)
        return {'message': {'content': 'ok'}, 'eval_count': limit}

    monkeypatch.setenv('SONDER_ALLOW_CLOUD', '1')
    monkeypatch.setattr(server, '_post', post)
    token = lanes._CURRENT.set(Controller())
    try:
        raw = server._make_generate(
            'glm-5.2:cloud', '', 0.1, 1200, 8192, cloud=True,
            compact_cloud_reasoning=True,
        )
        guarded = server._guard_managed_agent_call(raw)
        generate = server._bounded_cloud_agent_generate(
            guarded, per_call_limit=3, total_budget=5,
        )
        assert generate('first') == 'ok'
        assert raw.num_predict_override is None
        assert generate('second') == 'ok'
        assert raw.num_predict_override is None
        with pytest.raises(server.ModelCallError):
            generate('exhausted')
        assert limits == [3, 2]
    finally:
        lanes._CURRENT.reset(token)


def test_actual_managed_loop_revocation_after_model_return_prevents_tool(lane_env, monkeypatch):
    from dataclasses import replace
    import json
    from tests.test_managed_standalone_session import setup

    active = []
    def factory(controller, application):
        session, _, host, grant = setup(lane_env, controller)
        active.append((session, grant))
        return session
    calls = []
    def generate(*args, **kwargs):
        calls.append('model')
        grant = active[0][1]
        grant[0] = replace(grant[0], workspace_roots=())
        return json.dumps({'tool': 'file_read', 'args': {'path': 'private.txt'}})
    monkeypatch.setattr(server, '_make_generate', lambda *a, **k: generate)
    monkeypatch.setattr(server.sonder_speculation, 'speculation_enabled', lambda: False)
    monkeypatch.setattr(server, 'file_read', lambda **kwargs: pytest.fail('post-revocation tool'))
    with lanes.managed_controller_factory_scope(factory):
        with lanes.controller_scope(lambda: object()) as controller:
            try:
                server._agent_impl('Inspect the repository', max_steps=1,
                    project=str(lane_env[3]), return_host_receipt=True)
            except PermissionError:
                pass
    assert calls == ['model']


def test_nested_model_loop_keeps_revocation_guard_without_lane_controller():
    controller = Controller()
    token = lanes._CURRENT.set(controller)
    calls = []
    try:
        with server._managed_agent_admission_scope():
            with lanes.model_loop_scope():
                with lanes.model_loop_scope():
                    assert lanes.current() is None
                    guarded = server._guard_managed_agent_call(lambda: calls.append('model'))
                    controller.revoked = True
                    with pytest.raises(PermissionError):
                        guarded()
        assert calls == []
    finally:
        lanes._CURRENT.reset(token)
    assert server._managed_agent_controller() is None
