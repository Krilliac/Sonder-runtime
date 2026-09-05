import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
import pytest
import admin_auth
from sonder_runtime.bootstrap.app_control_http import AppControlBinding
from sonder_runtime.platform.config import SonderConfig, FeaturesConfig
from sonder_runtime.platform.app_control_config import AppControlConfig
from sonder_runtime.adapters.security.control_plane_paths import (
    ControlPlanePaths,
    live_control_plane_inventory,
)


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTH_SECRET", "strong-test-key-" + "a" * 48)
    private = tmp_path / "private"
    from sonder_runtime.application.compute_fabric.artifact_spool import (
        PrivateDirectoryAnchor,
    )

    with PrivateDirectoryAnchor.open_base(private):
        pass
    root = tmp_path / "workspace"
    root.mkdir()
    db = private / "accounts.db"
    fleet = private / "fleet.db"
    catalog = private / "catalog.json"

    def account_open():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn

    conn = account_open()
    admin_auth.register(conn, "alice", "test-password", role="admin")
    token, _ = admin_auth.login(conn, "alice", "test-password")
    conn.close()
    entry = dict(
        grant_id="grant1",
        revision=1,
        project="project1",
        accounts=["alice"],
        role="admin",
        roots=[str(root)],
        tools=["read_file"],
        allow_cloud=False,
        allow_remote=False,
        expires_at=time.time() + 7200,
    )
    catalog.write_text(json.dumps(dict(version=1, grants=[entry])), encoding="utf8")
    catalog.chmod(0o600)
    base = SonderConfig()
    config = replace(
        base,
        features=FeaturesConfig(host_control=True),
        state=replace(base.state, workspace_roots=(str(root),)),
        server=replace(base.server, host="127.0.0.1", auth_mode="both"),
        secrets=replace(base.secrets, api_key="deployment-key"),
        app_control=AppControlConfig(
            enabled=True,
            runtime_id="runtime-test-1",
            catalog_file=str(catalog),
            allow_numeric_loopback_native=True,
        ),
    )
    state = {"config": config}
    binding = AppControlBinding(
        lambda: state["config"],
        account_open=account_open,
        account_path=lambda: db,
        fleet_path=lambda: fleet,
        private_inventory=lambda: live_control_plane_inventory(
            additional=lambda: ControlPlanePaths(
                databases=(db, fleet), files=(catalog,)
            )
        ),
    )
    binding.start()
    return binding, token, state, account_open, catalog, entry


def invoke(binding, token, action, payload, control_token=""):
    result = []
    binding.perform(
        action,
        payload,
        account_token=token,
        control_token=control_token,
        publish=lambda status, body: result.append((status, body)),
    )
    return result[0]


def test_real_enrollment_retry_and_binding_lifecycle(control):
    binding, token, *_ = control
    status, body = invoke(
        binding,
        token,
        "enroll",
        dict(command_id="enroll1", project="project1", password="test-password"),
    )
    assert status == 201
    credential = body["control_token"]
    assert (
        invoke(
            binding,
            token,
            "enroll",
            dict(command_id="enroll1", project="project1", password="test-password"),
        )[1]["error"]["code"]
        == "CREDENTIAL_DELIVERY_UNKNOWN"
    )
    status, created = invoke(
        binding,
        token,
        "create_binding",
        dict(command_id="create1", title="Test", local_history_alias="local-chat"),
        credential,
    )
    assert status == 200
    bid = created["receipt"]["entity_id"]
    status, selected = invoke(
        binding,
        token,
        "select_binding",
        dict(
            command_id="select1",
            binding_id=bid,
            expected_binding_revision=1,
            expected_epoch=0,
        ),
        credential,
    )
    assert status == 200 and selected["receipt"]["selection_epoch"] == 1
    assert (
        invoke(binding, token, "list_bindings", {}, credential)[1]["items"][0][
            "binding_id"
        ]
        == bid
    )
    assert (
        invoke(
            binding,
            token,
            "clear_selection",
            dict(command_id="clear1", expected_epoch=1),
            credential,
        )[0]
        == 200
    )
    assert (
        invoke(
            binding,
            token,
            "revoke_binding",
            dict(command_id="revoke1", binding_id=bid, expected_revision=1),
            credential,
        )[0]
        == 200
    )
    raw = Path(binding.store.path).read_bytes()
    assert (
        credential.encode() not in raw
        and token.encode() not in raw
        and b"test-password" not in raw
    )


