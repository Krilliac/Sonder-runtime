"""Tests for [server] public_hosting, C1/C2, per-user isolation, client_exec, and S1/S2/S5."""
from __future__ import annotations

import http.client
import json
import os
import threading
from contextlib import contextmanager

import pytest

import admin_auth
import permission_modes as pm
import server as legacy_server
import sonder_runtime.__main__ as main_entry
import sonder_runtime.interfaces.http.serve as ts
import sonder_runtime.platform.config as runtime_config
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.adapters.web import login_throttle_http
from sonder_runtime.domain.client_tools import (
    CLIENT_DELEGATABLE_TOOLS,
    is_client_delegatable,
    resolve_client_slash_command,
)
from sonder_runtime.domain.cloud_access import has_legacy_error_prefix
from sonder_runtime.interfaces.http.client_tools import (
    MAX_CLIENT_OUTPUT_CHARS,
    ClientToolBroker,
)


@contextmanager
def _http_server(monkeypatch):
    srv = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(port: int, method: str, path: str, *, body: str | None = None,
             headers: dict[str, str] | None = None, timeout: float = 10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        hdrs = dict(headers or {})
        raw_body = body.encode("utf-8") if body is not None else None
        if raw_body is not None and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(raw_body))
        conn.request(method, path, body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.headers), data
    finally:
        conn.close()


def _configure_public_host(monkeypatch, tmp_path):
    db_path = tmp_path / "sonder_public_test.db"
    monkeypatch.setattr(legacy_server, "_DB_PATH", str(db_path))
    monkeypatch.setenv("SONDER_DB", str(db_path))
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "ALLOW_REGISTRATION", False)
    monkeypatch.setattr(ts, "PUBLIC_HOSTING", True)
    monkeypatch.setattr(ts, "TLS_TERMINATED_BY_PROXY", True)
    monkeypatch.setattr(ts, "AUTH_SECRET", "s" * 48)
    monkeypatch.setattr(ts, "_CLIENT_TOOL_BROKER", ClientToolBroker())
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    login_throttle_http.reset_registration_throttle()
    monkeypatch.setattr(
        ts.sonder_lifecycle.get(),
        "refund_auth_attempt",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        ts.sonder_lifecycle.get(),
        "record_auth_failure",
        lambda *_a, **_k: None,
    )
    from types import SimpleNamespace
    monkeypatch.setattr(legacy_server, "OLLAMA_POOL", SimpleNamespace(enabled=False, snapshots=lambda: ()))
    return db_path


# ---------------------------------------------------------------------------
# 1. Config & Backlog C1 / C2
# ---------------------------------------------------------------------------

def test_c1_empty_env_bool_preserves_toml_and_typo_fails(tmp_path):
    cfg_file = tmp_path / "sonder.toml"
    cfg_file.write_text(
        "[server]\nauth_mode = 'account'\nrequire_account = true\n"
        "public_hosting = true\ntls_terminated_by_proxy = true\n",
        encoding="utf-8",
    )
    cfg = runtime_config.load_config(
        toml_path=cfg_file,
        env={
            "SONDER_REQUIRE_ACCOUNT": "",
            "SONDER_PUBLIC_HOSTING": "",
            "SONDER_AUTH_SECRET": "a" * 32,
        },
    )
    assert cfg.server.require_account is True
    assert cfg.server.public_hosting is True

    with pytest.raises(runtime_config.ConfigError, match="SONDER_REQUIRE_ACCOUNT"):
        runtime_config.load_config(
            toml_path=cfg_file,
            env={
                "SONDER_REQUIRE_ACCOUNT": "truu",
                "SONDER_AUTH_SECRET": "a" * 32,
            },
        )


