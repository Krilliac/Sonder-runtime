"""PowerShell mirror of the RPC lockstep and private-link policy (rpc-policy.ps1 and launchers)."""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "rpc"
POLICY = SCRIPTS / "rpc-policy.ps1"
_SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]
EXPECTED_HELLO = bytes([14]) + (24).to_bytes(8, "little") + bytes(24)

pytestmark = pytest.mark.skipif(not _SHELLS, reason="PowerShell is not available")


@pytest.fixture(params=_SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _run(shell: str, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "probe.ps1"
    script.write_text(f". '{POLICY}'\n$ErrorActionPreference = 'Stop'\n{body}\n", encoding="utf-8-sig")
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, timeout=120, check=False,
    )


def _run_script(shell: str, tmp_path: Path, name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(tmp_path / "localappdata")
    for key in ("SONDER_RPC_PEERS", "SONDER_RPC_PORT", "SONDER_RPC_LINK_CIDR", "SONDER_RPC_PROTO_MAJOR",
                "SONDER_RPC_WORKER_ADDRESS", "SONDER_RPC_ALLOWED_PEER", "SONDER_RPC_INSTALL_DIR"):
        env.pop(key, None)
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(SCRIPTS / name), *args],
        capture_output=True, text=True, timeout=120, check=False, env=env,
    )


@contextmanager
def _fake_rpc_servers(replies: list[bytes | None]) -> Iterator[tuple[list[int], list[bytes]]]:
    """One loopback listener per reply; ``None`` closes the socket without replying."""
    servers = [socket.create_server(("127.0.0.1", 0)) for _ in replies]
    received: list[bytes] = []
    threads: list[threading.Thread] = []

    def serve(server: socket.socket, reply: bytes | None) -> None:
        server.settimeout(30)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            data = b""
            while len(data) < len(EXPECTED_HELLO):
                chunk = conn.recv(64)
                if not chunk:
                    break
                data += chunk
            received.append(data)
            if reply is not None:
                conn.sendall(reply)

    for server, reply in zip(servers, replies, strict=True):
        thread = threading.Thread(target=serve, args=(server, reply), daemon=True)
        thread.start()
        threads.append(thread)
    try:
        yield [s.getsockname()[1] for s in servers], received
    finally:
        for server in servers:
            server.close()
        for thread in threads:
            thread.join(timeout=5)


def _reply(major: int, minor: int = 0, patch: int = 0) -> bytes:
    return struct.pack("<Q", 28) + bytes([major, minor, patch, 0]) + bytes(24)


def _peers_literal(ports: list[int]) -> str:
    items = ",".join(
        f"[pscustomobject]@{{Host='127.0.0.1'; Port={p}; Address='127.0.0.1:{p}'}}" for p in ports
    )
    return f"@({items})"


def test_ps_hello_reads_the_protocol_version_over_tcp(shell: str, tmp_path: Path) -> None:
    with _fake_rpc_servers([_reply(10, 0, 2)]) as (ports, received):
        result = _run(shell, tmp_path, f"(Get-RpcProtocolVersion '127.0.0.1' {ports[0]} 5000).Text")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10.0.2"
    assert received == [EXPECTED_HELLO]


def test_ps_lockstep_accepts_matching_workers(shell: str, tmp_path: Path) -> None:
    with _fake_rpc_servers([_reply(10, 0, 2), _reply(10)]) as (ports, _):
        body = (
            f"$r = Assert-RpcProtocolLockstep -Peers {_peers_literal(ports)} -ExpectedMajor 10 -TimeoutMs 5000\n"
            "($r | ForEach-Object { $_.rpcProtocol }) -join ','"
        )
        result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "10.0.2,10.0.0"


def test_ps_lockstep_refuses_a_pinned_major_mismatch(shell: str, tmp_path: Path) -> None:
    with _fake_rpc_servers([_reply(7)]) as (ports, _):
        body = f"Assert-RpcProtocolLockstep -Peers {_peers_literal(ports)} -ExpectedMajor 10 -TimeoutMs 5000"
        result = _run(shell, tmp_path, body)
    assert result.returncode != 0
    assert "Cross-host RPC split refused" in result.stderr
    assert "7.0.0" in result.stderr and "SONDER_RPC_PROTO_MAJOR=10" in result.stderr


