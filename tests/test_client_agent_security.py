"""Regression tests for client_agent companion & HTTP ClientToolBroker security fixes."""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from sonder_runtime.domain.client_tools import (
    CLIENT_DELEGATABLE_TOOL_DEFINITIONS,
    resolve_client_slash_command,
)
from sonder_runtime.interfaces.client_agent import (
    ClientAgentPolicy,
    create_client_agent_server,
    execute_client_tool,
)
from sonder_runtime.interfaces.client_agent import executor as executor_mod
from sonder_runtime.interfaces.client_agent.__main__ import main as cli_main
from sonder_runtime.interfaces.client_agent.executor import ModeCeilingRefused
from sonder_runtime.interfaces.client_agent.server import (
    _MAX_REQUEST_BODY_BYTES,
    build_wsgi_app,
    is_loopback_host,
    normalize_origin,
)
from sonder_runtime.interfaces.http.client_tools import (
    ClientToolArgsTooLargeError,
    ClientToolBroker,
    ClientToolBrokerCapacityError,
)


@contextmanager
def _companion(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    mode: str = "manual",
    max_mode: str | None = None,
    allow_bypass: bool = False,
    token: str = "tok-sec-1234",
):
    srv = create_client_agent_server(
        host=host,
        port=0,
        workspace_root=workspace,
        mode=mode,
        max_mode=max_mode,
        allow_bypass=allow_bypass,
        token=token,
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
    host: str = "127.0.0.1",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
):
    conn = http.client.HTTPConnection(host, port, timeout=10)
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


# ---------------------------------------------------------------------------
# 1. Unprompted credential / secret read blocking & git_diff redaction
# ---------------------------------------------------------------------------

_SECRET_READ_PATHS = [
    ".env",
    ".env.local",
    "prod.env",
    ".ssh/id_rsa",
    ".aws/credentials",
    "server.pem",
    "tls.key",
    "credentials.json",
    "db_secret.yaml",
    ".git/config",
    ".gitconfig",
    ".config/git/config",
    ".config/gh/hosts.yml",
    ".docker/config.json",
]


@pytest.mark.parametrize("rel_path", _SECRET_READ_PATHS)
@pytest.mark.parametrize("mode", ["plan", "manual", "acceptEdits", "auto", "bypass"])
def test_file_read_refuses_credential_and_git_config_paths(tmp_path: Path, rel_path: str, mode: str):
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("TOP_SECRET_TOKEN=shh\n", encoding="utf-8")

    policy = ClientAgentPolicy(workspace_root=tmp_path, mode=mode, can_bypass=(mode == "bypass"))
    res = execute_client_tool("file_read", {"path": rel_path}, policy=policy)

    assert res.ok is False
    assert res.denied is True
    assert "TOP_SECRET_TOKEN" not in res.output
    assert "TOP_SECRET_TOKEN" not in res.error


@pytest.mark.parametrize("rel_path", [".gitconfig", ".config/git/config"])
def test_file_write_refuses_gitconfig_paths(tmp_path: Path, rel_path: str):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="bypass", can_bypass=True)
    res = execute_client_tool("file_write", {"path": rel_path, "content": "[core]\n\tfsmonitor = evil\n"}, policy=policy)
    assert res.ok is False
    assert res.denied is True
    assert not (tmp_path / rel_path).exists()


