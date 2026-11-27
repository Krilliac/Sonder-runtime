"""RPC version lockstep, private-link policy and node build inventory for cross-host splits."""
from __future__ import annotations

import ipaddress
import socket
import struct
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference import rpc_cluster as rc
from sonder_runtime.platform.config import ConfigError, load_config
from sonder_runtime.platform.rpc_sharding_config import (
    RpcShardingConfigError,
    parse_link_network,
    parse_rpc_proto_major,
    rpc_peer_allowlist,
)

# Placeholder private-range addresses; they belong to no real deployment.
WORKER = "10.77.0.2"
WORKER_B = "10.77.0.3"
OFF_LINK = "192.168.50.20"
LINK = "10.77.0.0/29"
LOOPBACK_URL = "http://127.0.0.1:11438"

# Wire bytes taken from ggml-rpc.cpp: cmd 14, uint64 LE size 24, 24 zero conn_caps.
EXPECTED_HELLO = bytes([14]) + (24).to_bytes(8, "little") + bytes(24)


def _hello_reply(major: int, minor: int = 0, patch: int = 0, *, size: int = 28) -> bytes:
    body = bytes([major, minor, patch, 0]) + bytes(max(0, size - 4))
    return struct.pack("<Q", size) + body[:size]


class _FakeSocket:
    def __init__(self, reply: bytes, *, chunk: int = 5) -> None:
        self._reply = reply
        self._chunk = chunk
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        take = min(size, self._chunk, len(self._reply))
        out, self._reply = self._reply[:take], self._reply[take:]
        return out

    def close(self) -> None:
        self.closed = True


def _connector(replies: dict[str, bytes | Exception], opened: list[_FakeSocket] | None = None):
    def connect(address: tuple[str, int], timeout: float) -> _FakeSocket:
        assert timeout > 0
        reply = replies[address[0]]
        if isinstance(reply, Exception):
            raise reply
        sock = _FakeSocket(reply)
        if opened is not None:
            opened.append(sock)
        return sock

    return connect


# --- platform policy -------------------------------------------------------------


def test_link_network_parses_strict_private_ipv4_networks() -> None:
    assert parse_link_network(None) is None
    assert parse_link_network("  ") is None
    assert parse_link_network(" 10.77.0.0/30 ") == ipaddress.IPv4Network("10.77.0.0/30")
    assert parse_link_network("169.254.10.0/24") == ipaddress.IPv4Network("169.254.10.0/24")


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("10.77.0.1/30", "host bits"),
        ("0.0.0.0/0", "not inside a private"),
        ("8.8.8.0/24", "not inside a private"),
        ("10.0.0.0/7", "not inside a private"),
        ("fd00::/64", "IPv4"),
        ("worker.lan/24", "IPv4 network"),
    ],
)
def test_link_network_refuses_public_wide_or_malformed(raw: str, fragment: str) -> None:
    with pytest.raises(RpcShardingConfigError, match=fragment):
        parse_link_network(raw)


def test_peers_must_sit_on_the_configured_link() -> None:
    allowlist = rpc_peer_allowlist(
        {"SONDER_RPC_PEERS": f"{WORKER},{WORKER_B}", "SONDER_RPC_LINK_CIDR": LINK}
    )
    assert allowlist.peers == ((WORKER, 50052), (WORKER_B, 50052))
    assert allowlist.link_network == ipaddress.IPv4Network(LINK)
    with pytest.raises(RpcShardingConfigError, match="not on the configured private link"):
        rpc_peer_allowlist({"SONDER_RPC_PEERS": f"{WORKER},{OFF_LINK}", "SONDER_RPC_LINK_CIDR": LINK})


def test_without_a_link_any_allowlisted_private_peer_still_loads() -> None:
    assert rpc_peer_allowlist({"SONDER_RPC_PEERS": OFF_LINK}).link_network is None