@pytest.fixture
def http_control(control, monkeypatch):
    from sonder_runtime.interfaces.http import serve
    from http.server import ThreadingHTTPServer
    import threading

    binding, token, state, *_ = control
    monkeypatch.setattr(serve, "_APP_CONTROL_BINDING", binding)
    monkeypatch.setattr(serve, "API_KEY", "deployment-key")
    monkeypatch.setattr(serve, "AUTH_MODE", "both")
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, token, binding
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(port, method, path, body=None, headers=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    encoded = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, encoded, headers or {})
    response = conn.getresponse()
    raw = response.read()
    result = (response.status, dict(response.getheaders()), json.loads(raw))
    conn.close()
    return result


def test_actual_http_both_mode_and_exact_retry(http_control):
    port, token, binding = http_control
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": token,
    }
    payload = dict(command_id="enroll1", project="project1", password="test-password")
    status, h, body = request(port, "POST", "/v1/app-control/enroll", payload, headers)
    assert status == 201
    assert h["Cache-Control"] == "no-store" and h["Referrer-Policy"] == "no-referrer"
    assert request(port, "POST", "/v1/app-control/enroll", payload, headers)[0] == 409
    credential = body["control_token"]
    headers["X-Sonder-App-Control"] = credential
    status, _, body = request(
        port, "POST", "/v1/app-control/bindings", dict(command_id="create1"), headers
    )
    assert status == 200
    assert (
        request(port, "GET", "/v1/app-control/bindings", headers=headers)[2]["items"][
            0
        ]["binding_id"]
        == body["receipt"]["entity_id"]
    )
    del headers["Authorization"]
    assert request(port, "GET", "/v1/app-control/bindings", headers=headers)[0] == 401


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"Origin": "https://evil.example"}, {}, 403),
        ({}, {"password": "x" * 17000}, 413),
        (
            {},
            {
                "command_id": "x",
                "project": "project1",
                "password": "test-password",
                "principal_id": "owner",
            },
            400,
        ),
    ],
)
def test_http_rejections_before_generic_routes(http_control, headers, body, status):
    port, token, _ = http_control
    actual = {
        "Content-Type": "application/json",
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": token,
        **headers,
    }
    response = request(port, "POST", "/v1/app-control/enroll", body, actual)
    assert response[0] == status
    assert response[1]["Cache-Control"] == "no-store"


def enrolled(control):
    binding, token, *_ = control
    status, body = invoke(
        binding,
        token,
        "enroll",
        dict(command_id="enroll1", project="project1", password="test-password"),
    )
    assert status == 201
    return body["control_token"]


@pytest.mark.parametrize(
    "change", ["logout", "role", "expiry", "rotation", "catalog", "roots"]
)
def test_live_authority_changes_fence_existing_control(control, monkeypatch, change):
    binding, token, state, open_db, catalog, entry = control
    credential = enrolled(control)
    if change in {"logout", "role", "expiry"}:
        conn = open_db()
        if change == "logout":
            admin_auth.revoke_session(conn, token)
        elif change == "role":
            admin_auth.set_account(conn, "alice", role="user")
        else:
            conn.execute("UPDATE account_sessions SET expires_ts=1")
            conn.commit()
        conn.close()
    elif change == "rotation":
        monkeypatch.setenv("SONDER_AUTH_SECRET", "rotated-test-key-" + "b" * 48)
    elif change == "catalog":
        entry["revision"] = 2
        catalog.write_text(json.dumps(dict(version=1, grants=[entry])), encoding="utf8")
    else:
        config = state["config"]
        state["config"] = replace(
            config, state=replace(config.state, workspace_roots=(str(catalog.parent),))
        )
    status, body = invoke(binding, token, "list_bindings", {}, credential)
    assert status >= 400 and "items" not in body


