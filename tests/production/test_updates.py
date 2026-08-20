"""SPEC-4: update state machine, manifests, safe extraction, trust gates."""
from __future__ import annotations

import io
import json
import tarfile

import pytest

import sonder_runtime.adapters.updates.service as sonder_updates
from sonder_runtime.adapters.updates.service import (
    BundleManifest,
    CANCELLABLE_STATES,
    ExtractionError,
    InvalidUpdateTransition,
    STATES,
    TRANSITIONS,
    TrustError,
    UpdateRepository,
    ConcurrencyConflict,
    build_bundle,
    safe_extract,
    validate_transition,
)

pytestmark = pytest.mark.unit


# -- state machine ----------------------------------------------------------

def test_every_transition_target_is_a_known_state():
    for state, nexts in TRANSITIONS.items():
        assert state in STATES
        assert nexts <= frozenset(STATES)


def test_happy_path_transitions_are_valid():
    chain = [
        "planned", "checking", "available", "downloading", "verified",
        "staged", "preflight", "backing_up", "draining", "installing",
        "migrating", "health_check", "committed",
    ]
    for current, following in zip(chain, chain[1:]):
        validate_transition(current, following)


@pytest.mark.parametrize(
    "current,requested",
    [
        ("committed", "planned"),      # terminal
        ("installing", "cancelled"),   # no cancel after install starts
        ("draining", "cancelled"),
        ("planned", "committed"),      # no skipping
        ("health_check", "installing"),  # no going backward
    ],
)
def test_forbidden_transitions_rejected(current, requested):
    with pytest.raises(InvalidUpdateTransition):
        validate_transition(current, requested)


def test_cancellation_window_excludes_activation_states():
    assert "installing" not in CANCELLABLE_STATES
    assert "draining" not in CANCELLABLE_STATES
    assert "migrating" not in CANCELLABLE_STATES
    assert "available" in CANCELLABLE_STATES


# -- manifest ---------------------------------------------------------------

def _minimal_manifest(**overrides) -> dict:
    raw = {
        "schema_version": 1,
        "bundle_id": "b", "version": "1.0.0", "commit_sha": "c",
        "channel": "stable", "platform": "linux", "architecture": "x86_64",
        "package_format": "tar.gz", "runtime": {"python_version": "3.11"},
        "state_schema": {}, "resources": {}, "entrypoints": {},
        "health_checks": [], "rollback": {},
        "files": [{"path": "a.py", "length": 1, "sha256": "0" * 64}],
    }
    raw.update(overrides)
    return raw


def test_manifest_missing_keys_rejected():
    raw = _minimal_manifest()
    del raw["files"]
    with pytest.raises(sonder_updates.UpdateError, match="missing keys"):
        BundleManifest.from_dict(raw)


def test_manifest_traversal_path_rejected():
    raw = _minimal_manifest(
        files=[{"path": "../evil.py", "length": 1, "sha256": "0" * 64}]
    )
    with pytest.raises(sonder_updates.UpdateError, match="escapes"):
        BundleManifest.from_dict(raw)


def test_manifest_future_schema_rejected():
    with pytest.raises(sonder_updates.UpdateError, match="schema_version"):
        BundleManifest.from_dict(_minimal_manifest(schema_version=99))


# -- bundle build -----------------------------------------------------------

