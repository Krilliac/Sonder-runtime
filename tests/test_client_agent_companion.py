"""Tests for the local client companion (`sonder_runtime.interfaces.client_agent`)."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from sonder_runtime.interfaces.client_agent import (
    CLIENT_DELEGATABLE_TOOLS,
    ClientAgentPolicy,
    create_client_agent_server,
    execute_client_tool,
)
from sonder_runtime.interfaces.client_agent.__main__ import main as cli_main
from sonder_runtime.interfaces.client_agent.server import normalize_origin


@contextmanager
def _companion_server(
    workspace: Path,
    *,
    mode: str = "manual",
    max_mode: str | None = None,
    allow_bypass: bool = False,
    token: str = "secret-token-1234",
    allowed_origins: tuple[str, ...] = ("https://app.example.com",),
):
    srv = create_client_agent_server(
        host="127.0.0.1",
        port=0,
        workspace_root=workspace,
        mode=mode,
        max_mode=max_mode,
        allow_bypass=allow_bypass,
        token=token,
        allowed_origins=allowed_origins,
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _req(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        hdrs = dict(headers or {})
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        if raw is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("Content-Length", str(len(raw)))
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.headers), json.loads(data.decode("utf-8")) if data else None
    finally:
        conn.close()


def test_loopback_bind_and_origin_normalization():
    with pytest.raises(ValueError, match="loopback"):
        create_client_agent_server(host="0.0.0.0", port=0)

    assert normalize_origin("https://App.Example.com/") == "https://app.example.com"
    assert normalize_origin("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert normalize_origin("null") is None
    assert normalize_origin("https://evil.com/path") is None
    assert normalize_origin("ftp://127.0.0.1") is None


def test_http_auth_origin_and_cors_preflight(tmp_path: Path):
    with _companion_server(tmp_path) as (port, _srv):
        # /health allows loopback & configured origin; blocks unknown origins
        st, hdrs, data = _req(
            port,
            "GET",
            "/health",
            headers={"Origin": "https://app.example.com"},
        )
        assert st == 200
        assert data["ok"] is True
        assert hdrs.get("Access-Control-Allow-Origin") == "https://app.example.com"
        assert hdrs.get("Access-Control-Allow-Private-Network") == "true"

        st_evil, _, _ = _req(
            port,
            "GET",
            "/health",
            headers={"Origin": "https://evil.attacker"},
        )
        assert st_evil == 403

        st_null, _, _ = _req(port, "GET", "/health", headers={"Origin": "null"})
        assert st_null == 403

        # CORS preflight
        st_opt, opt_hdrs, _ = _req(
            port,
            "OPTIONS",
            "/v1/client-agent/execute",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert st_opt == 204
        assert opt_hdrs.get("Access-Control-Allow-Origin") == "https://app.example.com"

        # Protected endpoints reject missing or wrong token
        st_noauth, _, _ = _req(port, "GET", "/v1/client-agent/status")
        assert st_noauth == 401

        st_badauth, _, _ = _req(
            port,
            "GET",
            "/v1/client-agent/status",
            headers={"X-Sonder-Client-Agent-Token": "wrong"},
        )
        assert st_badauth == 401

        st_ok, _, status_body = _req(
            port,
            "GET",
            "/v1/client-agent/status",
            headers={"X-Sonder-Client-Agent-Token": "secret-token-1234"},
        )
        assert st_ok == 200
        assert status_body["mode"] == "manual"
        assert set(status_body["tools"]) == set(CLIENT_DELEGATABLE_TOOLS)


def test_mode_enforcement_and_bypass_confirmation(tmp_path: Path):
    with _companion_server(tmp_path, mode="manual") as (port, srv):
        auth = {"X-Sonder-Client-Agent-Token": "secret-token-1234"}

        # Write in manual mode without approved=True returns needs_approval=True
        st, _, res = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"tool": "file_write", "args": {"path": "a.txt", "content": "hi"}},
            headers=auth,
        )
        assert st == 200
        assert res["ok"] is False
        assert res["needs_approval"] is True
        assert not (tmp_path / "a.txt").exists()

        # Approved once executes it
        st, _, res = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"tool": "file_write", "args": {"path": "a.txt", "content": "hi"}, "approved": True},
            headers=auth,
        )
        assert st == 200
        assert res["ok"] is True
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"

        # Default companion (allow_bypass=False) rejects remote escalation to bypass with 403
        st, _, err = _req(
            port,
            "POST",
            "/v1/client-agent/mode",
            body={"mode": "bypass", "confirm_bypass": True},
            headers=auth,
        )
        assert st == 403
        assert err["error"]["code"] == "MODE_FORBIDDEN"
        assert srv.policy.mode == "manual"

    # When started with allow_bypass=True (--confirm-bypass), switching to bypass
    # still requires confirm_bypass: true in the POST body.
    with _companion_server(tmp_path, mode="manual", allow_bypass=True) as (port, srv):
        auth = {"X-Sonder-Client-Agent-Token": "secret-token-1234"}

        st, _, err = _req(
            port,
            "POST",
            "/v1/client-agent/mode",
            body={"mode": "bypass"},
            headers=auth,
        )
        assert st == 400
        assert err["error"]["code"] == "BYPASS_CONFIRMATION_REQUIRED"
        assert srv.policy.mode == "manual"

        st, _, ok_mode = _req(
            port,
            "POST",
            "/v1/client-agent/mode",
            body={"mode": "bypass", "confirm_bypass": True},
            headers=auth,
        )
        assert st == 200
        assert ok_mode["mode"] == "bypass"
        assert ok_mode["can_bypass"] is True


def test_workspace_root_containment_vs_bypass(tmp_path: Path):
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("top-secret", encoding="utf-8")

    # auto mode runs without prompt, but still blocks escaping workspace root
    auto_policy = ClientAgentPolicy(workspace_root=workspace, mode="auto")
    res_blocked = execute_client_tool(
        "file_read",
        {"path": str(outside)},
        policy=auto_policy,
    )
    assert res_blocked.ok is False
    assert "escapes workspace root" in res_blocked.error

    # bypass mode allows reading outside workspace root
    bypass_policy = ClientAgentPolicy(workspace_root=workspace, mode="bypass")
    res_allowed = execute_client_tool(
        "file_read",
        {"path": str(outside)},
        policy=bypass_policy,
    )
    assert res_allowed.ok is True
    assert "top-secret" in res_allowed.output


def test_execute_all_client_tools_end_to_end(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True, capture_output=True)

    policy = ClientAgentPolicy(workspace_root=ws, mode="auto")

    assert execute_client_tool("directory_create", {"path": "sub"}, policy=policy).ok
    assert execute_client_tool(
        "file_write",
        {"path": "sub/hello.py", "content": "def test_ok():\n    assert 2 + 2 == 4\n"},
        policy=policy,
    ).ok
    assert execute_client_tool(
        "file_edit",
        {"path": "sub/hello.py", "old": "2 + 2 == 4", "new": "3 + 3 == 6"},
        policy=policy,
    ).ok

    read_res = execute_client_tool("file_read", {"path": "sub/hello.py"}, policy=policy)
    assert read_res.ok and "3 + 3 == 6" in read_res.output

    find_res = execute_client_tool("file_find", {"pattern": "*.py"}, policy=policy)
    assert find_res.ok and "sub/hello.py" in find_res.output

    tree_res = execute_client_tool("directory_tree", {"path": ".", "depth": 2}, policy=policy)
    assert tree_res.ok and "sub/" in tree_res.output and "hello.py" in tree_res.output

    search_res = execute_client_tool("text_search", {"query": "3 + 3"}, policy=policy)
    assert search_res.ok and "sub/hello.py" in search_res.output

    code_res = execute_client_tool(
        "run_code",
        {"language": "python", "code": "print(6 * 7)"},
        policy=policy,
    )
    assert code_res.ok and "42" in code_res.output

    prog_res = execute_client_tool(
        "run_program",
        {"command": f'"{sys.executable}" -c "print(123)"'},
        policy=policy,
    )
    assert prog_res.ok and "123" in prog_res.output

    test_res = execute_client_tool("test_run", {"path": "sub/hello.py"}, policy=policy)
    assert test_res.ok and "1 passed" in test_res.output

    # Git tools
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)
    assert execute_client_tool("git_status", {}, policy=policy).ok
    assert execute_client_tool("git_log", {"limit": 5}, policy=policy).ok
    assert execute_client_tool("git_branch", {}, policy=policy).ok
    assert execute_client_tool(
        "file_write",
        {"path": "sub/hello.py", "content": "changed\n"},
        policy=policy,
    ).ok
    diff_res = execute_client_tool("git_diff", {}, policy=policy)
    assert diff_res.ok and "changed" in diff_res.output

    # Delete
    assert execute_client_tool("file_delete", {"path": "sub/hello.py"}, policy=policy).ok
    assert not (ws / "sub/hello.py").exists()

    # Non-client-delegatable tool refused
    admin_res = execute_client_tool("admin_accounts", {}, policy=policy)
    assert admin_res.ok is False and admin_res.denied is True


def test_cli_requires_confirm_bypass(capsys):
    rc = cli_main(["--mode", "bypass"])
    assert rc == 2
    assert "confirm-bypass" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows NTFS junction test")
def test_p1_find_search_tree_do_not_escape_windows_junctions(tmp_path):
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    aws_dir = tmp_path / ".aws"
    ws.mkdir()
    outside.mkdir()
    aws_dir.mkdir()
    (ws / "inside.txt").write_text("SAFE_LOCAL\n", encoding="utf-8")
    (outside / "secret.txt").write_text("TOPSECRET_OUTSIDE\n", encoding="utf-8")
    (aws_dir / "credentials").write_text("aws_secret_access_key=FAKE\n", encoding="utf-8")

    j_out = ws / "j_out"
    j_aws = ws / "j_aws"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(j_out), str(outside)], check=True, capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(j_aws), str(aws_dir)], check=True, capture_output=True)

    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    read_res = execute_client_tool("file_read", {"path": "j_out/secret.txt"}, policy=policy)
    assert read_res.ok is False and "escapes workspace root" in read_res.error

    find_res = execute_client_tool("file_find", {"root": ".", "pattern": "*.txt"}, policy=policy)
    assert find_res.ok is True
    assert "inside.txt" in find_res.output
    assert "secret.txt" not in find_res.output
    assert "j_out" not in find_res.output

    search_res = execute_client_tool("text_search", {"root": ".", "query": "TOPSECRET_OUTSIDE"}, policy=policy)
    assert search_res.ok is True
    assert "TOPSECRET_OUTSIDE" not in search_res.output
    assert "secret.txt" not in search_res.output

    tree_res = execute_client_tool("directory_tree", {"path": ".", "depth": 3}, policy=policy)
    assert tree_res.ok is True
    assert "inside.txt" in tree_res.output
    assert "secret.txt" not in tree_res.output
    assert "credentials" not in tree_res.output


def test_p2_unc_device_nt_and_drive_relative_refused_before_path_resolve(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    hostile_paths = [
        r"\\nohost.invalid\share\secret.txt",
        "//nohost.invalid/share/secret.txt",
        r"\\.\C:\Windows\win.ini",
        r"\\?\UNC\nohost.invalid\share\a.txt",
        r"\??\C:\Windows\win.ini",
        r"\??",
        "/??",
        "/??/C:/Windows/win.ini",
        "C:relative.txt",
        "C:",
        "ok.txt\x00evil",
        "bad\npath.txt",
    ]
    resolve_touches: list[str] = []
    orig_resolve = Path.resolve

    def spy_resolve(self, *a, **k):
        resolve_touches.append(str(self))
        return orig_resolve(self, *a, **k)

    # Policy constructor must also refuse hostile roots BEFORE Path.resolve().
    monkeypatch.setattr(Path, "resolve", spy_resolve)
    for bad in hostile_paths:
        resolve_touches.clear()
        with pytest.raises(PermissionError):
            ClientAgentPolicy(workspace_root=bad, mode="manual")
        assert resolve_touches == [], f"ClientAgentPolicy touched resolve() on {bad!r}: {resolve_touches}"

    monkeypatch.setattr(Path, "resolve", orig_resolve)
    policies = [
        ClientAgentPolicy(workspace_root=ws, mode="manual"),
        ClientAgentPolicy(workspace_root=ws, mode="acceptEdits"),
        ClientAgentPolicy(workspace_root=ws, mode="bypass"),
    ]
    monkeypatch.setattr(Path, "resolve", spy_resolve)
    for pol in policies:
        for bad in hostile_paths:
            resolve_touches.clear()
            res = execute_client_tool("file_read", {"path": bad}, policy=pol)
            assert res.ok is False and res.error, f"mode={pol.mode} path={bad!r}: {res}"
            assert resolve_touches == [], f"mode={pol.mode} touched resolve() on {bad!r}: {resolve_touches}"

    # Simulated reparse-point ancestor pointing to SMB/UNC must also refuse BEFORE Path.resolve().
    from sonder_runtime.interfaces.client_agent import executor as _exec_mod

    fake_link = ws / "j_smb"
    monkeypatch.setattr(
        _exec_mod,
        "_is_reparse_or_link",
        lambda p: Path(p) == fake_link,
    )
    monkeypatch.setattr(
        os,
        "readlink",
        lambda p: r"\\?\UNC\nohost.invalid\share" if Path(p) == fake_link else "",
    )
    for pol in policies:
        resolve_touches.clear()
        res = execute_client_tool("file_read", {"path": "j_smb/secret.txt"}, policy=pol)
        assert res.ok is False and res.error
        assert resolve_touches == [], f"mode={pol.mode} touched resolve() on UNC junction: {resolve_touches}"