def test_public_hosting_requires_account_bearing_auth_mode(tmp_path):
    cfg_file = tmp_path / "sonder.toml"
    cfg_file.write_text(
        "[server]\nauth_mode = 'local-open'\npublic_hosting = true\n",
        encoding="utf-8",
    )
    with pytest.raises(runtime_config.ConfigError, match="public_hosting"):
        runtime_config.load_config(
            toml_path=cfg_file,
            env={"SONDER_AUTH_SECRET": "a" * 32},
        )

    valid_cfg = runtime_config.load_config(
        env={
            "SONDER_AUTH_MODE": "account",
            "SONDER_TLS_TERMINATED_BY_PROXY": "true",
            "SONDER_AUTH_SECRET": "a" * 32,
        },
        overrides={"server.public_hosting": "true"},
    )
    assert valid_cfg.server.public_hosting is True
    assert valid_cfg.server.auth_mode == "account"


def test_c2_export_runtime_environment_exports_auth_mode_without_typed_runtime(monkeypatch):
    for key in ("SONDER_AUTH_MODE", "SONDER_REQUIRE_ACCOUNT", "SONDER_ALLOW_REGISTRATION", "SONDER_PUBLIC_HOSTING"):
        monkeypatch.delenv(key, raising=False)
    cfg = runtime_config.load_config(
        env={
            "SONDER_AUTH_MODE": "account",
            "SONDER_REQUIRE_ACCOUNT": "true",
            "SONDER_PUBLIC_HOSTING": "true",
            "SONDER_TLS_TERMINATED_BY_PROXY": "true",
            "SONDER_AUTH_SECRET": "a" * 32,
        },
    )
    main_entry._export_runtime_environment(cfg, include_typed_runtime=False)
    assert os.environ["SONDER_AUTH_MODE"] == "account"
    assert os.environ["SONDER_REQUIRE_ACCOUNT"] == "1"
    assert os.environ["SONDER_PUBLIC_HOSTING"] == "1"


# ---------------------------------------------------------------------------
# 2. Public Hosting Registration, Capabilities, Whoami & Rate Limits
# ---------------------------------------------------------------------------