def test_search_find_tree_skip_credential_files_and_dirs(tmp_path: Path):
    (tmp_path / "app.py").write_text("TOKEN_MARKER = 'visible'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN_MARKER=env_secret\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"TOKEN_MARKER": "cred_secret"}\n', encoding="utf-8")
    (tmp_path / ".gitconfig").write_text("[user]\n\tname = TOKEN_MARKER\n", encoding="utf-8")
    (tmp_path / ".config" / "git").mkdir(parents=True)
    (tmp_path / ".config" / "git" / "config").write_text("TOKEN_MARKER=gitconfig\n", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("TOKEN_MARKER_SSH\n", encoding="utf-8")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("TOKEN_MARKER_AWS\n", encoding="utf-8")

    for mode in ("manual", "bypass"):
        policy = ClientAgentPolicy(workspace_root=tmp_path, mode=mode, can_bypass=(mode == "bypass"))

        search_res = execute_client_tool("text_search", {"query": "TOKEN_MARKER"}, policy=policy)
        assert search_res.ok is True
        assert "app.py" in search_res.output
        for forbidden in (".env", "credentials.json", ".gitconfig", "id_rsa", ".aws", "env_secret", "cred_secret", "gitconfig"):
            assert forbidden not in search_res.output

        find_res = execute_client_tool("file_find", {"pattern": "*"}, policy=policy)
        assert find_res.ok is True
        assert "app.py" in find_res.output
        for forbidden in (".env", "credentials.json", ".gitconfig", "id_rsa", ".aws", ".ssh", ".config/git/config"):
            assert forbidden not in find_res.output

        tree_res = execute_client_tool("directory_tree", {"path": ".", "depth": 4}, policy=policy)
        assert tree_res.ok is True
        assert "app.py" in tree_res.output
        for forbidden in (".env", "credentials.json", ".gitconfig", ".ssh", ".aws", "id_rsa"):
            assert forbidden not in tree_res.output

        # Targeting a credential dir directly is refused.
        direct_ssh = execute_client_tool("file_find", {"root": ".ssh"}, policy=policy)
        assert direct_ssh.ok is False and direct_ssh.denied is True
        direct_tree = execute_client_tool("directory_tree", {"path": ".aws"}, policy=policy)
        assert direct_tree.ok is False and direct_tree.denied is True
        direct_search = execute_client_tool("text_search", {"root": ".ssh", "query": "TOKEN"}, policy=policy)
        assert direct_search.ok is False and direct_search.denied is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_diff_refuses_credential_path_and_filters_unfiltered_diff(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=ws, check=True, capture_output=True)
    (ws / "app.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True, capture_output=True)

    (ws / "app.py").write_text("x = 2\n", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=SUPER_SECRET_NEW\n", encoding="utf-8")

    policy = ClientAgentPolicy(workspace_root=ws, mode="manual")

    explicit = execute_client_tool("git_diff", {"path": ".env"}, policy=policy)
    assert explicit.ok is False
    assert explicit.denied is True
    assert "SUPER_SECRET_NEW" not in explicit.output

    unfiltered = execute_client_tool("git_diff", {}, policy=policy)
    assert unfiltered.ok is True
    assert "+x = 2" in unfiltered.output
    assert "SUPER_SECRET_NEW" not in unfiltered.output
    assert ".env" not in unfiltered.output


# ---------------------------------------------------------------------------
# 2. Prevent remote bypass escalation & plan-mode override
# ---------------------------------------------------------------------------

def test_policy_pins_max_mode_and_can_bypass(tmp_path: Path):
    pol = ClientAgentPolicy(workspace_root=tmp_path, mode="manual")
    assert pol.can_bypass is False
    assert pol.max_mode == "auto"
    with pytest.raises(ModeCeilingRefused):
        pol.set_mode("bypass", confirm_bypass=True)
    assert pol.set_mode("auto") == "auto"

    plan_pol = ClientAgentPolicy(workspace_root=tmp_path, mode="plan", max_mode="plan")
    assert plan_pol.can_bypass is False
    with pytest.raises(ModeCeilingRefused):
        plan_pol.set_mode("manual")

    with pytest.raises(ModeCeilingRefused):
        ClientAgentPolicy(workspace_root=tmp_path, mode="bypass", can_bypass=False)

    with pytest.raises(ValueError):
        ClientAgentPolicy(workspace_root=tmp_path, mode="auto", max_mode="manual")


def test_http_mode_escalation_blocked_without_confirm_bypass_and_max_mode(tmp_path: Path):
    auth = {"X-Sonder-Client-Agent-Token": "tok-sec-1234"}
    with _companion(tmp_path, mode="manual", allow_bypass=False) as (port, srv):
        st, _, status_data = _req(port, "GET", "/v1/client-agent/status", headers=auth)
        assert st == 200
        assert status_data["can_bypass"] is False
        assert status_data["max_mode"] == "auto"

        st_esc, _, err = _req(
            port,
            "POST",
            "/v1/client-agent/mode",
            body={"mode": "bypass", "confirm_bypass": True},
            headers=auth,
        )
        assert st_esc == 403
        assert err["error"]["code"] == "MODE_FORBIDDEN"
        assert srv.policy.mode == "manual"

    with _companion(tmp_path, mode="plan", max_mode="plan", allow_bypass=False) as (port, srv):
        st_up, _, err = _req(
            port,
            "POST",
            "/v1/client-agent/mode",
            body={"mode": "acceptEdits"},
            headers=auth,
        )
        assert st_up == 403
        assert err["error"]["code"] == "MODE_FORBIDDEN"
        assert srv.policy.mode == "plan"


def test_cli_max_mode_bypass_requires_confirm_bypass(capsys):
    rc = cli_main(["--max-mode", "bypass"])
    assert rc == 2
    assert "confirm-bypass" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("file_write", {"path": "x.txt", "content": "nope"}),
        ("file_edit", {"path": "x.txt", "old": "a", "new": "b"}),
        ("file_delete", {"path": "x.txt"}),
        ("directory_create", {"path": "sub"}),
        ("run_code", {"language": "python", "code": "print(1)"}),
        ("run_program", {"command": "echo hi"}),
        ("test_run", {}),
    ],
)
def test_plan_mode_denies_mutating_and_exec_tools_even_with_approved_true(
    tmp_path: Path, tool: str, args: dict,
):
    (tmp_path / "x.txt").write_text("a\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="plan")
    res = execute_client_tool(tool, args, policy=policy, approved=True)
    assert res.ok is False
    assert res.denied is True
    assert res.needs_approval is False
    assert "plan mode" in res.error

    # Also via HTTP when narrowing an auto companion to plan mode per call.
    auth = {"X-Sonder-Client-Agent-Token": "tok-sec-1234"}
    with _companion(tmp_path, mode="auto") as (port, _srv):
        st, _, payload = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"tool": tool, "args": args, "mode": "plan", "approved": True},
            headers=auth,
        )
        assert st == 200
        assert payload["ok"] is False
        assert payload["denied"] is True


# ---------------------------------------------------------------------------
# 3. Symlink/SMB leak elimination, multi-hop links, reserved names,
#    file_delete symlink unlink, GIT_CEILING_DIRECTORIES, bash -c
# ---------------------------------------------------------------------------

def test_scandir_walkers_never_call_path_is_dir_on_links_or_follow_unc(monkeypatch, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "good.txt").write_text("hello world\n", encoding="utf-8")
    fake_link = ws / "smb_link"
    fake_link.mkdir()

    monkeypatch.setattr(executor_mod, "_is_reparse_or_link", lambda p: Path(p) == fake_link)
    monkeypatch.setattr(
        os,
        "readlink",
        lambda p: r"\\?\UNC\attacker.invalid\share" if Path(p) == fake_link else "",
    )

    orig_is_dir = Path.is_dir
    orig_resolve = Path.resolve

    def guard_is_dir(self):
        if self == fake_link:
            pytest.fail("Path.is_dir() must never be called on symlink/junction entries")
        return orig_is_dir(self)

    def guard_resolve(self, *a, **k):
        if self == fake_link:
            pytest.fail("Path.resolve() must never be called on UNC symlink/junction entries")
        return orig_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "is_dir", guard_is_dir)
    monkeypatch.setattr(Path, "resolve", guard_resolve)

    for mode in ("manual", "bypass"):
        policy = ClientAgentPolicy(workspace_root=ws, mode=mode, can_bypass=(mode == "bypass"))
        tree = execute_client_tool("directory_tree", {"path": ".", "depth": 3}, policy=policy)
        find = execute_client_tool("file_find", {"root": ".", "pattern": "*"}, policy=policy)
        search = execute_client_tool("text_search", {"root": ".", "query": "hello"}, policy=policy)
        assert tree.ok is True and "good.txt" in tree.output and "smb_link" not in tree.output
        assert find.ok is True and "good.txt" in find.output and "smb_link" not in find.output
        assert search.ok is True and "good.txt" in search.output


