"""SPEC-4: resumable download and cross-platform activation."""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

import sonder_updates
from sonder_updates import (
    TrustError,
    UpdateError,
    resumable_download,
    switch_active_pointer,
    _read_pointer,
    _sha256_file,
)

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, data: bytes, status: int = 200, headers=None):
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers = headers or {}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(script):
    """script: list of (expected_range_or_None, response_factory)."""
    calls = {"n": 0}

    def opener(url, headers):
        idx = calls["n"]
        calls["n"] += 1
        expected_range, factory = script[idx]
        if expected_range is None:
            assert "Range" not in headers, headers
        else:
            assert headers.get("Range") == expected_range, headers
        return factory()

    opener.calls = calls
    return opener


def test_fresh_download_verifies_hash(tmp_path):
    payload = b"sonder-release-bytes" * 100
    dest = tmp_path / "engine.tar.gz"
    opener = _opener([(None, lambda: _FakeResponse(payload))])
    result = resumable_download(
        "http://mirror/engine.tar.gz", dest,
        expected_length=len(payload),
        expected_sha256=__import__("hashlib").sha256(payload).hexdigest(),
        opener=opener,
    )
    assert dest.read_bytes() == payload
    assert result["resumed"] is False
    assert not (tmp_path / "engine.tar.gz.partial").exists()


def test_hash_mismatch_rejected(tmp_path):
    payload = b"tampered"
    dest = tmp_path / "e.tar.gz"
    opener = _opener([(None, lambda: _FakeResponse(payload))])
    with pytest.raises(TrustError):
        resumable_download("http://m/e", dest, expected_sha256="00" * 32,
                           opener=opener)


def test_length_mismatch_rejected(tmp_path):
    dest = tmp_path / "e.tar.gz"
    opener = _opener([(None, lambda: _FakeResponse(b"short"))])
    with pytest.raises(UpdateError):
        resumable_download("http://m/e", dest, expected_length=999,
                           opener=opener)


def test_resume_with_matching_validators(tmp_path):
    full = b"abcdefghij" * 50
    dest = tmp_path / "e.tar.gz"
    # Seed an interrupted partial and its validator sidecar.
    partial = tmp_path / "e.tar.gz.partial"
    partial.write_bytes(full[:200])
    (tmp_path / "e.tar.gz.partial.validators.json").write_text(
        '{"etag": "v1"}', encoding="utf-8"
    )
    opener = _opener([
        ("bytes=200-", lambda: _FakeResponse(full[200:], status=206)),
    ])
    result = resumable_download(
        "http://m/e", dest, validators={"etag": "v1"},
        expected_sha256=_hash(full), opener=opener,
    )
    assert dest.read_bytes() == full
    assert result["resumed"] is True
    assert result["resumed_from"] == 200


def test_changed_validators_discard_partial_and_restart(tmp_path):
    full = b"x" * 300
    dest = tmp_path / "e.tar.gz"
    (tmp_path / "e.tar.gz.partial").write_bytes(b"stale-bytes")
    (tmp_path / "e.tar.gz.partial.validators.json").write_text(
        '{"etag": "OLD"}', encoding="utf-8"
    )
    # Upstream changed (etag NEW): no Range header, full restart.
    opener = _opener([(None, lambda: _FakeResponse(full))])
    result = resumable_download(
        "http://m/e", dest, validators={"etag": "NEW"},
        expected_sha256=_hash(full), opener=opener,
    )
    assert dest.read_bytes() == full
    assert result["resumed"] is False


def test_server_ignores_range_falls_back_to_full(tmp_path):
    full = b"y" * 400
    dest = tmp_path / "e.tar.gz"
    (tmp_path / "e.tar.gz.partial").write_bytes(full[:100])
    (tmp_path / "e.tar.gz.partial.validators.json").write_text(
        '{"etag": "v1"}', encoding="utf-8"
    )
    # We ask to resume, but the server answers 200 (whole file): overwrite.
    opener = _opener([("bytes=100-", lambda: _FakeResponse(full, status=200))])
    result = resumable_download(
        "http://m/e", dest, validators={"etag": "v1"},
        expected_sha256=_hash(full), opener=opener,
    )
    assert dest.read_bytes() == full
    assert result["resumed"] is False


def _hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# --- activation pointer -----------------------------------------------------

def test_symlink_switch_and_read(tmp_path):
    target1 = tmp_path / "releases" / "1.0.0"
    target2 = tmp_path / "releases" / "1.1.0"
    target1.mkdir(parents=True)
    target2.mkdir(parents=True)
    link = tmp_path / "current"
    prev = switch_active_pointer(link, target1)
    assert prev is None
    assert _read_pointer(link) == str(target1)
    prev = switch_active_pointer(link, target2)
    assert Path(prev) == target1
    assert _read_pointer(link) == str(target2)


def test_pointer_file_fallback_when_symlink_unavailable(tmp_path, monkeypatch):
    target = tmp_path / "releases" / "2.0.0"
    target.mkdir(parents=True)
    link = tmp_path / "current"

    def no_symlink(*a, **k):
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(sonder_updates.os, "symlink", no_symlink)
    prev = switch_active_pointer(link, target)
    assert prev is None
    # A pointer file carries the target; _read_pointer resolves it.
    assert (tmp_path / "current.pointer").is_file()
    assert _read_pointer(link) == str(target)


def test_pointer_fallback_refuses_a_stale_symlink_it_cannot_retire(
    tmp_path, monkeypatch
):
    """Never report activation when `_read_pointer` still selects old link."""
    old_target = tmp_path / "releases" / "1.0.0"
    new_target = tmp_path / "releases" / "1.1.0"
    old_target.mkdir(parents=True)
    new_target.mkdir(parents=True)
    link = tmp_path / "current"
    os.symlink(old_target, link, target_is_directory=True)
    hidden_pointer = tmp_path / "releases" / "stale-fallback"
    (tmp_path / "current.pointer").write_text(
        str(hidden_pointer), encoding="utf-8"
    )

    def no_symlink(*_args, **_kwargs):
        raise OSError("symlink privilege not held")

    original_unlink = Path.unlink

    def deny_current_unlink(path, *args, **kwargs):
        if path == link:
            raise PermissionError("current symlink is in use")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(sonder_updates.os, "symlink", no_symlink)
    monkeypatch.setattr(Path, "unlink", deny_current_unlink)

    with pytest.raises(sonder_updates.UpdateError, match="cannot be removed"):
        switch_active_pointer(link, new_target)

    # The attempted fallback pointer is restored, so the new target cannot
    # spring into effect later merely because somebody deletes the old link.
    assert (tmp_path / "current.pointer").read_text(encoding="utf-8") == str(
        hidden_pointer
    )
    assert _read_pointer(link) == str(old_target)