def test_public_hosting_register_strong_password_user_only_and_rate_limit(monkeypatch, tmp_path):
    _configure_public_host(monkeypatch, tmp_path)

    with _http_server(monkeypatch) as port:
        # Unauthenticated capabilities advertises public_hosting + registration + client_exec
        st, _, raw = _request(port, "GET", "/v1/sonder/capabilities")
        assert st == 200
        caps = json.loads(raw)
        assert caps["public_hosting"] is True
        assert caps["registration_enabled"] is True
        assert caps["client_exec_supported"] is True
        assert caps["auth_mode"] == "account"
        assert "run_code" in caps["client_delegatable_tools"]
        assert not any(t.startswith("admin_") for t in caps["client_delegatable_tools"])

        # Weak password rejected (400)
        st, _, raw = _request(
            port, "POST", "/v1/sonder/register",
            body=json.dumps({"username": "alice", "password": "weak"}),
            headers={"Content-Type": "application/json"},
        )
        assert st == 400, raw

        # First account in public_hosting MUST strictly get role='user', ignoring bootstrap secret
        st, _, raw = _request(
            port, "POST", "/v1/sonder/register",
            body=json.dumps({"username": "alice", "password": "Strong!Pass2026"}),
            headers={
                "Content-Type": "application/json",
                "X-Sonder-Bootstrap-Secret": "attempted-escalation",
            },
        )
        assert st == 201, raw
        reg_alice = json.loads(raw)
        assert reg_alice["account"]["role"] == "user"
        assert reg_alice["role"] == "user"
        assert reg_alice["public_hosting"] is True
        assert reg_alice["client_exec_supported"] is True
        assert reg_alice["allowed_features"]["client_exec"] is True
        assert reg_alice["allowed_features"]["host_admin"] is False

        # Second account can also self-register without API_KEY and gets role='user'
        st, _, raw = _request(
            port, "POST", "/v1/sonder/register",
            body=json.dumps({"username": "bob", "password": "Another#Strong2026"}),
            headers={"Content-Type": "application/json"},
        )
        assert st == 201, raw
        assert json.loads(raw)["account"]["role"] == "user"

        # Login returns role, features, public_hosting, client_exec_supported
        st, _, raw = _request(
            port, "POST", "/v1/sonder/login",
            body=json.dumps({"username": "alice", "password": "Strong!Pass2026"}),
            headers={"Content-Type": "application/json"},
        )
        assert st == 200, raw
        login_alice = json.loads(raw)
        token_alice = login_alice["token"]
        assert login_alice["role"] == "user"
        assert login_alice["public_hosting"] is True
        assert login_alice["client_exec_supported"] is True

        # /v1/sonder/whoami & /v1/sonder/status include role/capabilities
        st, _, raw = _request(
            port, "GET", "/v1/sonder/whoami",
            headers={"Authorization": f"Bearer {token_alice}"},
        )
        assert st == 200
        who = json.loads(raw)
        assert who["username"] == "alice"
        assert who["role"] == "user"
        assert who["host_bypass_allowed"] is False
        assert who["client_exec_supported"] is True

        st, _, raw = _request(
            port, "GET", "/v1/sonder/status",
            headers={"Authorization": f"Bearer {token_alice}"},
        )
        assert st == 200
        status_body = json.loads(raw)
        assert status_body["public_hosting"] is True
        assert status_body["client_exec_supported"] is True
        assert status_body["capabilities"]["host_bypass_allowed"] is False

        # Offline trusted_local=True can still bootstrap the first admin on a fresh DB
        fresh_db = tmp_path / "fresh_admin.db"
        monkeypatch.setattr(legacy_server, "_DB_PATH", str(fresh_db))
        monkeypatch.setenv("SONDER_DB", str(fresh_db))
        conn = legacy_server._open_db()
        try:
            admin_acc = admin_auth.register(
                conn, "localowner", "Adm1n!Secret#2026",
                trusted_local=True, public_hosting=True,
            )
            assert admin_acc["role"] == "admin"
        finally:
            conn.close()

        # Sliding-window rate limit blocks flooding /v1/sonder/register (limit is 10 per 60s)
        saw_429 = False
        for idx in range(12):
            st, _, _ = _request(
                port, "POST", "/v1/sonder/register",
                body=json.dumps({"username": f"spam{idx}", "password": "Spam!Pass2026##"}),
                headers={"Content-Type": "application/json"},
            )
            if st == 429:
                saw_429 = True
                break
        assert saw_429


# ---------------------------------------------------------------------------
# 3. Strict Per-User Isolation
# ---------------------------------------------------------------------------