def test_wrong_password_role_grant_and_second_account(control):
    binding, token, state, open_db, *_ = control
    bad = dict(command_id="enroll1", project="project1", password="wrong-password")
    assert invoke(binding, token, "enroll", bad)[0] >= 400
    assert (
        invoke(
            binding,
            token,
            "enroll",
            {**bad, "password": "test-password", "project": "other"},
        )[0]
        >= 400
    )
    credential = enrolled(control)
    conn = open_db()
    admin_auth.register(conn, "bob", "other-password", role="admin")
    other, _ = admin_auth.login(conn, "bob", "other-password")
    conn.close()
    assert invoke(binding, other, "list_bindings", {}, credential)[0] == 401
    assert (
        invoke(
            binding,
            other,
            "enroll",
            dict(command_id="bob1", project="project1", password="other-password"),
        )[0]
        == 403
    )


def test_lost_secret_publication_retry_never_delivers_another_secret(control):
    binding, token, *_ = control
    payload = dict(command_id="enroll1", project="project1", password="test-password")

    def fail(status, body):
        assert status == 201
        raise BrokenPipeError("disconnected")

    with pytest.raises(BrokenPipeError):
        binding.perform(
            "enroll", payload, account_token=token, control_token="", publish=fail
        )
    status, body = invoke(binding, token, "enroll", payload)
    assert status == 409 and "control_token" not in body


def test_revocation_after_commit_suppresses_secret(control, monkeypatch):
    binding, token, state, open_db, *_ = control
    original = binding.store._commit

    def commit(conn):
        original(conn)
        if conn.execute("SELECT count(*) FROM app_control_sessions").fetchone()[0]:
            other = open_db()
            admin_auth.revoke_session(other, token)
            other.close()

    monkeypatch.setattr(binding.store, "_commit", commit)
    status, body = invoke(
        binding,
        token,
        "enroll",
        dict(command_id="enroll1", project="project1", password="test-password"),
    )
    assert status == 401 and "control_token" not in body


def test_duplicate_header_and_json_rejected_actual_wire(http_control):
    import http.client

    port, token, _ = http_control
    for duplicate in (
        "Origin",
        "X-Sonder-Account-Token",
        "Authorization",
        "Content-Length",
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = b"{}"
        conn.putrequest("POST", "/v1/app-control/enroll")
        fields = {
            "Authorization": "Bearer deployment-key",
            "X-Sonder-Account-Token": token,
            "Content-Length": "2",
            "Content-Type": "application/json",
        }
        if duplicate == "Origin":
            fields["Origin"] = "https://evil.example"
        for key, value in fields.items():
            conn.putheader(key, value)
        conn.putheader(duplicate, fields[duplicate])
        conn.endheaders(body)
        response = conn.getresponse()
        assert response.status == 400
        response.read()
        conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        "/v1/app-control/enroll",
        b'{"command_id":"one","command_id":"two"}',
        {
            "Authorization": "Bearer deployment-key",
            "X-Sonder-Account-Token": token,
            "Content-Type": "application/json",
        },
    )
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()


def test_recovery_uses_canonical_binding_and_actual_lane_store(control):
    from types import SimpleNamespace
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore

    binding, token, *_ = control
    credential = enrolled(control)
    status, body = invoke(
        binding,
        token,
        "create_binding",
        dict(command_id="create1", local_history_alias="forged-parent"),
        credential,
    )
    bid = body["receipt"]["entity_id"]
    lane_store = SQLiteAgentLaneStore(binding.store.path, None)
    binding._lanes = lambda: SimpleNamespace(
        store=lane_store, allowed_tools=("read_file",)
    )
    status, body = invoke(binding, token, "recovery", dict(binding_id=bid), credential)
    assert (
        status == 200 and body["items"] == [] and body["execution_available"] is False
    )
    assert body["binding"]["host_conversation_id"] == "app-session:" + bid
    assert (
        invoke(
            binding, token, "recovery", dict(binding_id="forged-parent"), credential
        )[0]
        == 404
    )
    assert (
        invoke(
            binding, token, "recovery", dict(binding_id=bid, limit=1000), credential
        )[0]
        == 400
    )