def test_off_link_peer_and_bad_settings_fail_config_load() -> None:
    base = {"SONDER_OPENAI_BASE_URL": LOOPBACK_URL}
    load_config(env={**base, "SONDER_RPC_PEERS": WORKER, "SONDER_RPC_LINK_CIDR": LINK,
                     "SONDER_RPC_PROTO_MAJOR": "10"})
    for extra, fragment in (
        ({"SONDER_RPC_PEERS": OFF_LINK, "SONDER_RPC_LINK_CIDR": LINK}, "private link"),
        ({"SONDER_RPC_LINK_CIDR": "203.0.113.0/24"}, "SONDER_RPC_LINK_CIDR"),
        ({"SONDER_RPC_PROTO_MAJOR": "ten"}, "SONDER_RPC_PROTO_MAJOR"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            load_config(env={**base, **extra})
        assert any(fragment in error for error in excinfo.value.errors), excinfo.value.errors


@pytest.mark.parametrize(("raw", "expected"), [(None, None), ("", None), ("7", 7), (" 10 ", 10), ("255", 255)])
def test_proto_major_parses(raw: str | None, expected: int | None) -> None:
    assert parse_rpc_proto_major(raw) == expected


@pytest.mark.parametrize("raw", ["0", "256", "-1", "10.0", "1e1", "x", "1000"])
def test_proto_major_refuses_out_of_range_or_non_decimal(raw: str) -> None:
    with pytest.raises(RpcShardingConfigError, match="SONDER_RPC_PROTO_MAJOR"):
        parse_rpc_proto_major(raw)


def test_settings_carry_link_and_pinned_major() -> None:
    settings = rc.rpc_sharding_settings_from_env({
        "SONDER_RPC_PEERS": WORKER,
        "SONDER_RPC_LINK_CIDR": LINK,
        "SONDER_RPC_PROTO_MAJOR": "10",
        "SONDER_OPENAI_BASE_URL": LOOPBACK_URL,
    })
    assert settings is not None
    assert settings.proto_major == 10
    assert settings.link_network == ipaddress.IPv4Network(LINK)


# --- RPC HELLO wire probe ---------------------------------------------------------


@pytest.mark.parametrize(("major", "minor", "patch"), [(7, 0, 0), (10, 0, 0), (10, 0, 2)])
def test_hello_sends_the_llama_cpp_request_and_reads_the_version(major: int, minor: int, patch: int) -> None:
    opened: list[_FakeSocket] = []
    version = rc.read_rpc_protocol_version(
        WORKER, connect_fn=_connector({WORKER: _hello_reply(major, minor, patch)}, opened)
    )
    assert version == rc.RpcProtocolVersion(major, minor, patch)
    assert str(version) == f"{major}.{minor}.{patch}"
    assert opened[0].sent == EXPECTED_HELLO
    assert opened[0].closed


def test_hello_rejects_peers_outside_the_policy_before_connecting() -> None:
    def never(_address: tuple[str, int], _timeout: float) -> _FakeSocket:
        raise AssertionError("must not connect")

    for bad in ("0.0.0.0", "8.8.8.8", "127.0.0.1", "worker.lan"):
        with pytest.raises(RpcShardingConfigError):
            rc.read_rpc_protocol_version(bad, connect_fn=never)


@pytest.mark.parametrize("size", [0, 2, 4097, 2**63])
def test_hello_reply_size_out_of_bounds_is_refused(size: int) -> None:
    opened: list[_FakeSocket] = []
    reply = struct.pack("<Q", size) + bytes(8)
    with pytest.raises(RpcShardingConfigError, match="not a compatible llama.cpp rpc-server"):
        rc.read_rpc_protocol_version(WORKER, connect_fn=_connector({WORKER: reply}, opened))
    assert opened[0].closed


def test_hello_connection_closed_mid_reply_raises_oserror() -> None:
    opened: list[_FakeSocket] = []
    with pytest.raises(ConnectionError):
        rc.read_rpc_protocol_version(WORKER, connect_fn=_connector({WORKER: _hello_reply(10)[:10]}, opened))
    assert opened[0].closed


def test_hello_over_a_real_tcp_socket() -> None:
    received: list[bytes] = []
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]

        def serve() -> None:
            conn, _ = server.accept()
            with conn:
                data = b""
                while len(data) < len(EXPECTED_HELLO):
                    chunk = conn.recv(64)
                    if not chunk:
                        break
                    data += chunk
                received.append(data)
                conn.sendall(_hello_reply(10, 0, 2))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def to_loopback(_address: tuple[str, int], timeout: float) -> socket.socket:
            return rc._default_rpc_socket(("127.0.0.1", port), timeout)

        version = rc.read_rpc_protocol_version(WORKER, timeout=5.0, connect_fn=to_loopback)
        thread.join(timeout=5)
    assert version == rc.RpcProtocolVersion(10, 0, 2)
    assert received == [EXPECTED_HELLO]


# --- lockstep gate ----------------------------------------------------------------


def test_lockstep_passes_when_every_worker_matches_the_pinned_major() -> None:
    report = rc.check_rpc_lockstep(
        [WORKER, WORKER_B],
        expected_major=10,
        main_build="b9001-1715275",
        connect_fn=_connector({WORKER: _hello_reply(10, 0, 2), WORKER_B: _hello_reply(10, 0, 0)}),
    )
    assert report.ok, report.errors
    main, first, second = report.nodes
    assert (main.role, main.endpoint, main.llama_cpp_build) == ("main", "local", "b9001-1715275")
    assert (first.endpoint, str(first.protocol)) == (f"{WORKER}:50052", "10.0.2")
    assert second.to_dict()["rpc_proto_major"] == 10
    assert report.to_dict()["expected_rpc_proto_major"] == 10


def test_lockstep_refuses_a_worker_on_an_older_protocol() -> None:
    report = rc.check_rpc_lockstep(
        [WORKER], expected_major=10, connect_fn=_connector({WORKER: _hello_reply(7)})
    )
    assert not report.ok
    (error,) = report.errors
    assert f"{WORKER}:50052" in error and "7.0.0" in error and "SONDER_RPC_PROTO_MAJOR=10" in error


def test_lockstep_without_a_pin_still_refuses_disagreeing_workers() -> None:
    connect = _connector({WORKER: _hello_reply(10), WORKER_B: _hello_reply(7)})
    report = rc.check_rpc_lockstep([WORKER, WORKER_B], connect_fn=connect)
    assert not report.ok
    assert "between workers" in report.errors[0]
    assert f"{WORKER}:50052=10.0.0" in report.errors[0] and f"{WORKER_B}:50052=7.0.0" in report.errors[0]
    same = rc.check_rpc_lockstep(
        [WORKER, WORKER_B], connect_fn=_connector({WORKER: _hello_reply(7), WORKER_B: _hello_reply(7, 0, 1)})
    )
    assert same.ok


def test_lockstep_fails_closed_when_a_worker_does_not_answer() -> None:
    connect = _connector({WORKER: TimeoutError("timed out"), WORKER_B: _hello_reply(10)})
    report = rc.check_rpc_lockstep([WORKER, WORKER_B], expected_major=10, connect_fn=connect)
    assert not report.ok
    unreachable = report.nodes[1]
    assert unreachable.reachable is False and unreachable.protocol is None
    assert "did not answer the RPC HELLO" in report.errors[0]


def test_require_lockstep_raises_with_every_failure_listed() -> None:
    settings = rc.rpc_sharding_settings_from_env({
        "SONDER_RPC_PEERS": f"{WORKER},{WORKER_B}",
        "SONDER_RPC_PROTO_MAJOR": "10",
        "SONDER_OPENAI_BASE_URL": LOOPBACK_URL,
    })
    assert settings is not None
    connect = _connector({WORKER: _hello_reply(7), WORKER_B: ConnectionRefusedError("refused")})
    with pytest.raises(rc.RpcClusterConfigError) as excinfo:
        rc.require_rpc_lockstep(settings, connect_fn=connect)
    message = str(excinfo.value)
    assert message.startswith("cross-host RPC split refused:")
    assert f"{WORKER}:50052" in message and f"{WORKER_B}:50052" in message
    ok = rc.require_rpc_lockstep(
        settings, connect_fn=_connector({WORKER: _hello_reply(10), WORKER_B: _hello_reply(10)})
    )
    assert ok.ok


# --- node llama.cpp build ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("version: 1.1.0 (build 9123, commit 1715275)\nbuilt with MSVC for x64\n", "b9123-1715275"),
        ("load_backend: ...\nversion: 6000 (d834d44)\nbuilt with clang\n", "b6000-d834d44"),
        ("usage: llama-server [options]", None),
        ("", None),
    ],
)
def test_parse_llama_build(text: str, expected: str | None) -> None:
    assert rc.parse_llama_build(text) == expected


def test_local_llama_build_runs_the_rpc_folder_binary(tmp_path: Path) -> None:
    (tmp_path / "llama-server.exe").write_bytes(b"")
    seen: list[list[str]] = []

    def runner(argv: Sequence[str], timeout: float) -> str:
        seen.append(list(argv))
        return "version: 1.0.0 (build 9001, commit b1b770d)\n"

    assert rc.local_llama_build(tmp_path, run_fn=runner) == "b9001-b1b770d"
    assert seen == [[str(tmp_path / "llama-server.exe"), "--version"]]


def test_local_llama_build_is_none_when_missing_or_hung(tmp_path: Path) -> None:
    assert rc.local_llama_build(tmp_path, run_fn=lambda _a, _t: "version: 1 (abc)") is None
    (tmp_path / "llama-server").write_bytes(b"")

    def hung(argv: Sequence[str], timeout: float) -> str:
        raise subprocess.TimeoutExpired(list(argv), timeout)

    assert rc.local_llama_build(tmp_path, run_fn=hung) is None