def test_ps_lockstep_refuses_disagreeing_or_silent_workers(shell: str, tmp_path: Path) -> None:
    with _fake_rpc_servers([_reply(10), _reply(7)]) as (ports, _):
        result = _run(shell, tmp_path, f"Assert-RpcProtocolLockstep -Peers {_peers_literal(ports)} -TimeoutMs 5000")
    assert result.returncode != 0
    assert "between workers" in result.stderr
    with _fake_rpc_servers([None]) as (ports, _):
        result = _run(shell, tmp_path, f"Assert-RpcProtocolLockstep -Peers {_peers_literal(ports)} -TimeoutMs 5000")
    assert result.returncode != 0
    assert "did not answer the RPC HELLO" in result.stderr


def test_ps_link_network_matches_the_python_policy(shell: str, tmp_path: Path) -> None:
    body = r"""
$link = ConvertTo-RpcLinkNetwork '10.77.0.0/29'
$link.Text
Test-RpcAddressOnLink '10.77.0.2' $link
Test-RpcAddressOnLink '10.77.0.8' $link
Test-RpcAddressOnLink '192.168.1.5' $null
$null -eq (ConvertTo-RpcLinkNetwork '')
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["10.77.0.0/29", "True", "False", "True", "True"]


@pytest.mark.parametrize("cidr", ["10.77.0.1/30", "8.8.8.0/24", "10.0.0.0/7", "172.16.0.0/11", "0.0.0.0/0", "x/24"])
def test_ps_link_network_refuses_bad_values(shell: str, tmp_path: Path, cidr: str) -> None:
    result = _run(shell, tmp_path, f"ConvertTo-RpcLinkNetwork '{cidr}'")
    assert result.returncode != 0


def test_ps_peer_list_refuses_off_link_peers(shell: str, tmp_path: Path) -> None:
    body = "$l = ConvertTo-RpcLinkNetwork '10.77.0.0/29'; ConvertTo-RpcPeerList -Entries @('10.77.0.2,192.168.50.20') -Port 50052 -Link $l"
    result = _run(shell, tmp_path, body)
    assert result.returncode != 0
    assert "not on the configured private link 10.77.0.0/29" in result.stderr


def test_ps_proto_major_and_build_parsing(shell: str, tmp_path: Path) -> None:
    body = r"""
ConvertTo-RpcProtoMajor '10'
$null -eq (ConvertTo-RpcProtoMajor ' ')
ConvertFrom-RpcLlamaVersionText "version: 1.1.0 (build 9123, commit 1715275)`nbuilt with MSVC"
ConvertFrom-RpcLlamaVersionText "version: 6000 (d834d44)"
$null -eq (ConvertFrom-RpcLlamaVersionText 'usage: x')
$null -eq (Get-RpcLlamaBuild 'C:\does-not-exist\llama-server.exe')
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["10", "True", "b9123-1715275", "b6000-d834d44", "True", "True"]
    for bad in ("0", "256", "x"):
        assert _run(shell, tmp_path, f"ConvertTo-RpcProtoMajor '{bad}'").returncode != 0


def test_sharded_launcher_refuses_off_link_peers_and_bad_pin(shell: str, tmp_path: Path) -> None:
    ok = _run_script(shell, tmp_path, "start-rpc-sharded.ps1",
                     ["-Action", "Env", "-RpcPeers", "10.77.0.2", "-RpcLinkCidr", "10.77.0.0/29"])
    assert ok.returncode == 0, ok.stderr
    assert "10.77.0.2:50052" in ok.stdout
    off = _run_script(shell, tmp_path, "start-rpc-sharded.ps1",
                      ["-Action", "Env", "-RpcPeers", "10.77.0.2", "-RpcLinkCidr", "10.88.0.0/24"])
    assert off.returncode != 0 and "not on the configured private link" in off.stderr
    pin = _run_script(shell, tmp_path, "start-rpc-sharded.ps1",
                      ["-Action", "Env", "-RpcPeers", "10.77.0.2", "-RpcProtoMajor", "abc"])
    assert pin.returncode != 0 and "SONDER_RPC_PROTO_MAJOR" in pin.stderr


def test_worker_refuses_a_bind_address_off_the_link(shell: str, tmp_path: Path) -> None:
    result = _run_script(shell, tmp_path, "start-rpc-worker.ps1",
                         ["-Action", "Health", "-BindAddress", "10.77.0.2", "-LinkCidr", "10.88.0.0/24",
                          "-InstallDir", str(tmp_path)])
    assert result.returncode != 0
    assert "rpc-server bind address 10.77.0.2 is not on the configured private link" in result.stderr