def test_weak_actual_secret_prevents_start(control, monkeypatch):
    binding, *_ = control
    monkeypatch.setenv("SONDER_AUTH_SECRET", "weak")
    from sonder_runtime.application.ports.app_control_http import ControlError

    with pytest.raises(ControlError):
        binding.start()


def test_password_admission_is_bounded_before_pbkdf(control, monkeypatch):
    binding, token, *_ = control
    calls = []
    original = admin_auth.reauthenticate

    def count(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(admin_auth, "reauthenticate", count)
    for n in range(8):
        assert (
            invoke(
                binding,
                token,
                "enroll",
                dict(
                    command_id="bad" + str(n),
                    project="project1",
                    password="wrong-password",
                ),
            )[0]
            == 403
        )
    assert (
        invoke(
            binding,
            token,
            "enroll",
            dict(command_id="next", project="project1", password="test-password"),
        )[0]
        == 429
    )
    assert len(calls) == 8


def test_peer_request_slots_and_body_admission_are_bounded(http_control):
    from sonder_runtime.interfaces.http import app_control as wire

    port, token, _ = http_control
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": token,
    }
    held = []
    try:
        for _ in range(8):
            assert wire._SLOTS.acquire(blocking=False)
            held.append(1)
        assert request(port, "POST", "/v1/app-control/enroll", {}, headers)[0] == 429
    finally:
        for _ in held:
            wire._SLOTS.release()


def test_proxy_only_backend_cannot_use_native_option_as_bypass(control):
    binding, token, state, *_ = control
    config = state["config"]
    state["config"] = replace(
        config,
        server=replace(
            config.server,
            tls_terminated_by_proxy=True,
            cors_origins=("https://app.example",),
        ),
        app_control=replace(
            config.app_control,
            app_origins=("https://app.example",),
            proxy_cidrs=("10.0.0.0/8",),
            proxy_only_backend=True,
        ),
    )
    binding._initial = None
    binding.start()
    assert not binding.transport_allowed(
        listener="127.0.0.1", raw_peer="127.0.0.1", origin=None
    )
    assert not binding.transport_allowed(
        listener="0.0.0.0", raw_peer="10.0.0.1", origin="https://evil.example"
    )
    assert binding.transport_allowed(
        listener="0.0.0.0", raw_peer="10.0.0.1", origin="https://app.example"
    )
    assert not binding.transport_allowed(
        listener="0.0.0.0", raw_peer="10.0.0.1", origin=None
    )


def test_actual_typed_http_configuration_composes_private_store(control, monkeypatch):
    from sonder_runtime.interfaces.http import serve

    binding, token, state, open_db, *_ = control
    monkeypatch.setenv("SONDER_FLEET_DB", binding.store.path)
    conn = open_db()
    account_path = next(
        r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"
    )
    conn.close()
    monkeypatch.setattr(serve.server, "_DB_PATH", account_path)
    monkeypatch.setattr(serve.server, "_open_db", open_db)
    # Restore all mutable configuration globals after invoking the real binder.
    names = (
        "CONFIGURED_PORT",
        "API_KEY",
        "AUTH_SECRET",
        "HOST",
        "REQUIRE_ACCOUNT",
        "AUTH_MODE",
        "CORS_ORIGINS",
        "TLS_TERMINATED_BY_PROXY",
        "ALLOW_REGISTRATION",
        "MAX_REQUEST_BYTES",
        "MAX_DISCARDED_BODY_BYTES",
        "REQUEST_TIMEOUT_SECONDS",
        "STREAM_IDLE_TIMEOUT_SECONDS",
        "HTTP_SESSION_STATE_LIMIT",
        "HTTP_SESSION_STATE_OWNER_LIMIT",
        "TRAIN_MAX_N",
        "_HEALTH_STATUS_FACADE",
        "_TRUSTED_PROXY_NETWORKS",
        "_ARTIFACT_TRANSFER_BINDING",
        "_ARTIFACT_TRANSFER_CONFIG",
        "_APP_CONTROL_BINDING",
        "_APP_CONTROL_CONFIG",
    )
    for name in names:
        monkeypatch.setattr(serve, name, getattr(serve, name))
    serve.configure_typed_config(state["config"])
    composed = serve._APP_CONTROL_BINDING
    assert composed.store.path == binding.store.path
    assert (
        invoke(
            composed,
            token,
            "enroll",
            dict(command_id="enroll1", project="project1", password="test-password"),
        )[0]
        == 201
    )