def test_multihop_symlink_chain_to_unc_and_loop_refused_before_resolve(monkeypatch, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    l1 = ws / "link1"
    l2 = ws / "link2"
    l3 = ws / "link3"
    loop_a = ws / "loop_a"
    loop_b = ws / "loop_b"

    targets = {
        l1: "link2",
        l2: "link3",
        l3: r"\\attacker.invalid\share\secret.txt",
        loop_a: "loop_b",
        loop_b: "loop_a",
    }
    monkeypatch.setattr(executor_mod, "_is_reparse_or_link", lambda p: Path(p) in targets)
    monkeypatch.setattr(os, "readlink", lambda p: targets[Path(p)])

    resolve_calls: list[str] = []
    orig_resolve = Path.resolve

    def spy_resolve(self, *a, **k):
        resolve_calls.append(str(self))
        return orig_resolve(self, *a, **k)

    policy = ClientAgentPolicy(workspace_root=ws, mode="bypass", can_bypass=True)
    monkeypatch.setattr(Path, "resolve", spy_resolve)

    res_chain = execute_client_tool("file_read", {"path": "link1"}, policy=policy)
    assert res_chain.ok is False and "UNC" in res_chain.error
    assert resolve_calls == []

    res_loop = execute_client_tool("file_read", {"path": "loop_a"}, policy=policy)
    assert res_loop.ok is False and "too many symlink" in res_loop.error
    assert resolve_calls == []


@pytest.mark.parametrize(
    "bad_name",
    ["CON", "NUL", "AUX", "PRN", "COM1", "COM9", "LPT1", "LPT9", "sub/CON.txt", "NUL .log", "con:ads"],
)
def test_windows_reserved_device_names_rejected_before_resolve(monkeypatch, tmp_path: Path, bad_name: str):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="bypass", can_bypass=True)
    resolve_calls: list[str] = []
    orig_resolve = Path.resolve

    def spy_resolve(self, *a, **k):
        resolve_calls.append(str(self))
        return orig_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    res = execute_client_tool("file_write", {"path": bad_name, "content": "hi"}, policy=policy)
    assert res.ok is False
    assert "reserved" in res.error.lower()
    assert resolve_calls == []


