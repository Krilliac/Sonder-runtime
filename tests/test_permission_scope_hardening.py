"""Scoped permission modes: ceiling, unambiguous keys, lowering-only HTTP setter."""
from __future__ import annotations

import json

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.interfaces.http import chat_tool_stream


@pytest.fixture
def mode_state(monkeypatch, tmp_path):
    state = tmp_path / "permission_mode.json"
    monkeypatch.setattr(pm, "_state_path", lambda: str(state))
    saved = (dict(pm._STATE), dict(pm._SCOPED_MODES), pm._LOADED)
    pm._reset_for_tests()
    yield state
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved[0])
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved[1])
        pm._LOADED = saved[2]


# --- finding 1: a lowered global mode is a ceiling ---------------------------


def test_console_global_lowering_clamps_a_granted_session_mode(mode_state):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)  # console: global auto
        with pm.mode_scope("owner", "s1"):
            assert pm.set_session_mode(pm.AUTO) == pm.AUTO
            assert pm.current_mode() == pm.AUTO
        pm.set_mode(pm.MANUAL)  # console: lower the global mode (no scope)
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL  # and stays after reading again
    assert pm.global_mode() == pm.MANUAL
    assert pm._SCOPED_MODES == {}


def test_effective_mode_is_the_stricter_even_if_a_loose_scope_is_stored(mode_state):
    pm.set_mode(pm.MANUAL)
    with pm._LOCK:
        pm._SCOPED_MODES[pm._scope_key("owner", "s1")] = pm.AUTO
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL


def test_stricter_scope_survives_the_global_mode_being_raised(mode_state):
    pm.set_mode(pm.AUTO)
    with pm.mode_scope("owner", "s1"):
        pm.set_session_mode(pm.PLAN)
    pm.set_mode(pm.ACCEPT_EDITS)
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.PLAN


def test_cycle_mode_without_scope_also_clamps_scopes(mode_state):
    pm.set_mode(pm.ACCEPT_EDITS)
    with pm.mode_scope("owner", "s1"):
        pm.set_session_mode(pm.ACCEPT_EDITS)
    pm.cycle_mode(-1)  # acceptEdits -> manual
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL


def test_saved_scope_above_saved_global_loads_clamped(mode_state):
    mode_state.write_text(json.dumps({
        "mode": "manual", "scope_format": 2,
        "scopes": {pm._scope_key("owner", "s1"): "auto"},
    }), encoding="utf-8")
    pm._reset_for_tests()
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL


# --- finding 2: unambiguous scope keys --------------------------------------


def test_pipe_in_principal_does_not_collide_with_another_scope(mode_state):
    pm.set_mode(pm.AUTO)
    with pm.mode_scope("ops|east", "1"):
        pm.set_session_mode(pm.PLAN)
    with pm.mode_scope("ops", "east|1"):
        assert pm.current_mode() == pm.AUTO
        pm.set_session_mode(pm.MANUAL)
    with pm.mode_scope("ops|east", "1"):
        assert pm.current_mode() == pm.PLAN
    with pm.mode_scope("ops", "east|1"):
        assert pm.current_mode() == pm.MANUAL
    assert pm._scope_key("ops|east", "1") != pm._scope_key("ops", "east|1")


def test_scope_keys_round_trip_through_the_state_file(mode_state):
    pm.set_mode(pm.AUTO)
    with pm.mode_scope("ops|east", "1"):
        pm.set_session_mode(pm.PLAN)
    pm._reset_for_tests()
    with pm.mode_scope("ops|east", "1"):
        assert pm.current_mode() == pm.PLAN
    with pm.mode_scope("ops", "east|1"):
        assert pm.current_mode() == pm.AUTO


def test_legacy_keys_migrate_only_when_unambiguous(mode_state):
    mode_state.write_text(json.dumps({
        "mode": "auto",
        "scopes": {"ops|s1": "plan", "ops|east|1": "plan", "nopipe": "plan"},
    }), encoding="utf-8")
    pm._reset_for_tests()
    with pm.mode_scope("ops", "s1"):
        assert pm.current_mode() == pm.PLAN
    # The ambiguous legacy key is dropped (fails closed for a loosened mode,
    # and neither candidate reading inherits it).
    with pm.mode_scope("ops|east", "1"):
        assert pm.current_mode() == pm.AUTO
    with pm.mode_scope("ops", "east|1"):
        assert pm.current_mode() == pm.AUTO
    assert list(pm._SCOPED_MODES) == [pm._scope_key("ops", "s1")]


def test_session_ids_are_bounded_and_validated(mode_state):
    for session in ("x" * 129, "bad\nid", "nul\x00id"):
        with pm.mode_scope("owner", session):
            with pytest.raises(ValueError):
                pm.set_session_mode(pm.PLAN)
            with pytest.raises(ValueError):
                pm.set_mode(pm.PLAN)
    assert pm.global_mode() == pm.MANUAL  # a refused scoped write never fell through to global
    assert pm._SCOPED_MODES == {}


# --- finding 3: the unattended HTTP session setter only lowers ---------------


class _Handler:
    def __init__(self, session: str):
        self.headers = {"X-Sonder-Session": session}
        self.sent = []

    def _correlation(self) -> str:
        return "corr-test"

    def _send_json_payload(self, payload, status=200):
        self.sent.append((status, payload))


def _post(session, body):
    handler = _Handler(session)
    chat_tool_stream.handle_permission_mode_post(
        handler, body, {"mode": "account", "principal": "owner"},
        permission_policy=permission_policy,
        principal_of=lambda _ctx: "owner",
        idempotent_action=lambda _ctx, _key, _name, action: action(),
        send_idempotency_refusal=lambda _h, _r: False,
        observe_state=lambda: None,
        mode_data=lambda: {"mode": pm.current_mode()},
        approver_of=lambda _ctx: "owner",
    )
    return handler.sent[-1]


def test_http_session_setter_lowers_but_refuses_to_raise(mode_state):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    status, _ = _post("s1", {"mode": "plan", "scope": "session"})
    assert status == 200
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.PLAN
    status, _ = _post("s1", {"mode": "acceptEdits", "scope": "session"})
    assert status == 403
    status, _ = _post("s1", {"mode": "auto", "scope": "session"})
    assert status == 403
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.PLAN


def test_http_session_raise_takes_effect_only_after_console_approval(mode_state):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    _post("s1", {"mode": "manual", "scope": "session"})
    status, _ = _post("s1", {"mode": "auto", "scope": "session"})
    assert status == 403
    with pm.mode_scope("owner", "s1"):
        assert pm.current_mode() == pm.MANUAL
        with pm.attended_mode_change():
            assert pm.set_session_mode(pm.AUTO) == pm.AUTO  # console approval
        assert pm.current_mode() == pm.AUTO