@pytest.fixture()
def mini_source(tmp_path):
    """A tiny fake runtime whose entry point succeeds for migrate/status."""
    source = tmp_path / "source"
    package = source / "sonder_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        "print('{\"ok\": true}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (source / "app.py").write_text("print('app')\n", encoding="utf-8")
    return source


def test_build_bundle_produces_verifiable_archive(mini_source, tmp_path):
    out = tmp_path / "out"
    result = build_bundle(
        mini_source, out, version="1.2.3", channel="stable"
    )
    manifest = BundleManifest.load(result["manifest"])
    assert manifest["version"] == "1.2.3"
    archive_info = manifest["archive"]
    from pathlib import Path

    archive = Path(result["archive"])
    assert archive.stat().st_size == archive_info["length"]
    assert sonder_updates._sha256_file(archive) == archive_info["sha256"]
    extract_to = tmp_path / "x"
    safe_extract(archive, extract_to, max_expanded_bytes=10_000_000)
    assert manifest.verify_tree(extract_to) == []


# -- adversarial extraction -------------------------------------------------

def _tar_with_member(tmp_path, info: tarfile.TarInfo, data: bytes = b""):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        if data:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        else:
            tar.addfile(info)
    return archive


def test_extract_rejects_path_traversal(tmp_path):
    info = tarfile.TarInfo("../../outside.txt")
    archive = _tar_with_member(tmp_path, info, b"x")
    with pytest.raises(ExtractionError, match="escapes"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_extract_rejects_absolute_path(tmp_path):
    info = tarfile.TarInfo("/etc/evil.conf")
    archive = _tar_with_member(tmp_path, info, b"x")
    with pytest.raises(ExtractionError, match="escapes"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_extract_rejects_symlink(tmp_path):
    info = tarfile.TarInfo("link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    archive = _tar_with_member(tmp_path, info)
    with pytest.raises(ExtractionError, match="link"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_extract_rejects_device_node(tmp_path):
    info = tarfile.TarInfo("dev")
    info.type = tarfile.CHRTYPE
    archive = _tar_with_member(tmp_path, info)
    with pytest.raises(ExtractionError, match="device"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_extract_rejects_size_bomb(tmp_path):
    archive = tmp_path / "bomb.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"a" * 4096
        info = tarfile.TarInfo("big.bin")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ExtractionError, match="budget"):
        safe_extract(archive, tmp_path / "out", max_expanded_bytes=1024)


def test_truncated_archive_never_verifies(tmp_path, mini_source):
    # A truncated tar stream may end "cleanly" at a member boundary, so the
    # defense is layered: extraction either raises, or the staged tree fails
    # manifest verification. Either way the payload is refused.
    out = tmp_path / "out"
    result = build_bundle(mini_source, out, version="9.9.9")
    from pathlib import Path

    manifest = BundleManifest.load(result["manifest"])
    archive = Path(result["archive"])
    for fraction in (2, 4, 16):
        truncated = tmp_path / f"trunc-{fraction}.tar.gz"
        truncated.write_bytes(
            archive.read_bytes()[: archive.stat().st_size // fraction]
        )
        dest = tmp_path / f"x-{fraction}"
        try:
            safe_extract(truncated, dest, max_expanded_bytes=10_000_000)
        except ExtractionError:
            continue  # rejected outright
        assert manifest.verify_tree(dest), (
            f"truncation to 1/{fraction} produced a tree that verifies"
        )


# -- repository CAS ---------------------------------------------------------

def test_repository_cas_rejects_stale_revision(tmp_path):
    repo = UpdateRepository(str(tmp_path / "updates.db"))
    plan = repo.create_plan(
        channel="stable", source_kind="offline", source_ref="/x",
        from_release_id="rel_a", target_version="1.0.0",
        target_manifest_sha256="m", status="planned",
    )
    advanced = repo.advance(plan, "checking")
    assert advanced["revision"] == plan["revision"] + 1
    with pytest.raises(ConcurrencyConflict):
        repo.advance(plan, "checking")  # stale revision


def test_repository_idempotency_key_returns_same_plan(tmp_path):
    repo = UpdateRepository(str(tmp_path / "updates.db"))
    kwargs = dict(
        channel="stable", source_kind="offline", source_ref="/x",
        from_release_id="rel_a", target_version="1.0.0",
        target_manifest_sha256="m", status="planned",
        idempotency_key="idem-1",
    )
    first = repo.create_plan(**kwargs)
    second = repo.create_plan(**kwargs)
    assert first["update_id"] == second["update_id"]


# -- trust gate -------------------------------------------------------------

def test_unsigned_bundle_refused_without_gate(mini_source, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("SONDER_UPDATE_ALLOW_UNSIGNED", raising=False)
    out = tmp_path / "bundle"
    build_bundle(mini_source, out, version="1.0.0")
    manifest = BundleManifest.load(out / "manifest.json")
    with pytest.raises(TrustError, match="TUF"):
        sonder_updates.verify_bundle_trust(
            Path(out), manifest, allow_unverified=False
        )
    # Even the flag alone is not enough without the environment gate.
    with pytest.raises(TrustError):
        sonder_updates.verify_bundle_trust(
            Path(out), manifest, allow_unverified=True
        )


def test_unsigned_bundle_allowed_with_explicit_double_gate(
    mini_source, tmp_path, monkeypatch
):
    from pathlib import Path

    monkeypatch.setenv("SONDER_UPDATE_ALLOW_UNSIGNED", "1")
    out = tmp_path / "bundle"
    build_bundle(mini_source, out, version="1.0.0")
    manifest = BundleManifest.load(out / "manifest.json")
    mode = sonder_updates.verify_bundle_trust(
        Path(out), manifest, allow_unverified=True
    )
    assert mode == "unsigned-explicitly-allowed"