def test_non_reserved_similar_names_remain_writable(tmp_path: Path):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="acceptEdits")
    for name in ("console.py", "null.txt", "auxiliary.md", "com10.txt"):
        res = execute_client_tool("file_write", {"path": name, "content": "ok\n"}, policy=policy)
        assert res.ok is True, (name, res.error)


def test_file_delete_unlinks_symlink_without_deleting_target(tmp_path: Path):
    target = tmp_path / "real.txt"
    target.write_text("keep me\n", encoding="utf-8")
    link = tmp_path / "alias.txt"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("os.symlink not permitted in this Windows environment")

    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="acceptEdits")
    res = execute_client_tool("file_delete", {"path": "alias.txt"}, policy=policy)
    assert res.ok is True, res.error
    assert not os.path.lexists(link)
    assert target.exists() and target.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_ceiling_directories_blocks_parent_repo_discovery(tmp_path: Path):
    parent_repo = tmp_path / "parent_repo"
    parent_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=parent_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=parent_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=parent_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=parent_repo, check=True, capture_output=True)
    (parent_repo / "root_secret.txt").write_text("parent commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=parent_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "parent-only-commit"], cwd=parent_repo, check=True, capture_output=True)

    child_ws = parent_repo / "sandbox_ws"
    child_ws.mkdir()

    policy = ClientAgentPolicy(workspace_root=child_ws, mode="manual")
    for tool in ("git_status", "git_log", "git_branch"):
        res = execute_client_tool(tool, {}, policy=policy)
        assert res.ok is False, f"{tool} should not discover parent repo: {res}"
        assert "parent-only-commit" not in res.output


def test_run_code_bash_uses_non_login_dash_c(monkeypatch, tmp_path: Path):
    seen_argv: list[list[str]] = []

    def fake_run(argv, *, cwd, timeout, stdin_text="", env=None):
        seen_argv.append(list(argv))
        return 0, "ok\n", ""

    monkeypatch.setattr(executor_mod, "_run_process_sync", fake_run)
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    res = execute_client_tool("run_code", {"language": "bash", "code": "echo ok"}, policy=policy)
    assert res.ok is True
    assert seen_argv == [["bash", "-c", "echo ok"]]


# ---------------------------------------------------------------------------
# 4. ClientToolBroker hardening
# ---------------------------------------------------------------------------

def test_broker_caps_pending_calls_total_calls_and_args_size():
    now = {"t": 1000.0}
    broker = ClientToolBroker(
        clock=lambda: now["t"],
        max_pending_calls=3,
        max_pending_per_principal=2,
        max_total_calls=4,
        max_args_bytes=512,
    )

    # Args size cap
    with pytest.raises(ClientToolArgsTooLargeError):
        broker.create_call(principal="alice", session="s1", tool="file_write", args={"content": "x" * 600})

    # Per-principal pending cap (2)
    c1 = broker.create_call(principal="alice", session="s1", tool="file_read", args={"path": "a"})
    c2 = broker.create_call(principal="alice", session="s1", tool="file_read", args={"path": "b"})
    with pytest.raises(ClientToolBrokerCapacityError) as exc_info:
        broker.create_call(principal="alice", session="s1", tool="file_read", args={"path": "c"})
    assert exc_info.value.status_code == 429

    # Global pending cap (3)
    c3 = broker.create_call(principal="bob", session="s2", tool="file_read", args={"path": "d"})
    with pytest.raises(ClientToolBrokerCapacityError):
        broker.create_call(principal="carol", session="s3", tool="file_read", args={"path": "e"})

    # Completing c1 frees a slot and keeps total records bounded.
    sub = broker.submit_result(c1.call_id, {"ok": True, "output": "done"}, principal="alice", session="s1")
    assert sub.status == 200
    c4 = broker.create_call(principal="carol", session="s3", tool="file_read", args={"path": "f"})
    assert c4.call_id in broker._calls
    assert len(broker._calls) <= 4
    # Expire old calls and verify submit_result prunes them
    now["t"] += 1000.0
    sub_expired = broker.submit_result(c2.call_id, {"ok": True, "output": "late"}, principal="alice", session="s1")
    assert sub_expired.status == 404
    assert broker._calls == {}
    _ = c3


def test_broker_shrinks_completed_payloads_while_preserving_idempotency():
    broker = ClientToolBroker()
    handoff = broker.create_call(
        principal="alice",
        session="s1",
        tool="file_read",
        args={"path": "big.txt", "extra": "y" * 200},
        mode="handoff",
    )
    big_out = "A" * 10_000
    res1 = broker.submit_result(handoff.call_id, {"ok": True, "output": big_out}, principal="alice", session="s1")
    assert res1.status == 200 and res1.body["idempotent"] is False
    # Stored record shrunk immediately for handoff mode
    assert handoff.result is not None and handoff.result.output == ""
    assert handoff.args == {}
    # Idempotency & conflict still work via retained digest
    res_dup = broker.submit_result(handoff.call_id, {"ok": True, "output": big_out}, principal="alice", session="s1")
    assert res_dup.status == 200 and res_dup.body["idempotent"] is True
    res_diff = broker.submit_result(handoff.call_id, {"ok": True, "output": "different"}, principal="alice", session="s1")
    assert res_diff.status == 409

    # Stream mode returns full output to wait_for_result, then shrinks stored output
    stream_call = broker.create_call(
        principal="alice",
        session="s1",
        tool="file_read",
        args={"path": "stream.txt"},
        mode="stream",
    )
    broker.submit_result(stream_call.call_id, {"ok": True, "output": big_out}, principal="alice", session="s1")
    outcome = broker.wait_for_result(stream_call.call_id, timeout_seconds=5.0)
    assert outcome.ok is True and outcome.output == big_out
    assert stream_call.result is not None and stream_call.result.output == ""
    # sse_event still works right after wait_for_result
    ev = stream_call.sse_event(outcome.status)
    assert ev["ok"] is True and ev["args"] == {"path": "stream.txt"}


def test_broker_wait_for_result_deadline_race_returns_accepted_result():
    now = {"t": 100.0}
    broker = ClientToolBroker(clock=lambda: now["t"])
    call = broker.create_call(
        principal="alice",
        session="s1",
        tool="run_code",
        args={"code": "print(1)"},
        timeout_seconds=1.0,
    )

    # Simulate submit_result completing right AFTER deadline expires (remaining <= 0)
    # and BEFORE wait_for_result acquires self._lock.
    clock_calls = {"n": 0}

    def racing_clock() -> float:
        clock_calls["n"] += 1
        if clock_calls["n"] == 1:
            return 100.0  # started
        if clock_calls["n"] == 2:
            # Advance past deadline AND deliver the result before the lock check runs.
            now["t"] = 105.0
            broker.submit_result(
                call.call_id,
                {"ok": True, "output": "race-winner"},
                principal="alice",
                session="s1",
            )
            return 105.0
        return now["t"]

    # Temporarily keep call unexpired for submit_result inside racing_clock by giving
    # submit_result time <= expires_at while wait_for_result's remaining <= 0.
    call.expires_at = 200.0
    broker._clock = racing_clock
    outcome = broker.wait_for_result(call.call_id, timeout_seconds=1.0)
    assert outcome.status == "finished"
    assert outcome.ok is True
    assert outcome.output == "race-winner"


def test_broker_wait_for_result_honors_is_aborted():
    broker = ClientToolBroker()
    call = broker.create_call(
        principal="alice",
        session="s1",
        tool="run_code",
        args={"code": "print(1)"},
        timeout_seconds=120.0,
    )
    outcome = broker.wait_for_result(
        call.call_id,
        timeout_seconds=120.0,
        heartbeat_interval=0.05,
        is_aborted=lambda: True,
    )
    assert outcome.status == "expired"
    assert outcome.ok is False
    assert "disconnected" in outcome.error
    assert call.status == "expired"


def test_broker_rejects_conflicting_body_session_vs_header():
    broker = ClientToolBroker()
    call = broker.create_call(principal="alice", session="sess-a", tool="file_read", args={"path": "a"})

    conflict = broker.submit_result(
        call.call_id,
        {"ok": True, "output": "ok", "session": "sess-b"},
        principal="alice",
        session="sess-a",
    )
    assert conflict.status == 400
    assert conflict.body["error"]["code"] == "INVALID_REQUEST"
    assert "conflicts" in conflict.body["error"]["message"]

    matching = broker.submit_result(
        call.call_id,
        {"ok": True, "output": "ok", "session": "sess-a"},
        principal="alice",
        session="sess-a",
    )
    assert matching.status == 200 and matching.body["accepted"] is True


# ---------------------------------------------------------------------------
# 5. Server & schema polish (413, 411, IPv6, origin/token hardening, schema)
# ---------------------------------------------------------------------------

def test_server_413_payload_too_large_and_411_chunked_without_content_length(tmp_path: Path):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="manual")
    app = build_wsgi_app(policy=policy, token="tok-sec-1234")

    def call_wsgi(environ: dict) -> tuple[str, dict]:
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        chunks = list(app(environ, start_response))
        raw = b"".join(chunks)
        return captured["status"], json.loads(raw.decode("utf-8"))

    # 413 PAYLOAD_TOO_LARGE
    st_413, body_413 = call_wsgi({
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/client-agent/execute",
        "HTTP_X_SONDER_CLIENT_AGENT_TOKEN": "tok-sec-1234",
        "CONTENT_LENGTH": str(_MAX_REQUEST_BODY_BYTES + 1),
        "wsgi.input": io.BytesIO(b""),
    })
    assert st_413.startswith("413 ")
    assert body_413["error"]["code"] == "PAYLOAD_TOO_LARGE"

    # 411 LENGTH_REQUIRED for chunked body without Content-Length
    st_411, body_411 = call_wsgi({
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/client-agent/mode",
        "HTTP_X_SONDER_CLIENT_AGENT_TOKEN": "tok-sec-1234",
        "HTTP_TRANSFER_ENCODING": "chunked",
        "wsgi.input": io.BytesIO(b"0\r\n\r\n"),
    })
    assert st_411.startswith("411 ")
    assert body_411["error"]["code"] == "LENGTH_REQUIRED"

    # Non-ASCII token returns 401 cleanly (never 500 TypeError)
    st_401, body_401 = call_wsgi({
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/v1/client-agent/status",
        "HTTP_X_SONDER_CLIENT_AGENT_TOKEN": "tökén-ñ",
    })
    assert st_401.startswith("401 ")
    assert body_401["error"]["code"] == "UNAUTHORIZED"