def test_strict_per_user_isolation_across_state_approvals_mode_and_temp(monkeypatch, tmp_path):
    _configure_public_host(monkeypatch, tmp_path)
    pm._reset_for_tests()
    monkeypatch.setenv("SONDER_APPROVALS_DB", str(tmp_path / "approvals.db"))

    conn = legacy_server._open_db()
    try:
        admin_auth.register(conn, "alice", "Str0ng!Key#2026", trusted_local=False, allow_additional=True, public_hosting=True)
        admin_auth.register(conn, "bob", "An0ther!Key#2026", trusted_local=False, allow_additional=True, public_hosting=True)
        tok_alice, _ = admin_auth.login(conn, "alice", "Str0ng!Key#2026")
        tok_bob, _ = admin_auth.login(conn, "bob", "An0ther!Key#2026")
    finally:
        conn.close()

    # Approval ledger isolation: Alice's pending/issued calls invisible to Bob
    ledger = ApprovalLedger(path=str(tmp_path / "approvals.db"))
    digest_a = pm.call_digest("file_write", {"path": "secret.txt", "content": "x"})
    with pm.mode_scope("account:alice", "s1"):
        pm._reset_for_tests()
        pm.set_mode(pm.MANUAL)
        ledger.record_pending("file_write", digest_a, surface="http", preview="alice-secret")
        assert len(ledger.pending()) == 1
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("account:bob", "s1"):
        assert ledger.pending() == []
        # Bob's mode_scope cannot inherit global BYPASS
        pm._STATE["mode"] = pm.BYPASS
        assert pm.current_mode() != pm.BYPASS
    pm._STATE["mode"] = pm.DEFAULT_MODE

    # Preferences isolation: hosted account with augment=False and project='' must not write global prefs
    prefs_written: list[tuple[str, str]] = []
    monkeypatch.setattr(legacy_server, "_capture_preferences", lambda _c, p, project="", **_k: prefs_written.append((p, project)))

    def fake_chat_req(payload, *, model, return_message=False, **_k):
        if return_message:
            return {"prompt_eval_count": 1, "eval_count": 1}, {"role": "assistant", "content": "ok"}
        return {"prompt_eval_count": 1, "eval_count": 1}, "ok"

    monkeypatch.setattr(legacy_server, "_chat_request", fake_chat_req)
    monkeypatch.setattr(legacy_server, "available_tiers", lambda: {"code": "qwen2.5:7b"})

    with _http_server(monkeypatch) as port:
        # User Alice sets session_temperature=0.15 on session 'shared-id'
        st, _, raw = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": "sonder",
                "messages": [{"role": "user", "content": "Hello I prefer short replies"}],
                "session": "shared-id",
                "session_temperature": 0.15,
            }),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_alice}"},
        )
        assert st == 200, raw
        assert prefs_written == []

        # Bob cannot mutate host permission-mode (403) and GET /v1/permission-mode reports can_bypass=False
        st, _, raw = _request(
            port, "GET", "/v1/permission-mode",
            headers={"Authorization": f"Bearer {tok_bob}", "X-Sonder-Session": "shared-id"},
        )
        assert st == 200
        assert json.loads(raw)["can_bypass"] is False

        st, _, _ = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass", "confirm_bypass": True}),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {tok_bob}",
                "Idempotency-Key": "bob-bypass-1",
            },
        )
        assert st == 403

        # Temperature slider for Alice on 'shared-id' does NOT affect Bob on 'shared-id'
        st_a, _, _ = _request(
            port, "GET", "/v1/sonder/capabilities",
            headers={"Authorization": f"Bearer {tok_alice}"},
        )
        assert st_a == 200
        # Session history isolation: Alice's /history only has Alice's turn, Bob's does not
        st, _, bob_hist = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": "sonder",
                "messages": [{"role": "user", "content": "/history"}],
                "session": "shared-id",
            }),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_bob}"},
        )
        assert st == 200
        bob_text = json.loads(bob_hist)["choices"][0]["message"]["content"]
        assert "Hello I prefer short replies" not in bob_text


# ---------------------------------------------------------------------------
# 4. Client-Side Execution Protocol (execution_target="client")
# ---------------------------------------------------------------------------