def test_rejected_control_start_preserves_published_artifact_binding(control, monkeypatch):
    from types import SimpleNamespace
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.bootstrap import artifact_transfer, app_control_http
    calls = []
    old_config = object()
    prior = SimpleNamespace(close=lambda: calls.append("prior closed"))
    candidate = SimpleNamespace(start=lambda: calls.append("candidate started"),
                                close=lambda: calls.append("candidate closed"))
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_CONFIG", old_config)
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", prior)
    original_control = serve._APP_CONTROL_BINDING
    original_control_config = serve._APP_CONTROL_CONFIG
    monkeypatch.setattr(artifact_transfer, "ArtifactTransferBinding", lambda config: candidate)
    def reject(self):
        raise PermissionError("control startup refused")
    monkeypatch.setattr(app_control_http.AppControlBinding, "start", reject)
    with pytest.raises(PermissionError, match="control startup refused"):
        serve.configure_typed_config(control[2]["config"])
    assert serve._ARTIFACT_TRANSFER_BINDING is prior
    assert serve._ARTIFACT_TRANSFER_CONFIG is old_config
    assert serve._APP_CONTROL_BINDING is original_control
    assert serve._APP_CONTROL_CONFIG is original_control_config
    assert calls == ["candidate started", "candidate closed"]


def test_account_source_replacement_refuses_identical_database_copy(control):
    import shutil

    binding, token, state, open_db, *_ = control
    credential = enrolled(control)
    path = Path(binding._account_path())
    prior = path.with_suffix(".prior")
    path.rename(prior)
    shutil.copyfile(prior, path)
    assert invoke(binding, token, "list_bindings", {}, credential)[0] >= 400


def test_configured_deployment_key_cannot_use_generic_local_downgrade(control):
    from sonder_runtime.interfaces.http.serve import _app_control_deployment_authorized

    binding, token, state, *_ = control
    config = state["config"]
    config = replace(
        config,
        server=replace(config.server, auth_mode="api-key"),
        secrets=replace(config.secrets, api_key=""),
    )
    assert not _app_control_deployment_authorized("", config)


def test_all_private_method_errors_are_noncacheable(http_control):
    import http.client

    port, token, _ = http_control
    for method in ("PUT", "DELETE", "PATCH"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            method,
            "/v1/app-control/enroll",
            headers={
                "Authorization": "Bearer deployment-key",
                "X-Sonder-Account-Token": token,
            },
        )
        response = conn.getresponse()
        assert response.status >= 400
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Referrer-Policy") == "no-referrer"
        response.read()
        conn.close()