def test_origin_normalization_rejects_colon_spoof_and_unicode_digits():
    assert normalize_origin("http://127.0.0.1:evil.com:80") is None
    assert normalize_origin("http://127.0.0.1:80²") is None
    assert normalize_origin("http://[::1]:80²") is None
    assert is_loopback_host("127.0.0.1²") is False
    assert normalize_origin("https://app.example.com:443") == "https://app.example.com"
    assert normalize_origin("http://127.0.0.1:80") == "http://127.0.0.1"


def test_server_binds_ipv6_loopback(tmp_path: Path):
    if not socket.has_ipv6:
        pytest.skip("IPv6 not supported")
    try:
        with _companion(tmp_path, host="::1") as (port, srv):
            assert srv.address_family == socket.AF_INET6
            st, _, data = _req(port, "GET", "/health", host="::1")
            assert st == 200 and data["ok"] is True
        with _companion(tmp_path, host="[::1]") as (port, srv):
            assert srv.address_family == socket.AF_INET6
            st, _, data = _req(port, "GET", "/health", host="::1")
            assert st == 200 and data["ok"] is True
    except OSError:
        pytest.skip("IPv6 loopback bind unavailable on this host")


def test_client_tool_schema_excludes_unsupported_cpp_and_csharp():
    run_code_def = next(d for d in CLIENT_DELEGATABLE_TOOL_DEFINITIONS if d.name == "run_code")
    langs = run_code_def.parameters["properties"]["language"]["enum"]
    assert "cpp" not in langs
    assert "csharp" not in langs
    assert set(langs) == {"python", "js", "powershell", "bash"}
    assert "cpp" not in run_code_def.description.lower()
    assert "csharp" not in run_code_def.description.lower()
    plan = resolve_client_slash_command("/run ```cpp\nint main(){}\n```")
    assert plan is not None and plan.args["language"] == "python"


