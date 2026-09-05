"""Actual loopback Handler and account/fleet/approval storage recovery acceptance.

Models and verifier job gateway are scripted; this does not prove an external
provider, a native verification subprocess, or a multi-machine deployment.
"""

import time
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import admin_auth
import pytest
import server
from tests.test_app_work_http import work_http, control, preparation
from tests.test_app_control_http import invoke


def install(h):
    from sonder_runtime.bootstrap.app_work_recovery_http import (
        AppWorkRecoveryHttpBinding,
    )

    slot = []

    def register(app, registry):
        assert app is h.service.application and not slot
        slot.append(registry)
        return SimpleNamespace(
            commit=lambda: None, rollback=lambda **kw: registry.close()
        )

    def require(app):
        assert app is h.service.application
        return slot[0]

    service = AppWorkRecoveryHttpBinding(
        h.service, register_owned=register, require_owned=require
    )
    h.service._recovery_binding = service
    return service


def observed(h, path, *, headers=None):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status, _, body = h.request("GET", path, custom_headers=headers)
        assert status == 200, body
        if not body["recovery"]["busy"]:
            return body["recovery"]
        time.sleep(2.1)  # Respect the real wire boundary's per-peer admission rate.
    pytest.fail("bounded recovery callback has not completed")


def test_http_original_pending_new_login_and_two_separate_approvals(
    work_http, monkeypatch
):
    from sonder_runtime.bootstrap.managed_conversation import _ManagedTurn
    from tests.test_delegated_verification import _verifier

    h = work_http
    recovery = install(h)
    verified = []
    original_stage = _ManagedTurn.stage_final

    def stage_impl(view, facts):
        lanes = h.service.lanes
        with view._session._bound._scope() as current:
            child = lanes.spawn(
                command_id="recovery-child",
                parent_session_id=view._session.parent_session_id,
                task="inspect",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
                max_wall_seconds=600,
            )["lane"]
        lanes.run_pending(child["id"], current)
        verifier, gateway, proofs = _verifier(
            (lanes, lanes.store, lanes.gateway, current.workspace_roots[0], current, {})
        )
        execute = gateway.execute_check

        def checked(*args, **kwargs):
            execute(*args, **kwargs)
            for proof in proofs.values():
                proof["digest"] = hashlib.sha256(
                    json.dumps(
                        {k: v for k, v in proof.items() if k != "digest"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()

        gateway.execute_check = checked
        verified.append((verifier, gateway))
        assert not view.verify_delegated(
            view._draft, verifier_factory=lambda *args: verifier
        ).valid
        original_stage(
            view, replace(facts, delegated_work=True, terminal_class="UNVERIFIED")
        )

    def stage(view, facts):
        try:
            return stage_impl(view, facts)
        except BaseException:
            import traceback

            h.failures.append(traceback.format_exc())
            raise

    monkeypatch.setattr(_ManagedTurn, "stage_final", stage)
    monkeypatch.setattr(
        server, "_standalone_verifier_factory", lambda *args: verified[0][0]
    )
    try:
        status, _, body = h.request("POST", "/v1/app-control/work", preparation())
        assert status == 200, body
        wid = body["work"]["work_id"]
        workpath = "/v1/app-control/work/" + wid
        status, _, body = h.request("POST", workpath + "/execute", {})
        assert status == 409, body
        pending = body["pending"]
        h.ledger.issue(pending["tool"], pending["call_digest"], approver="operator")
        assert h.request("POST", workpath + "/execute", {})[0] == 202
        h.service.dispatcher._executor.shutdown(wait=True)
        status, _, original = h.request("GET", workpath)
        assert status == 200 and original["work"]["state"] == "verification_pending", (
            json.dumps(original) + "\n" + "\n".join(h.failures)
        )
        # A fresh login and control enrollment select the original durable binding.
        selected = h.service._issue(
            h.token, h.credential, "read_work", {"work_id": wid}
        )
        binding_id = selected.binding.binding_id
        original_row = h.service.dispatcher.status(selected, work_id=wid)
        h.service.authority.release_selection(selected)
        conn = h.account_open()
        token, _ = admin_auth.login(conn, "alice", "test-password")
        conn.close()
        status, enrolled = invoke(
            h.control,
            token,
            "enroll",
            dict(
                command_id="fresh-enroll", project="project1", password="test-password"
            ),
        )
        assert status == 201, enrolled
        credential = enrolled["control_token"]
        status, body = invoke(
            h.control,
            token,
            "select_binding",
            dict(
                command_id="fresh-select",
                binding_id=binding_id,
                expected_binding_revision=1,
                expected_epoch=0,
            ),
            credential,
        )
        assert status == 200, body
        headers = dict(
            h.headers,
            **{"X-Sonder-Account-Token": token, "X-Sonder-App-Control": credential}
        )
        status, _, body = h.request(
            "POST",
            workpath + "/recovery",
            dict(attachment_command_id="reattach", completion_command_id="certify"),
            custom_headers=headers,
        )
        assert status == 202, body
        path = "/v1/app-control/recovery/" + body["recovery"]["attempt_id"]
        ready = observed(h, path, headers=headers)
        assert ready["phase"] == "prepared", ready
        for action, phase in (
            ("attach", "attachment_pending"),
            ("attach", "attached"),
            ("resume", "approval_pending"),
            ("resume", "terminal"),
        ):
            status, _, body = h.request(
                "POST", path + "/" + action, {}, custom_headers=headers
            )
            assert status == 202, body
            result = observed(h, path, headers=headers)
            assert result["phase"] == phase, result
            if phase.endswith("pending"):
                approval = result["approval"]
                h.ledger.issue(
                    approval["tool"], approval["call_digest"], approver="operator"
                )
                assert verified[0][1].calls == 0
        assert verified[0][1].calls == 1 and len(h.models) == 1
        entry = recovery.registry._entries[body["recovery"]["attempt_id"]]
        assert entry.work.prepared == original_row.prepared
        assert (
            entry.work.terminal == original_row.verification_pending.original_terminal
        )
        assert result["work"]["completion"]["phase"] == "certified_after_return"
        encoded = json.dumps(result)
        assert (
            str(h.service.output_root) not in encoded
            and "account_session_ref" not in encoded
        )
        assert "issuer" not in encoded and "inspected repository" not in encoded
    finally:
        recovery.registry.close()