def test_catalog_rollback_after_higher_enrollment_fences_read_routes(
    control, monkeypatch
):
    import os

    binding, token, state, open_db, catalog, entry = control
    credential = enrolled(control)
    first_stat = catalog.stat()
    first = catalog.read_bytes()
    entry["revision"] = 2
    catalog.write_text(json.dumps(dict(version=1, grants=[entry])), encoding="utf8")
    assert (
        invoke(
            binding,
            token,
            "enroll",
            dict(command_id="enroll2", project="project1", password="test-password"),
        )[0]
        == 201
    )
    catalog.write_bytes(first)
    os.utime(catalog, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    # Same original bytes/inode/mtime again: the durable high-water still wins.
    assert invoke(binding, token, "list_bindings", {}, credential)[0] == 409


def test_selection_readback_is_exact_and_observational(http_control):
    port, token, binding = http_control
    credential = invoke(
        binding,
        token,
        "enroll",
        dict(command_id="enroll1", project="project1", password="test-password"),
    )[1]["control_token"]
    headers = {
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": token,
        "X-Sonder-App-Control": credential,
    }
    assert request(port, "GET", "/v1/app-control/selection", headers=headers)[
        2
    ] == dict(ok=True, selection=None)
    bid = invoke(
        binding, token, "create_binding", dict(command_id="create1"), credential
    )[1]["receipt"]["entity_id"]
    selected = invoke(
        binding,
        token,
        "select_binding",
        dict(
            command_id="select1",
            binding_id=bid,
            expected_binding_revision=1,
            expected_epoch=0,
        ),
        credential,
    )[1]["receipt"]
    public = request(port, "GET", "/v1/app-control/selection", headers=headers)[2][
        "selection"
    ]
    assert public == dict(
        selection_id=selected["entity_id"], epoch=1, binding_id=bid, binding_revision=1
    )
    assert (
        request(port, "GET", "/v1/app-control/selection", headers=headers)[2][
            "selection"
        ]
        == public
    )


def test_actual_http_two_account_binding_isolation(http_control, control):
    port, token, binding = http_control
    _, _, _, open_db, catalog, entry = control
    entry["accounts"] = ["alice", "bob"]
    catalog.write_text(json.dumps(dict(version=1, grants=[entry])), encoding="utf8")
    conn = open_db()
    admin_auth.register(conn, "bob", "other-password", role="admin")
    other, _ = admin_auth.login(conn, "bob", "other-password")
    conn.close()

    def headers(account, credential=""):
        return {
            "Authorization": "Bearer deployment-key",
            "Content-Type": "application/json",
            "X-Sonder-Account-Token": account,
            "X-Sonder-App-Control": credential,
        }

    credentials = []
    for who, password in ((token, "test-password"), (other, "other-password")):
        status, _, result = request(
            port,
            "POST",
            "/v1/app-control/enroll",
            dict(command_id="enroll1", project="project1", password=password),
            headers(who),
        )
        assert status == 201
        credentials.append(result["control_token"])
    first, second = credentials
    result = request(
        port,
        "POST",
        "/v1/app-control/bindings",
        dict(command_id="create1"),
        headers(token, first),
    )[2]
    bid = result["receipt"]["entity_id"]
    assert (
        request(
            port, "GET", "/v1/app-control/bindings", headers=headers(other, second)
        )[2]["items"]
        == []
    )
    assert (
        request(
            port,
            "GET",
            "/v1/app-control/recovery?binding_id=" + bid,
            headers=headers(other, second),
        )[0]
        == 404
    )
    assert (
        request(port, "GET", "/v1/app-control/bindings", headers=headers(other, first))[
            0
        ]
        == 401
    )


def test_wire_does_not_publish_second_response_after_writer_failure(control):
    from email.message import Message
    from types import SimpleNamespace
    from sonder_runtime.interfaces.http.app_control import handle_app_control

    binding, token, state, *_ = control
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = "2"
    headers["X-Sonder-Account-Token"] = token
    replies = []

    def write(*args, **kwargs):
        replies.append(args)
        raise RuntimeError("writer failed after beginning response")

    handler = SimpleNamespace(
        path="/v1/app-control/enroll",
        headers=headers,
        server=SimpleNamespace(server_address=("127.0.0.1", 1234)),
        _peer=lambda: "127.0.0.1",
        _read_json=lambda **kwargs: {},
        _send_json_payload=write,
        close_connection=False,
    )
    fake = SimpleNamespace(
        store=object(),
        _config=lambda: state["config"],
        transport_allowed=lambda **kwargs: True,
        perform=lambda action, payload, **kwargs: kwargs["publish"](201, {"ok": True}),
    )
    assert handle_app_control(
        handler, "POST", fake, deployment_authorized=lambda *_: True
    )
    assert len(replies) == 1 and handler.close_connection
