"""Explicit loopback-only HTTP acceptance; no production route is enabled."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import pytest

from tests.test_artifact_transfer import transfers
from sonder_runtime.adapters.compute_fabric.artifact_transfer import (
    ArtifactTransferClient,
    HttpsArtifactTransferPeer,
)
from sonder_runtime.adapters.persistence.artifact_transfer import (
    SQLiteArtifactTransferStore,
)
from sonder_runtime.application.artifacts.transfer import (
    ArtifactRange,
    ArtifactTransferService,
    TransferError,
)
from sonder_runtime.interfaces.http.facades.artifact_transfer import (
    dispatch_artifact_transfer,
    transfer_error_status,
)


@pytest.fixture
def receiver(transfers):
    service, store, grant, context = transfers

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_transfer()

        def do_POST(self):
            self.handle_transfer()

        def do_PUT(self):
            self.handle_transfer()

        def handle_transfer(self):
            if self.headers.get("Authorization") != "Bearer test-credential":
                self.send_error(401)
                return
            if getattr(self.server, "redirect", False):
                self.send_response(302)
                self.send_header("Location", "https://example.invalid:443/private")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = urllib.parse.urlsplit(self.path)
            pieces = path.path.strip("/").split("/")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1024 * 1024:
                    raise TransferError("INVALID_BOUND")
                raw = self.rfile.read(size)
                payload = (
                    json.loads(raw)
                    if raw and self.headers.get("Content-Type") == "application/json"
                    else {}
                )
                if pieces == ["v1", "artifact-transfers"]:
                    action = "begin"
                elif pieces[1] == "artifact-transfers":
                    payload["transfer_id"] = pieces[2]
                    if len(pieces) == 3:
                        action = "inspect"
                    elif pieces[3] == "chunks":
                        action = "append"
                        payload.update(
                            offset=int(pieces[4]),
                            chunk_sha256=self.headers["X-Sonder-Chunk-Sha256"],
                        )
                    else:
                        action = pieces[3]
                else:
                    payload["artifact_id"] = pieces[2]
                    action = "artifact"
                    if len(pieces) == 4:
                        action = "range"
                        query = urllib.parse.parse_qs(path.query)
                        payload.update(
                            offset=int(query["offset"][0]),
                            length=int(query["length"][0]),
                        )
                result = dispatch_artifact_transfer(
                    self.server.service, action, payload, context, body=raw
                )
                if isinstance(result.body, ArtifactRange):
                    blob = result.body.body
                    extra = {
                        "X-Sonder-Offset": str(result.body.offset),
                        "X-Sonder-Size": str(result.body.size_bytes),
                        "X-Sonder-Artifact-Sha256": result.body.sha256,
                        "X-Sonder-Chunk-Sha256": result.body.chunk_sha256,
                    }
                    if getattr(self.server, "corrupt", False):
                        blob = bytes([blob[0] ^ 1]) + blob[1:] if blob else b"x"
                else:
                    blob, extra = json.dumps(result.body).encode(), {}
                self.send_response(result.status_code)
                self.send_header("Content-Length", str(len(blob)))
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(blob)
            except TransferError as error:
                blob = json.dumps({"code": str(error)}).encode()
                self.send_response(transfer_error_status(error))
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.service = service
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    peer = HttpsArtifactTransferPeer.for_test_loopback(
        origin, credential_provider=lambda: "test-credential"
    )
    yield server, peer, origin
    server.shutdown()
    server.server_close()
    thread.join(3)


def wait_sealed(peer, tid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = peer.inspect(tid)
        if result["state"] != "verifying":
            assert result["state"] == "sealed", result
            return result
        time.sleep(0.02)
    pytest.fail("seal timed out")


def test_real_66_mib_streaming_client_restart_and_resumed_download(
    receiver, transfers, tmp_path
):
    server, peer, origin = receiver
    service, store, grant, context = transfers
    source = tmp_path / "generated.bin"
    block = bytes(range(256)) * 4096
    sha = hashlib.sha256()
    with source.open("wb") as stream:
        for _ in range(66):
            stream.write(block)
            sha.update(block)
    spec = dict(
        sha256=sha.hexdigest(),
        size_bytes=66 * 1024 * 1024,
        media_type="application/octet-stream",
    )
    client = ArtifactTransferClient(peer)
    with source.open("rb") as stream:
        first = client.upload(stream, spec, "large", max_chunks=3)
    assert first["offset"] == 3 * 1024 * 1024
    # A separate client process reattaches using only the stable command receipt.
    script = """
