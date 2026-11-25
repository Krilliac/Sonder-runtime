"""Loopback HTTP and executor hardening tests for the client companion (:11439)."""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from sonder_runtime.interfaces.client_agent import (
    ClientAgentPolicy,
    create_client_agent_server,
    execute_client_tool,
)
from sonder_runtime.interfaces.client_agent import executor as executor_mod
from sonder_runtime.interfaces.client_agent import server as server_mod
from sonder_runtime.interfaces.client_agent.server import (
    build_wsgi_app,
    is_allowed_host_header,
    normalize_origin,
)

TEST_TOKEN = "companion-hardening-secret-token-987654"


@contextmanager
def _companion(
    workspace: Path,
    *,
    mode: str = "auto",
    token: str = TEST_TOKEN,
    allowed_origins: tuple[str, ...] = ("https://ui.example.com",),
) -> Iterator[tuple[int, server_mod.ClientAgentServer]]:
    srv = create_client_agent_server(
        host="127.0.0.1",
        port=0,
        workspace_root=workspace,
        mode=mode,
        token=token,
        allowed_origins=allowed_origins,
    )
    port = int(srv.server_address[1])
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=3.0)


def _raw_http(port: int, request_bytes: bytes) -> tuple[int, dict[str, str], bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        try:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks: list[bytes] = []
        while True:
            try:
                piece = sock.recv(65_536)
            except ConnectionResetError:
                break
            if not piece:
                break
            chunks.append(piece)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_code = int(lines[0].split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status_code, headers, body


def _http_json(
    port: int,
    method: str,
    path: str,
    *,
    raw_body: bytes | None = None,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, object] | None]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
    try:
        hdrs = dict(headers or {})
        payload = raw_body if raw_body is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        if payload is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("Content-Length", str(len(payload)))
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        parsed = json.loads(data.decode("utf-8")) if data else None
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed
    finally:
        conn.close()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, 0, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32(0)
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_pid_dead(pid: int, timeout_s: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


# ---------------------------------------------------------------------------
# 1. Host header (DNS rebinding), Origin, & auth hardening
# ---------------------------------------------------------------------------


def test_host_header_validator_and_dns_rebinding_defense(tmp_path: Path) -> None:
    assert is_allowed_host_header("127.0.0.1") is True
    assert is_allowed_host_header("127.0.0.1:11439") is True
    assert is_allowed_host_header("localhost:11439") is True
    assert is_allowed_host_header("[::1]:11439") is True
    for bad_host in (
        "",
        "evil.attacker.example:11439",
        "0.0.0.0:11439",
        "127.0.0.1:evil",
        "127.0.0.1:99999",
        "127.0.0.1:80²",
        "127.0.0.1\r\nX-Test: 1",
        "user@127.0.0.1:11439",
        "127.0.0.1,evil.com",
    ):
        assert is_allowed_host_header(bad_host) is False, bad_host

    with _companion(tmp_path) as (port, _srv):
        # Spoofed Host blocked with 403 HOST_FORBIDDEN and zero CORS headers even on /health.
        st, hdrs, body = _raw_http(
            port,
            (
                b"GET /health HTTP/1.1\r\n"
                b"Host: rebind.attacker.invalid:11439\r\n"
                b"Origin: https://ui.example.com\r\n"
                b"Connection: close\r\n\r\n"
            ),
        )
        assert st == 403
        assert "access-control-allow-origin" not in hdrs
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["error"]["code"] == "HOST_FORBIDDEN"

        # Missing Host header on HTTP/1.1 is refused with 403 HOST_FORBIDDEN.
        st_missing, _, body_missing = _raw_http(
            port,
            b"GET /health HTTP/1.1\r\nConnection: close\r\n\r\n",
        )
        assert st_missing == 403
        assert json.loads(body_missing.decode("utf-8"))["error"]["code"] == "HOST_FORBIDDEN"

        # Duplicate Host header is refused with 400 INVALID_REQUEST.
        st_dup_host, _, body_dup_host = _raw_http(
            port,
            (
                f"GET /health HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Host: evil.invalid:{port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii"),
        )
        assert st_dup_host == 400
        assert json.loads(body_dup_host.decode("utf-8"))["error"]["code"] == "INVALID_REQUEST"

        # Duplicate Content-Length header is refused with 400 INVALID_REQUEST.
        st_dup_cl, _, body_dup_cl = _raw_http(
            port,
            (
                f"POST /v1/client-agent/mode HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"X-Sonder-Client-Agent-Token: {TEST_TOKEN}\r\n"
                f"Content-Length: 2\r\n"
                f"Content-Length: 4\r\n"
                f"Connection: close\r\n\r\n{{}}"
            ).encode("ascii"),
        )
        assert st_dup_cl == 400
        assert json.loads(body_dup_cl.decode("utf-8"))["error"]["code"] == "INVALID_REQUEST"


def test_origin_and_token_control_char_rejection(tmp_path: Path) -> None:
    assert normalize_origin("https://ui.example.com\x00") is None
    assert normalize_origin("https://ui.example.com\\evil") is None
    assert normalize_origin("https://ui.example.com%2f") is None
    assert normalize_origin("https://" + "a" * 600 + ".example.com") is None

    with pytest.raises(ValueError, match="control characters"):
        create_client_agent_server(workspace_root=tmp_path, port=0, token="bad\r\ntoken")

    # Control chars or oversized auth token rejected via WSGI cleanly as 401
    app = build_wsgi_app(policy=ClientAgentPolicy(workspace_root=tmp_path), token=TEST_TOKEN)
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    out = b"".join(
        app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/v1/client-agent/status",
                "HTTP_AUTHORIZATION": f"Bearer {TEST_TOKEN}\x00extra",
            },
            start_response,
        )
    )
    assert str(captured["status"]).startswith("401 ")
    assert json.loads(out.decode("utf-8"))["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 2. Request-line, header-count/bytes, JSON nesting/NaN/truncation bounds
# ---------------------------------------------------------------------------


def test_request_line_and_header_limits_return_sanitized_json(tmp_path: Path) -> None:
    with _companion(tmp_path) as (port, _srv):
        # Request line > 4096 bytes -> 414 URI_TOO_LONG JSON (never HTML)
        for length in (4500, 70_000):
            huge_path = "/" + ("a" * length)
            st_414, hdrs_414, body_414 = _raw_http(
                port,
                f"GET {huge_path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("ascii"),
            )
            assert st_414 == 414
            assert "application/json" in hdrs_414.get("content-type", "")
            assert json.loads(body_414.decode("utf-8"))["error"]["code"] == "URI_TOO_LONG"

        # > 64 headers -> 431 REQUEST_HEADER_FIELDS_TOO_LARGE JSON
        many_headers = "".join(f"X-Pad-{i}: v\r\n" for i in range(70))
        st_431a, _, body_431a = _raw_http(
            port,
            f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{many_headers}Connection: close\r\n\r\n".encode("ascii"),
        )
        assert st_431a == 431
        assert json.loads(body_431a.decode("utf-8"))["error"]["code"] == "REQUEST_HEADER_FIELDS_TOO_LARGE"

        # Total headers > 16 KiB -> 431 REQUEST_HEADER_FIELDS_TOO_LARGE JSON
        big_headers = "".join(f"X-Big-{i}: {'x' * 2000}\r\n" for i in range(10))
        st_431b, _, body_431b = _raw_http(
            port,
            f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{big_headers}Connection: close\r\n\r\n".encode("ascii"),
        )
        assert st_431b == 431
        assert json.loads(body_431b.decode("utf-8"))["error"]["code"] == "REQUEST_HEADER_FIELDS_TOO_LARGE"


def test_json_depth_recursion_nan_truncated_body_and_field_bounds(tmp_path: Path) -> None:
    auth = {"X-Sonder-Client-Agent-Token": TEST_TOKEN}
    with _companion(tmp_path) as (port, _srv):
        # 1. Extreme nesting triggering RecursionError -> clean 400 (never 500 traceback)
        deep_recursion = (b'{"a":' * 2000) + b"1" + (b"}" * 2000)
        st, _, data = _http_json(
            port, "POST", "/v1/client-agent/execute", raw_body=deep_recursion, headers=auth,
        )
        assert st == 400 and data is not None and data["error"]["code"] == "INVALID_REQUEST"

        # 2. Nesting depth 80 (> _MAX_JSON_DEPTH=64) -> clean 400
        deep_80 = (b'{"a":' * 80) + b"1" + (b"}" * 80)
        st, _, data = _http_json(
            port, "POST", "/v1/client-agent/execute", raw_body=deep_80, headers=auth,
        )
        assert st == 400 and data is not None and data["error"]["code"] == "INVALID_REQUEST"

        # 3. Non-standard NaN / Infinity JSON constants -> clean 400
        for bad_json in (b'{"tool":"file_read","args":{"offset":NaN}}', b'{"tool":"file_read","args":{"offset":Infinity}}'):
            st, _, data = _http_json(
                port, "POST", "/v1/client-agent/execute", raw_body=bad_json, headers=auth,
            )
            assert st == 400 and data is not None and data["error"]["code"] == "INVALID_REQUEST"

        # 4. Truncated body (Content-Length > bytes actually sent) -> clean 400
        st_trunc, _, body_trunc = _raw_http(
            port,
            (
                f"POST /v1/client-agent/mode HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"X-Sonder-Client-Agent-Token: {TEST_TOKEN}\r\n"
                f"Content-Length: 500\r\n"
                f"Connection: close\r\n\r\n"
                f'{{"mode":"plan"}}'
            ).encode("ascii"),
        )
        assert st_trunc == 400
        assert json.loads(body_trunc.decode("utf-8"))["error"]["code"] == "INVALID_REQUEST"

        # 5. Oversized call_id / non-bool approved / unknown per-call mode / non-string tool -> 400
        for payload in (
            {"tool": "file_read", "call_id": "c" * 300, "args": {"path": "a.txt"}},
            {"tool": ["not-a-string"], "args": {}},
            {"tool": "file_read", "approved": "yes", "args": {}},
            {"tool": "file_read", "mode": "superadmin", "args": {}},
        ):
            st, _, data = _http_json(
                port, "POST", "/v1/client-agent/execute", body=payload, headers=auth,
            )
            assert st == 400, (payload, st, data)


# ---------------------------------------------------------------------------
# 3. Root-path containment, file bounds, TOCTOU, & strict numeric parsing
# ---------------------------------------------------------------------------


def test_run_program_rejects_unc_and_nt_executable_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: list[list[str]] = []

    def fail_if_called(argv: list[str], **kwargs: object) -> tuple[int, str, str]:
        called.append(argv)
        raise AssertionError("subprocess must not be launched for unsafe executable spelling")

    monkeypatch.setattr(executor_mod, "_run_process_sync", fail_if_called)
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    for hostile in (
        r"\\attacker.invalid\share\evil.exe",
        r"//attacker.invalid/share/evil.exe",
        r"\??\C:\Windows\System32\cmd.exe",
        "CON",
    ):
        res = execute_client_tool("run_program", {"command": hostile}, policy=policy)
        assert res.ok is False, hostile
        assert called == []


def test_file_bounds_numeric_edge_cases_and_walk_depth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    (tmp_path / "doc.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    # Bool / NaN / Inf / float numeric args never raise OverflowError or 500
    for tool, args in (
        ("file_read", {"path": "doc.txt", "offset": True}),
        ("file_read", {"path": "doc.txt", "limit": 1.5}),
        ("file_read", {"path": "doc.txt", "offset": float("inf")}),
        ("directory_tree", {"path": ".", "depth": False}),
        ("run_code", {"code": "print(1)", "timeout": float("nan")}),
        ("run_program", {"command": "echo hi", "timeout": True}),
        ("test_run", {"timeout": "not-an-int"}),
        ("git_log", {"limit": True}),
    ):
        res = execute_client_tool(tool, args, policy=policy)
        assert res.ok is False and res.error, (tool, args, res)

    # file_write content cap & file_edit size cap
    monkeypatch.setattr(executor_mod, "_MAX_FILE_WRITE_CHARS", 128)
    res_big_write = execute_client_tool(
        "file_write", {"path": "big.txt", "content": "x" * 200}, policy=policy,
    )
    assert res_big_write.ok is False and "exceeds" in res_big_write.error

    monkeypatch.setattr(executor_mod, "_MAX_EDIT_FILE_BYTES", 32)
    (tmp_path / "large.txt").write_text("a" * 64, encoding="utf-8")
    res_big_edit = execute_client_tool(
        "file_edit", {"path": "large.txt", "old": "a", "new": "b"}, policy=policy,
    )
    assert res_big_edit.ok is False and "maximum editable size" in res_big_edit.error

    # file_delete refuses workspace root itself
    res_del_root = execute_client_tool("file_delete", {"path": "."}, policy=policy)
    assert res_del_root.ok is False and "directories" in res_del_root.error

    # text_search routes through _open_regular_file_no_follow (post-open TOCTOU safe)
    opened_via_safe_fd: list[Path] = []
    orig_open = executor_mod._open_regular_file_no_follow

    def spy_open(path: Path, root: Path, **kwargs: object) -> int:
        opened_via_safe_fd.append(path)
        return orig_open(path, root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(executor_mod, "_open_regular_file_no_follow", spy_open)
    search_res = execute_client_tool("text_search", {"query": "alpha"}, policy=policy)
    assert search_res.ok is True and "doc.txt" in search_res.output
    assert any(p.name == "doc.txt" for p in opened_via_safe_fd)

    # file_read handles a single overlong line cleanly without buffering unbounded data
    monkeypatch.setattr(executor_mod, "_MAX_FILE_READ_CHARS", 128)
    (tmp_path / "longline.txt").write_text("Z" * 500 + "\nsecond\n", encoding="utf-8")
    read_res = execute_client_tool("file_read", {"path": "longline.txt", "offset": 1, "limit": 2}, policy=policy)
    assert read_res.ok is True
    assert len(read_res.output) <= 128


# ---------------------------------------------------------------------------
# 4. Subprocess timeout, process-tree cleanup, bounded stdout, & env scrubbing
# ---------------------------------------------------------------------------


def test_subprocess_timeout_kills_grandchild_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        "tmp_file = pid_file.with_suffix('.tmp')\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "tmp_file.write_text(str(child.pid), encoding='utf-8')\n"
        "tmp_file.replace(pid_file)\n"
        "time.sleep(60)\n"
    )
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    started = time.monotonic()
    res = execute_client_tool(
        "run_code",
        {"language": "python", "code": parent_code, "timeout": 2},
        policy=policy,
    )
    elapsed = time.monotonic() - started
    assert res.ok is False
    assert "timed out" in res.error.lower()
    assert elapsed < 12.0
    assert pid_file.is_file(), "grandchild did not start before timeout"
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _wait_pid_dead(grandchild_pid, timeout_s=5.0), (
        f"grandchild PID {grandchild_pid} survived parent timeout!"
    )


def test_subprocess_exit_cleans_up_detached_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "orphan.pid"
    parent_code = (
        "import pathlib, subprocess, sys\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        "tmp_file = pid_file.with_suffix('.tmp')\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        "tmp_file.write_text(str(child.pid), encoding='utf-8')\n"
        "tmp_file.replace(pid_file)\n"
        "print('parent-done')\n"
    )
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    res = execute_client_tool(
        "run_code",
        {"language": "python", "code": parent_code, "timeout": 10},
        policy=policy,
    )
    assert res.ok is True and "parent-done" in res.output
    assert pid_file.is_file()
    orphan_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _wait_pid_dead(orphan_pid, timeout_s=5.0), (
        f"detached grandchild PID {orphan_pid} survived after parent exit!"
    )


def test_subprocess_bounds_stdout_flood_and_scrubs_sonder_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("SONDER_CLIENT_AGENT_TOKEN", "leak-me-token-999")
    monkeypatch.setenv("SONDER_API_KEY", "leak-me-apikey-888")
    monkeypatch.setenv("SONDER_NODE1_SECRET", "leak-me-secret-777")
    monkeypatch.setenv("SAFE_NORMAL_VAR", "visible-value")

    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")

    # 1. Subprocess must not inherit Sonder secret env vars.
    env_code = (
        "import json, os\n"
        "keys = ['SONDER_CLIENT_AGENT_TOKEN', 'SONDER_API_KEY', 'SONDER_NODE1_SECRET', 'SAFE_NORMAL_VAR']\n"
        "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
    )
    res_env = execute_client_tool("run_code", {"language": "python", "code": env_code}, policy=policy)
    assert res_env.ok is True
    seen = json.loads(res_env.output)
    assert seen["SONDER_CLIENT_AGENT_TOKEN"] is None
    assert seen["SONDER_API_KEY"] is None
    assert seen["SONDER_NODE1_SECRET"] is None
    assert seen["SAFE_NORMAL_VAR"] == "visible-value"

    # 2. 1 MiB stdout flood is bounded cleanly without OOM.
    res_flood = execute_client_tool(
        "run_code",
        {"language": "python", "code": "print('A' * (1024 * 1024))", "timeout": 10},
        policy=policy,
    )
    assert res_flood.ok is True
    assert len(res_flood.output) <= executor_mod.MAX_OUTPUT_CHARS
    assert "[truncated]" in res_flood.output


# ---------------------------------------------------------------------------
# 5. Sanitized errors: zero traceback & zero secret leakage
# ---------------------------------------------------------------------------


def test_sanitized_errors_never_leak_tracebacks_or_companion_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    auth = {"X-Sonder-Client-Agent-Token": TEST_TOKEN}

    # 1. Unexpected internal error inside executor -> generic sanitized tool error
    def _boom_exec(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"sensitive internal crash Bearer {TEST_TOKEN}")

    monkeypatch.setattr(executor_mod, "_exec_file_read", _boom_exec)
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")
    tool_res = execute_client_tool("file_read", {"path": "x.txt"}, policy=policy)
    assert tool_res.ok is False
    assert tool_res.error == "internal tool execution error"
    assert TEST_TOKEN not in tool_res.error
    assert "Traceback" not in tool_res.error
    assert "RuntimeError" not in tool_res.error

    # 2. Unexpected internal error inside WSGI handler -> HTTP 500 JSON with zero traceback/secret
    def _boom_tool(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"secret-crash {TEST_TOKEN}")

    monkeypatch.setattr(server_mod, "execute_client_tool", _boom_tool)
    with _companion(tmp_path) as (port, _srv):
        st_500, hdrs_500, data_500 = _http_json(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"tool": "file_read", "args": {"path": "x.txt"}},
            headers=auth,
        )
        assert st_500 == 500
        assert "application/json" in hdrs_500.get("content-type", "")
        assert data_500 == {
            "ok": False,
            "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"},
        }
        assert TEST_TOKEN not in json.dumps(data_500)

        # 3. 404 path reflection redacts token and bounds length
        st_404, _, data_404 = _http_json(
            port,
            "GET",
            f"/unknown/{TEST_TOKEN}/" + ("x" * 300),
            headers=auth,
        )
        assert st_404 == 404 and data_404 is not None
        msg_404 = str(data_404["error"]["message"])
        assert TEST_TOKEN not in msg_404
        assert len(msg_404) < 200

    # 4. Token echoed by a tool result is scrubbed before leaving the HTTP response
    monkeypatch.setattr(
        server_mod,
        "execute_client_tool",
        lambda *a, **kw: executor_mod.ClientToolExecutionResult(
            ok=False,
            output=f"saw {TEST_TOKEN}",
            error=f"failed with Bearer {TEST_TOKEN}",
        ),
    )
    with _companion(tmp_path) as (port, _srv):
        st_scrub, _, data_scrub = _http_json(
            port,
            "POST",
            "/v1/client-agent/execute",
            body={"tool": "file_read", "args": {"path": "x.txt"}},
            headers=auth,
        )
        assert st_scrub == 200 and data_scrub is not None
        assert TEST_TOKEN not in json.dumps(data_scrub)
        assert "[redacted]" in str(data_scrub["error"])
        assert "[redacted]" in str(data_scrub["output"])
