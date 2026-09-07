"""Test-only two-process loopback evidence for the artifact transfer path.

This deliberately exercises the existing test-only HTTP loopback factory.  It
does not prove an independent-node, TLS, failover, restart, or production
artifact-mobility guarantee.
"""

from __future__ import annotations

import hashlib
import io
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.compute_fabric.artifact_transfer import (
    ArtifactTransferClient,
    HttpsArtifactTransferPeer,
)
from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
from sonder_runtime.interfaces.http import serve
from sonder_runtime.platform.artifact_transfer_config import ArtifactTransferConfig
from sonder_runtime.platform.config import Secrets, SonderConfig, StateConfig


_PAYLOAD_SIZE = 2 * 1024 * 1024 + 37
_MAX_RANGE_BYTES = 1024 * 1024
_REPORT_TIMEOUT_SECONDS = 30
_PROCESS_JOIN_SECONDS = 15
_SEAL_TIMEOUT_SECONDS = 15
_CHILD_SHUTDOWN_SECONDS = 30


def _test_transfer_key(role: str) -> str:
    """Return a deterministic test-only credential without reporting it."""
    return "artifact-loopback-%s-" % role + (role[0] * 32)


def _payload() -> bytes:
    block = bytes(range(251))
    return (block * ((_PAYLOAD_SIZE + len(block) - 1) // len(block)))[:_PAYLOAD_SIZE]


def _receiver_config(root: Path, role: str) -> SonderConfig:
    return SonderConfig(
        state=StateConfig(home=str((root / "state").resolve())),
        secrets=Secrets(
            api_key="admin-" + (role[0] * 32),
            artifact_transfer_key=_test_transfer_key(role),
        ),
        artifact_transfer=ArtifactTransferConfig(
            enabled=True,
            store_dir=str((root / "private").resolve()),
            principal_id="loopback-owner",
            project_id="artifact-mobility-rehearsal",
            peer_node_id="loopback-" + role,
            grant_id="loopback-" + role + "-grant",
            expires_at=int(time.time()) + 300,
            can_read=True,
            can_write=True,
        ),
    )


def _start_receiver(root_value: str, role: str):
    root = Path(root_value).resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = _receiver_config(root, role)
    binding = ArtifactTransferBinding(lambda: config)
    binding.start()
    serve._ARTIFACT_TRANSFER_BINDING = binding
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:%d" % server.server_address[1]
    peer = HttpsArtifactTransferPeer.for_test_loopback(
        origin, credential_provider=lambda: _test_transfer_key(role)
    )
    return root, binding, server, thread, peer, origin


def _stop_receiver(binding, server, thread) -> None:
    try:
        server.shutdown()
        server.server_close()
        thread.join(_PROCESS_JOIN_SECONDS)
        if thread.is_alive():
            raise RuntimeError("receiver thread did not stop")
    finally:
        if serve._ARTIFACT_TRANSFER_BINDING is binding:
            serve._ARTIFACT_TRANSFER_BINDING = None
        binding.close()


def _wait_for_seal(peer, receipt: dict) -> dict:
    deadline = time.monotonic() + _SEAL_TIMEOUT_SECONDS
    while receipt.get("state") == "verifying" and time.monotonic() < deadline:
        time.sleep(0.02)
        receipt = peer.inspect(receipt["transfer_id"])
    if receipt.get("state") != "sealed":
        raise RuntimeError("artifact receiver did not seal within the bound")
    return receipt


class PeerRangeStream:
    """Test-local seekable source that relays one validated peer range at a time."""

    def __init__(self, peer, artifact: dict):
        self._peer = peer
        self._artifact = artifact
        self._offset = 0

    def seek(self, offset: int, whence: int = 0) -> int:
        size = self._artifact["size_bytes"]
        if type(offset) is not int or whence != 0 or not 0 <= offset <= size:
            raise ValueError("invalid bounded source seek")
        self._offset = offset
        return offset

    def read(self, length: int) -> bytes:
        size = self._artifact["size_bytes"]
        if type(length) is not int or not 0 <= length <= _MAX_RANGE_BYTES:
            raise ValueError("invalid bounded source read")
        if not length or self._offset == size:
            return b""
        amount = min(length, size - self._offset)
        item = self._peer.read_range(self._artifact["artifact_id"], self._offset, amount)
        if (
            item.sha256 != self._artifact["sha256"]
            or item.size_bytes != size
            or item.offset != self._offset
            or item.length != amount
        ):
            raise RuntimeError("source range did not match its sealed receipt")
        self._offset += amount
        return item.body


def _digest_remote_artifact(peer, artifact: dict) -> tuple[str, list[int]]:
    digest = hashlib.sha256()
    offset = 0
    lengths: list[int] = []
    while offset < artifact["size_bytes"]:
        amount = min(_MAX_RANGE_BYTES, artifact["size_bytes"] - offset)
        item = peer.read_range(artifact["artifact_id"], offset, amount)
        if (
            item.sha256 != artifact["sha256"]
            or item.size_bytes != artifact["size_bytes"]
            or item.offset != offset
            or item.length != amount
        ):
            raise RuntimeError("destination range did not match its sealed receipt")
        digest.update(item.body)
        lengths.append(item.length)
        offset += item.length
    return digest.hexdigest(), lengths


def _source_worker(root_value: str, stop, reports) -> None:
    binding = server = thread = None
    try:
        root, binding, server, thread, peer, origin = _start_receiver(root_value, "source")
        payload = _payload()
        spec = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "media_type": "application/octet-stream",
        }
        receipt = ArtifactTransferClient(peer).upload(
            io.BytesIO(payload), spec, "loopback-source-upload"
        )
        receipt = _wait_for_seal(peer, receipt)
        artifact = peer.artifact(receipt["artifact"]["artifact_id"])
        if artifact != receipt["artifact"]:
            raise RuntimeError("source receipt did not resolve to its artifact")
        reports.put(
            {
                "kind": "source-ready",
                "pid": os.getpid(),
                "root": str(root),
                "origin": origin,
                "artifact": artifact,
            }
        )
        if not stop.wait(_CHILD_SHUTDOWN_SECONDS):
            raise TimeoutError("source did not receive bounded shutdown signal")
    except Exception as error:
        reports.put({"kind": "failure", "role": "source", "error_type": type(error).__name__})
    finally:
        if binding is not None:
            _stop_receiver(binding, server, thread)


def _destination_worker(root_value: str, source_origin: str, source_artifact: dict, reports) -> None:
    binding = server = thread = None
    try:
        root, binding, server, thread, peer, origin = _start_receiver(root_value, "destination")
        source_peer = HttpsArtifactTransferPeer.for_test_loopback(
            source_origin, credential_provider=lambda: _test_transfer_key("source")
        )
        spec = {
            key: source_artifact[key]
            for key in ("sha256", "size_bytes", "media_type")
        }
        receipt = ArtifactTransferClient(peer).upload(
            PeerRangeStream(source_peer, source_artifact),
            spec,
            "loopback-destination-upload",
        )
        receipt = _wait_for_seal(peer, receipt)
        artifact = peer.artifact(receipt["artifact"]["artifact_id"])
        if artifact != receipt["artifact"]:
            raise RuntimeError("destination receipt did not resolve to its artifact")
        verified_sha256, range_lengths = _digest_remote_artifact(peer, artifact)
        reports.put(
            {
                "kind": "destination-complete",
                "pid": os.getpid(),
                "root": str(root),
                "origin": origin,
                "receipt": receipt,
                "verified_sha256": verified_sha256,
                "range_lengths": range_lengths,
            }
        )
    except Exception as error:
        reports.put(
            {"kind": "failure", "role": "destination", "error_type": type(error).__name__}
        )
    finally:
        if binding is not None:
            _stop_receiver(binding, server, thread)


def _await_report(reports, expected_kind: str) -> dict:
    deadline = time.monotonic() + _REPORT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            report = reports.get(timeout=min(0.25, deadline - time.monotonic()))
        except Empty:
            continue
        if report.get("kind") == "failure":
            pytest.fail("%s worker failed: %s" % (report["role"], report["error_type"]))
        if report.get("kind") == expected_kind:
            return report
    pytest.fail("timed out waiting for %s" % expected_kind)


def _finish_process(process) -> bool:
    if process is None:
        return False
    process.join(_PROCESS_JOIN_SECONDS)
    if not process.is_alive():
        return False
    process.terminate()
    process.join(_PROCESS_JOIN_SECONDS)
    return True


def test_two_spawned_loopback_receivers_rehearse_artifact_transfer_path(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    reports = context.Queue()
    stop_source = context.Event()
    source = context.Process(
        target=_source_worker,
        args=(str(tmp_path / "source"), stop_source, reports),
        name="artifact-loopback-source",
    )
    destination = None
    source_forced = destination_forced = False
    try:
        source.start()
        source_report = _await_report(reports, "source-ready")
        destination = context.Process(
            target=_destination_worker,
            args=(
                str(tmp_path / "destination"),
                source_report["origin"],
                source_report["artifact"],
                reports,
            ),
            name="artifact-loopback-destination",
        )
        destination.start()
        destination_report = _await_report(reports, "destination-complete")
    finally:
        stop_source.set()
        destination_forced = _finish_process(destination)
        source_forced = _finish_process(source)

    assert not source_forced
    assert not destination_forced
    assert source.exitcode == 0
    assert destination is not None and destination.exitcode == 0
    assert source_report["pid"] != os.getpid()
    assert destination_report["pid"] != os.getpid()
    assert source_report["pid"] != destination_report["pid"]
    assert source_report["origin"].startswith("http://127.0.0.1:")
    assert destination_report["origin"].startswith("http://127.0.0.1:")
    assert source_report["origin"] != destination_report["origin"]
    assert Path(source_report["root"]) != Path(destination_report["root"])

    source_artifact = source_report["artifact"]
    destination_receipt = destination_report["receipt"]
    destination_artifact = destination_receipt["artifact"]
    assert destination_receipt["state"] == "sealed"
    assert destination_artifact["sha256"] == source_artifact["sha256"]
    assert destination_artifact["size_bytes"] == _PAYLOAD_SIZE
    assert destination_report["verified_sha256"] == source_artifact["sha256"]
    assert destination_report["range_lengths"]
    assert sum(destination_report["range_lengths"]) == _PAYLOAD_SIZE
    assert all(0 < length <= _MAX_RANGE_BYTES for length in destination_report["range_lengths"])