# ---------------------------------------------------------------------------
# 6. H-08: Companion single-use approval challenge bound to (call_id, tool, args_digest)
# ---------------------------------------------------------------------------

def test_h08_approval_challenge_binding_replay_tamper_and_ttl(tmp_path: Path):
    now = {"t": 1000.0}
    srv = create_client_agent_server(
        host="127.0.0.1",
        port=0,
        workspace_root=tmp_path,
        mode="manual",
        token="tok-h08",
        approval_ttl_seconds=300.0,
        clock=lambda: now["t"],
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    auth = {"X-Sonder-Client-Agent-Token": "tok-h08"}
    try:
        # 1. Bare approved=True without a companion token never executes.
        st, _, ch1 = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={
                "call_id": "cid-1",
                "tool": "file_write",
                "args": {"path": "note.txt", "content": "hello"},
                "approved": True,
            },
            headers=auth,
        )
        assert st == 200
        assert ch1["ok"] is False and ch1["needs_approval"] is True
        assert ch1["approval_token"] and len(ch1["args_digest"]) == 64
        assert not (tmp_path / "note.txt").exists()

        # 2. Tampering with args, tool, call_id, or args_digest fails closed and burns the token.
        for label, tampered_body in [
            (
                "args",
                {
                    "call_id": "cid-1",
                    "tool": "file_write",
                    "args": {"path": "note.txt", "content": "EVIL"},
                    "approved": True,
                    "approval_token": ch1["approval_token"],
                    "args_digest": ch1["args_digest"],
                },
            ),
        ]:
            st_t, _, res_t = _req(port, "POST", "/v1/client-agent/execute", body=tampered_body, headers=auth)
            assert st_t == 200, label
            assert res_t["ok"] is False and res_t["denied"] is True, (label, res_t)
            assert not (tmp_path / "note.txt").exists()

        # Token burned on tamper cannot be used even with the original args.
        st_burned, _, res_burned = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={
                "call_id": "cid-1",
                "tool": "file_write",
                "args": {"path": "note.txt", "content": "hello"},
                "approved": True,
                "approval_token": ch1["approval_token"],
                "args_digest": ch1["args_digest"],
            },
            headers=auth,
        )
        assert st_burned == 200 and res_burned["ok"] is False and res_burned["needs_approval"] is True
        assert not (tmp_path / "note.txt").exists()

        # Test call_id, tool, and args_digest tamper individually.
        for field_name, override in [
            ("call_id", {"call_id": "cid-other"}),
            ("tool", {"tool": "file_delete"}),
            ("args_digest", {"args_digest": "0" * 64}),
        ]:
            _, _, ch = _req(
                port,
                "POST",
                "/v1/client-agent/execute",
                body={"call_id": "cid-2", "tool": "file_write", "args": {"path": "t.txt", "content": "ok"}},
                headers=auth,
            )
            bad_req = {
                "call_id": "cid-2",
                "tool": "file_write",
                "args": {"path": "t.txt", "content": "ok"},
                "approved": True,
                "approval_token": ch["approval_token"],
                "args_digest": ch["args_digest"],
                **override,
            }
            _, _, res_bad = _req(port, "POST", "/v1/client-agent/execute", body=bad_req, headers=auth)
            assert res_bad["ok"] is False and res_bad["denied"] is True, (field_name, res_bad)
            assert not (tmp_path / "t.txt").exists()

        # 3. Canonical key-order match succeeds once, and single-use replay fails.
        _, _, ch_ok = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"call_id": "cid-3", "tool": "file_write", "args": {"path": "ok.txt", "content": "v1"}},
            headers=auth,
        )
        st_exec, _, res_exec = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={
                "call_id": "cid-3",
                "tool": "file_write",
                # Reordered keys produce the identical canonical SHA-256 digest.
                "args": {"content": "v1", "path": "ok.txt"},
                "approved": True,
                "approval_token": ch_ok["approval_token"],
                "args_digest": ch_ok["args_digest"],
            },
            headers=auth,
        )
        assert st_exec == 200 and res_exec["ok"] is True
        assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "v1"

        # Replay the same approval_token -> refused.
        (tmp_path / "ok.txt").write_text("modified", encoding="utf-8")
        st_rep, _, res_rep = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={
                "call_id": "cid-3",
                "tool": "file_write",
                "args": {"path": "ok.txt", "content": "v1"},
                "approved": True,
                "approval_token": ch_ok["approval_token"],
                "args_digest": ch_ok["args_digest"],
            },
            headers=auth,
        )
        assert st_rep == 200 and res_rep["ok"] is False and res_rep["needs_approval"] is True
        assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "modified"

        # 4. Expired challenge fails closed after TTL.
        _, _, ch_exp = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"call_id": "cid-4", "tool": "file_write", "args": {"path": "late.txt", "content": "x"}},
            headers=auth,
        )
        now["t"] += 301.0
        st_late, _, res_late = _req(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={
                "call_id": "cid-4",
                "tool": "file_write",
                "args": {"path": "late.txt", "content": "x"},
                "approved": True,
                "approval_token": ch_exp["approval_token"],
                "args_digest": ch_exp["args_digest"],
            },
            headers=auth,
        )
        assert st_late == 200 and res_late["ok"] is False and res_late["needs_approval"] is True
        assert not (tmp_path / "late.txt").exists()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 7. M-07: Symlink / junction traversal & post-open TOCTOU hardening