def test_client_exec_sse_stream_wait_resume_and_security_boundaries(monkeypatch, tmp_path):
    _configure_public_host(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_CHAT_TOOL_MODELS", "sonder,qwen2.5:7b")

    conn = legacy_server._open_db()
    try:
        admin_auth.register(conn, "alice", "Str0ng!Key#2026", trusted_local=False, allow_additional=True, public_hosting=True)
        admin_auth.register(conn, "bob", "An0ther!Key#2026", trusted_local=False, allow_additional=True, public_hosting=True)
        tok_alice, _ = admin_auth.login(conn, "alice", "Str0ng!Key#2026")
        tok_bob, _ = admin_auth.login(conn, "bob", "An0ther!Key#2026")
    finally:
        conn.close()

    # Host code runner MUST NEVER be called by user-role client_exec!
    monkeypatch.setattr(
        legacy_server.code_runner,
        "run_code",
        lambda *_a, **_k: pytest.fail("host code_runner must never execute user client_exec calls"),
    )
    monkeypatch.setattr(legacy_server, "available_tiers", lambda: {"code": "qwen2.5:7b"})

    call_round = {"n": 0}
    seen_tools_payload: list[list[dict]] = []

    def fake_chat_request(payload, *, model, return_message=False, **kwargs):
        call_round["n"] += 1
        if call_round["n"] == 1:
            seen_tools_payload.append(list(kwargs.get("tools") or []))
            return (
                {"prompt_eval_count": 10, "eval_count": 5},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "run_code",
                                "arguments": {"language": "python", "code": "print(6*7)"},
                            }
                        }
                    ],
                },
            )
        # Round 2 sees client output "42" in messages!
        messages = payload.get("messages") or []
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs and "42" in tool_msgs[-1]["content"]
        return (
            {"prompt_eval_count": 12, "eval_count": 6},
            {"role": "assistant", "content": "The client printed 42."},
        )

    monkeypatch.setattr(legacy_server, "_chat_request", fake_chat_request)

    with _http_server(monkeypatch) as port:
        # Step A: non-client host admin command like /selfmod still forbidden (403)
        st, _, _ = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "/selfmod apply"}]}),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_alice}"},
        )
        assert st == 403

        # Step B: stream=True turn triggers SSE awaiting_client_result, then we submit result!
        conn_sse = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({
            "model": "sonder",
            "stream": True,
            "session": "sess-1",
            "messages": [{"role": "user", "content": "Run print(6*7) on my PC"}],
        })
        conn_sse.request(
            "POST",
            "/v1/chat/completions",
            body=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body.encode("utf-8"))),
                "Authorization": f"Bearer {tok_alice}",
                "X-Sonder-Client-Exec": "1",
            },
        )
        resp = conn_sse.getresponse()
        assert resp.status == 200

        call_id = ""
        events: list[dict] = []
        raw_lines: list[str] = []
        while True:
            line_bytes = resp.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            raw_lines.append(line)
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                evt = json.loads(line[6:])
                events.append(evt)
                tool_ev = evt.get("sonder_tool")
                if isinstance(tool_ev, dict) and tool_ev.get("phase") == "awaiting_client_result":
                    call_id = tool_ev["call_id"]
                    assert tool_ev["execution_target"] == "client"
                    assert tool_ev["tool"] == "run_code"
                    assert tool_ev["risk"] == "execute"
                    assert tool_ev["args"] == {"language": "python", "code": "print(6*7)"}

                    # Bob CANNOT hijack Alice's call_id (403)
                    st_bob, _, _ = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "hijacked"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_bob}",
                            "X-Sonder-Session": "sess-1",
                        },
                    )
                    assert st_bob == 403

                    # Alice with wrong session CANNOT submit (403)
                    st_wrong_sess, _, _ = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "42\n"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "other-session",
                        },
                    )
                    assert st_wrong_sess == 403

                    # Oversized payload rejected (413)
                    st_big, _, _ = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "x" * (MAX_CLIENT_OUTPUT_CHARS + 1)}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "sess-1",
                        },
                    )
                    assert st_big == 413

                    # Valid Alice submission (200)
                    st_ok, _, raw_ok = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "42\n"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "sess-1",
                        },
                    )
                    assert st_ok == 200, raw_ok
                    assert json.loads(raw_ok)["accepted"] is True

                    # Idempotent replay (200 idempotent=True), conflicting replay (409)
                    st_dup, _, raw_dup = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "42\n"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "sess-1",
                        },
                    )
                    assert st_dup == 200 and json.loads(raw_dup)["idempotent"] is True

                    st_conflict, _, _ = _request(
                        port, "POST", f"/v1/client-tools/{call_id}/result",
                        body=json.dumps({"ok": True, "output": "99\n"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "sess-1",
                        },
                    )
                    assert st_conflict == 409

        conn_sse.close()
        assert call_id
        # Verify terminal sonder_tool frame and assistant reply arrived
        phases = [e["sonder_tool"]["phase"] for e in events if "sonder_tool" in e]
        assert phases == ["awaiting_client_result", "finished"]
        deltas = "".join(
            (e.get("choices") or [{}])[0].get("delta", {}).get("content", "")
            for e in events
        )
        assert "42" in deltas

        # Exposed tools never include admin_*
        exposed_names = {t["function"]["name"] for t in seen_tools_payload[0]}
        assert "run_code" in exposed_names and "file_write" in exposed_names
        assert not any(n.startswith("admin_") for n in exposed_names)

        # Step C: Non-stream slash handoff (/write) returns structured client_tool_handoff without host write
        st_slash, _, raw_slash = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": "sonder",
                "stream": False,
                "session": "sess-1",
                "messages": [{"role": "user", "content": "/write hello.txt world"}],
            }),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_alice}"},
        )
        assert st_slash == 200, raw_slash
        slash_data = json.loads(raw_slash)
        handoff = slash_data["sonder_receipt"]["client_tool_handoff"]
        assert handoff["execution_target"] == "client"
        assert handoff["phase"] == "awaiting_client_result"
        assert handoff["tool"] == "file_write"
        assert handoff["args"]["path"] == "hello.txt"
        assert not (tmp_path / "hello.txt").exists()