import json,sys
from sonder_runtime.adapters.compute_fabric.artifact_transfer import ArtifactTransferClient,HttpsArtifactTransferPeer
peer=HttpsArtifactTransferPeer.for_test_loopback(sys.argv[1],credential_provider=lambda:'test-credential')
with open(sys.argv[2],'rb') as source:
    print(json.dumps(ArtifactTransferClient(peer).upload(source,json.loads(sys.argv[3]),'large',max_chunks=2)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, origin, str(source), json.dumps(spec)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["offset"] == 5 * 1024 * 1024
    # Reopen the receiver ledger/private store as well.
    reopened = ArtifactTransferService(
        SQLiteArtifactTransferStore(store.root), authorizer=lambda c, a: grant
    )
    server.service = reopened

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def seek(self, offset):
            return self.stream.seek(offset)

        def read(self, size):
            assert 0 < size <= 1024 * 1024
            return self.stream.read(size)

    try:
        with source.open("rb") as stream:
            uploaded = client.upload(BoundedReader(stream), spec, "large")
        artifact = wait_sealed(peer, uploaded["transfer_id"])["artifact"]
        cache = ArtifactTransferService(
            SQLiteArtifactTransferStore(tmp_path / "cache"),
            authorizer=lambda c, a: grant,
        )
        try:
            partial = client.download(
                artifact, cache, "download", context, max_chunks=7
            )
            assert partial["offset"] == 7 * 1024 * 1024
        finally:
            cache.close()
        cache = ArtifactTransferService(
            SQLiteArtifactTransferStore(tmp_path / "cache"),
            authorizer=lambda c, a: grant,
        )
        try:
            downloaded = client.download(artifact, cache, "download", context)
            deadline = time.monotonic() + 30
            while downloaded["state"] == "verifying" and time.monotonic() < deadline:
                downloaded = cache.inspect_upload(downloaded["transfer_id"], context)
                time.sleep(0.02)
            assert downloaded["state"] == "sealed"
            verified = hashlib.sha256()
            for offset in range(0, spec["size_bytes"], 1024 * 1024):
                verified.update(
                    cache.read_range(
                        downloaded["artifact"]["artifact_id"],
                        offset,
                        1024 * 1024,
                        context,
                    ).body
                )
            assert verified.hexdigest() == spec["sha256"]
        finally:
            cache.close()
    finally:
        reopened.close()


def test_wire_cannot_supply_scope(receiver, transfers):
    _, peer, _ = receiver
    service, _, _, context = transfers
    with pytest.raises(TransferError, match="INVALID_REQUEST"):
        dispatch_artifact_transfer(
            service,
            "inspect",
            {"transfer_id": "0" * 32, "principal_id": "victim"},
            context,
        )
    with pytest.raises(TransferError, match="INVALID_TEST_ORIGIN"):
        HttpsArtifactTransferPeer.for_test_loopback(
            "http://192.0.2.1:1234", credential_provider=lambda: "x"
        )


def test_redirect_and_corrupted_binary_are_rejected(receiver, transfers):
    from tests.test_artifact_transfer import begin, digest, sealed

    server, peer, _ = receiver
    service, _, _, context = transfers
    data = b"private-opaque-fixture"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    artifact = sealed(service, context, tid)["artifact"]
    server.corrupt = True
    with pytest.raises(TransferError, match="PEER_DIGEST"):
        peer.read_range(artifact["artifact_id"], 0, len(data))
    server.corrupt = False
    server.redirect = True
    with pytest.raises(TransferError, match="PEER_UNAVAILABLE"):
        peer.artifact(artifact["artifact_id"])

def test_peer_rejects_non_bytes_response_body():
    class Response:
        status = 200
        headers = {"Content-Length": "4"}

        def read(self, _limit):
            return "text"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, _request, *, timeout):
            assert timeout == 10
            return Response()

    peer = HttpsArtifactTransferPeer.for_test_loopback(
        "http://127.0.0.1:1234", credential_provider=lambda: "test-credential"
    )
    peer._opener = Opener()
    with pytest.raises(TransferError, match="PEER_PROTOCOL"):
        peer.inspect("0" * 32)