# ---------------------------------------------------------------------------

def test_m07_intra_workspace_junction_and_symlink_refused_for_read_write_edit(tmp_path: Path):
    ws = tmp_path / "ws"
    real_dir = ws / "real"
    real_dir.mkdir(parents=True)
    target_file = real_dir / "data.txt"
    target_file.write_text("original\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    link_dir = ws / "link_dir"
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_dir), str(real_dir)],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip("NTFS junction creation unavailable")
    else:
        os.symlink(real_dir, link_dir)

    for tool, args in [
        ("file_read", {"path": "link_dir/data.txt"}),
        ("file_write", {"path": "link_dir/data.txt", "content": "pwned"}),
        ("file_edit", {"path": "link_dir/data.txt", "old": "original", "new": "pwned"}),
        ("directory_create", {"path": "link_dir/new_sub"}),
    ]:
        res = execute_client_tool(tool, args, policy=policy)
        assert res.ok is False, (tool, res)
        assert "symlink" in res.error.lower() or "junction" in res.error.lower(), (tool, res.error)
    assert target_file.read_text(encoding="utf-8") == "original\n"
    assert not (real_dir / "new_sub").exists()


def test_m07_post_open_toctou_escape_aborts_before_truncation(monkeypatch, tmp_path: Path):
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    victim = ws / "doc.txt"
    victim.write_text("keep-intact\n", encoding="utf-8")
    outside_target = outside / "escaped.txt"
    outside_target.write_text("outside-secret\n", encoding="utf-8")

    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    if os.name == "nt":
        # Simulate a post-open handle whose kernel final path escaped the workspace.
        monkeypatch.setattr(executor_mod, "_windows_final_path_for_fd", lambda _fd: outside_target)
    else:
        # Simulate a post-open inode swap detected by samestat.
        monkeypatch.setattr(os.path, "samestat", lambda _a, _b: False)

    res_write = execute_client_tool("file_write", {"path": "doc.txt", "content": "OVERWRITTEN"}, policy=policy)
    assert res_write.ok is False
    # Overwrite must not truncate before post-open verification passes.
    assert victim.read_text(encoding="utf-8") == "keep-intact\n"

    res_read = execute_client_tool("file_read", {"path": "doc.txt"}, policy=policy)
    assert res_read.ok is False
    assert "keep-intact" not in res_read.output

    res_edit = execute_client_tool("file_edit", {"path": "doc.txt", "old": "keep", "new": "hack"}, policy=policy)
    assert res_edit.ok is False
    assert victim.read_text(encoding="utf-8") == "keep-intact\n"