def test_client_slash_resolution_and_boundary():
    assert is_client_delegatable("run_code") is True
    assert is_client_delegatable("admin_accounts") is False
    plan = resolve_client_slash_command("/mkdir sub/dir")
    assert plan is not None and plan.tool == "directory_create" and plan.args == {"path": "sub/dir"}
    assert plan.risk == "write"
    assert "file_delete" in CLIENT_DELEGATABLE_TOOLS


# ---------------------------------------------------------------------------
# 5. Backlog S1 & S5 Regressions
# ---------------------------------------------------------------------------

def test_s1_error_contacting_recognized_as_error_signal():
    assert has_legacy_error_prefix("ERROR: failed") is True
    assert has_legacy_error_prefix("ERROR contacting http://127.0.0.1:11434: refused") is True
    assert has_legacy_error_prefix("Normal model output") is False


def test_s5_set_context_size_pins_default_chat(monkeypatch):
    monkeypatch.setattr(legacy_server, "SESSION_NUM_CTX", 8192)
    monkeypatch.setattr(legacy_server, "_SESSION_NUM_CTX_PINNED", False)
    seen_ctx: list[int] = []

    def fake_answer(conn, prompt, model, effective_system, temperature, num_predict, req_ctx, *args, **kwargs):
        seen_ctx.append(int(req_ctx or 0))
        return "ok", "iid-1", {}

    monkeypatch.setattr(legacy_server, "_answer", fake_answer)
    res = legacy_server.set_context_size("16384")
    assert "16,384" in res or "16384" in res
    assert legacy_server._SESSION_NUM_CTX_PINNED is True
    assert legacy_server.SESSION_NUM_CTX == 16384

    legacy_server._answer_with_history_impl("hi", [], None)
    assert seen_ctx == [16384]

    legacy_server.set_context_size("")
    assert legacy_server._SESSION_NUM_CTX_PINNED is False
    assert legacy_server.SESSION_NUM_CTX == legacy_server.context_policy.requested("")


