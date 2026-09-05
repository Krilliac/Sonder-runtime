"""Large resumable bytes through the production Handler and credential binding."""
from dataclasses import replace
import hashlib
import time

from tests.test_artifact_transfer_production_http import receiver
from sonder_runtime.adapters.compute_fabric.artifact_transfer import (
    ArtifactTransferClient, HttpsArtifactTransferPeer,
)
from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
from sonder_runtime.interfaces.http import serve


def test_large_production_transfer_resumes_after_binding_reopen(receiver, tmp_path, monkeypatch):
    address, current, original = receiver
    block = bytes(range(256)) * 4096
    source = tmp_path / "source.bin"
    digest = hashlib.sha256()
    with source.open("wb") as stream:
        for _ in range(66):
            stream.write(block)
            digest.update(block)
    spec = {"sha256": digest.hexdigest(), "size_bytes": 66 * len(block),
            "media_type": "application/octet-stream"}
    peer = HttpsArtifactTransferPeer.for_test_loopback(
        "http://%s:%s" % address,
        credential_provider=lambda: current[0].secrets.artifact_transfer_key,
    )
    client = ArtifactTransferClient(peer)
    with source.open("rb") as stream:
        partial = client.upload(stream, spec, "composed-large", max_chunks=3)
    assert partial["offset"] == 3 * len(block)
    original.close()
    reopened = ArtifactTransferBinding(lambda: current[0])
    reopened.start()
    monkeypatch.setattr(serve, "_ARTIFACT_TRANSFER_BINDING", reopened)
    cache_config = replace(current[0], artifact_transfer=replace(
        current[0].artifact_transfer, store_dir=str(tmp_path / "private-cache")))
    cache = ArtifactTransferBinding(lambda: cache_config)
    try:
        with source.open("rb") as stream:
            resumed = client.upload(stream, spec, "composed-large")
        assert resumed["transfer_id"] == partial["transfer_id"]
        deadline = time.monotonic() + 30
        while resumed["state"] == "verifying" and time.monotonic() < deadline:
            resumed = peer.inspect(resumed["transfer_id"])
            time.sleep(.01)
        assert resumed["state"] == "sealed"
        context = cache.authenticate("Bearer " + cache_config.secrets.artifact_transfer_key,
                                     correlation_id="composed-cache")
        downloaded = client.download(resumed["artifact"], cache.service(), "download", context)
        deadline = time.monotonic() + 30
        while downloaded["state"] == "verifying" and time.monotonic() < deadline:
            downloaded = cache.service().inspect_upload(downloaded["transfer_id"], context)
            time.sleep(.01)
        assert downloaded["state"] == "sealed"
        verified = hashlib.sha256()
        for offset in range(0, spec["size_bytes"], len(block)):
            verified.update(cache.service().read_range(
                downloaded["artifact"]["artifact_id"], offset, len(block), context).body)
        assert verified.hexdigest() == spec["sha256"]
    finally:
        cache.close()
        reopened.close()