# ---------------------------------------------------------------------------
# 8. M-08: Companion CLI redacts bearer token on stdout unless --print-token
# ---------------------------------------------------------------------------

def test_m08_cli_redacts_token_by_default_and_prints_only_with_flag(monkeypatch, tmp_path: Path, capsys):
    from sonder_runtime.interfaces.client_agent import __main__ as cli_mod

    class _FakeServer:
        def __init__(self, *, token: str, workspace_root: Path):
            self.token = token
            self.policy = ClientAgentPolicy(workspace_root=workspace_root, mode="manual")
            self.allowed_origins = frozenset()
            self.server_address = ("127.0.0.1", 11439)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            return

    raw_secret = "super-secret-companion-bearer-9999"

    def _fake_create(**kwargs):
        return _FakeServer(token=kwargs.get("token") or raw_secret, workspace_root=kwargs["workspace_root"])

    monkeypatch.setattr(cli_mod, "create_client_agent_server", _fake_create)

    rc_default = cli_main(["--workspace", str(tmp_path), "--token", raw_secret])
    out_default = capsys.readouterr().out
    assert rc_default == 0
    assert raw_secret not in out_default
    assert "[redacted]" in out_default
    assert "--print-token" in out_default

    rc_explicit = cli_main(["--workspace", str(tmp_path), "--token", raw_secret, "--print-token"])
    out_explicit = capsys.readouterr().out
    assert rc_explicit == 0
    assert raw_secret in out_explicit