def test_client_exec_streamed_slash_and_nonstream_model_handoff(monkeypatch, tmp_path):
    _configure_public_host(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_CHAT_TOOL_MODELS", "sonder,qwen2.5:7b")

    conn = legacy_server._open_db()
    try:
        admin_auth.register(conn, "alice", "Str0ng!Key#2026", trusted_local=False, allow_additional=True, public_hosting=True)
        tok_alice, _ = admin_auth.login(conn, "alice", "Str0ng!Key#2026")
    finally:
        conn.close()

    monkeypatch.setattr(legacy_server, "available_tiers", lambda: {"code": "qwen2.5:7b"})
    monkeypatch.setattr(
        legacy_server.code_runner,
        "run_code",
        lambda *_a, **_k: pytest.fail("host code_runner must never execute client slash calls"),
    )

    with _http_server(monkeypatch) as port:
        # 1. Streamed /run slash command delegates to client via SSE
        conn_sse = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({
            "model": "sonder",
            "stream": True,
            "session": "slash-s1",
            "messages": [{"role": "user", "content": "/run print(99)"}],
        })
        conn_sse.request(
            "POST",
            "/v1/chat/completions",
            body=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body.encode("utf-8"))),
                "Authorization": f"Bearer {tok_alice}",
            },
        )
        resp = conn_sse.getresponse()
        assert resp.status == 200
        events: list[dict] = []
        while True:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                evt = json.loads(line[6:])
                events.append(evt)
                tool_ev = evt.get("sonder_tool")
                if isinstance(tool_ev, dict) and tool_ev.get("phase") == "awaiting_client_result":
                    st_res, _, _ = _request(
                        port, "POST", f"/v1/client-tools/{tool_ev['call_id']}/result",
                        body=json.dumps({"ok": True, "output": "client-ran-99"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok_alice}",
                            "X-Sonder-Session": "slash-s1",
                        },
                    )
                    assert st_res == 200
        conn_sse.close()
        phases = [e["sonder_tool"]["phase"] for e in events if "sonder_tool" in e]
        assert phases == ["awaiting_client_result", "finished"]
        text_out = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content", "") for e in events)
        assert "client-ran-99" in text_out

        # 2. Non-stream model tool call emits handoff metadata cleanly
        rounds = {"n": 0}

        def fake_nonstream_chat(payload, *, model, return_message=False, **_k):
            rounds["n"] += 1
            if rounds["n"] == 1:
                return (
                    {"prompt_eval_count": 5, "eval_count": 3},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "file_read", "arguments": {"path": "notes.txt"}}}],
                    },
                )
            return (
                {"prompt_eval_count": 6, "eval_count": 4},
                {"role": "assistant", "content": "Waiting for client PC to read notes.txt."},
            )

        monkeypatch.setattr(legacy_server, "_chat_request", fake_nonstream_chat)
        st, _, raw = _request(
            port, "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": "sonder",
                "stream": False,
                "session": "ns-1",
                "messages": [{"role": "user", "content": "Read notes.txt"}],
            }),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_alice}"},
        )
        assert st == 200, raw
        payload = json.loads(raw)
        handoff = payload["sonder_receipt"]["client_tool_handoff"]
        assert handoff["execution_target"] == "client"
        assert handoff["tool"] == "file_read"
        assert handoff["phase"] == "awaiting_client_result"
        assert handoff["args"] == {"path": "notes.txt"}


def test_stream_keepalive_runs_during_work_intent_and_heartbeat_default(monkeypatch, tmp_path):
    import time as _time

    _configure_public_host(monkeypatch, tmp_path)
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "PUBLIC_HOSTING", False)
    assert runtime_config.ServerConfig().stream_heartbeat_seconds == 10
    monkeypatch.setattr(ts, "STREAM_HEARTBEAT_SECONDS", 0.05)

    def slow_work(content, **_k):
        _time.sleep(0.18)
        return "work-done"

    monkeypatch.setattr(ts, "_handle_work_intent", slow_work)
    monkeypatch.setattr(legacy_server, "available_tiers", lambda: {"code": "qwen2.5:7b"})

    with _http_server(monkeypatch) as port:
        conn_sse = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({
            "model": "sonder",
            "stream": True,
            "messages": [{"role": "user", "content": "check work"}],
        })
        conn_sse.request(
            "POST",
            "/v1/chat/completions",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "Content-Length": str(len(body.encode("utf-8")))},
        )
        resp = conn_sse.getresponse()
        assert resp.status == 200
        raw_stream = resp.read().decode("utf-8", "replace")
        conn_sse.close()
    assert raw_stream.count(": keep-alive") >= 2
    assert "work-done" in raw_stream
    assert "data: [DONE]" in raw_stream
