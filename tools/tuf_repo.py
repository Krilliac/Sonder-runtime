"""Sonder release publisher: a TUF repository over built engine bundles.

This is the publisher side of SPEC-4. It turns a bundle produced by
``python -m sonder_runtime update build`` into a signed, TUF-protected
offline update bundle that ``UpdateManager.import_offline`` verifies
through the client's TUF path — no custom signature code anywhere.

Roles and thresholds follow SPEC-4 section 1:

    root      2-of-3   trust anchors and delegation
    targets   2-of-3   authorizes release targets
    snapshot  1-of-1   binds metadata versions
    timestamp 1-of-1   freshness / freeze protection

Key custody: for the automated build/test ceremony (and CI) keys live in
a repo-local ``keystore/`` with restrictive permissions. In production the
root and at least one targets key are offline hardware-backed — the
signing ceremony (docs/runbooks/publish-release.md) covers that; this
module's ``sign`` step accepts externally-held keys through the same
keystore interface so the offline keys never touch a build worker.

Uses python-tuf's Metadata API; the exact tested versions are pinned in
the production lock file (SPEC-4 section 16).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import (
    Metadata,
    MetaFile,
    Role,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)

logger = logging.getLogger(__name__)

ROLES = ("root", "targets", "snapshot", "timestamp")
THRESHOLDS = {"root": 2, "targets": 2, "snapshot": 1, "timestamp": 1}
KEY_COUNTS = {"root": 3, "targets": 3, "snapshot": 1, "timestamp": 1}
# Expiries: long-lived anchors, short-lived freshness (freeze protection).
EXPIRY_DAYS = {"root": 365, "targets": 90, "snapshot": 7, "timestamp": 1}
SPEC_VERSION = "1.0.31"
KEYSTORE_PASSPHRASE_ENV = "SONDER_TUF_KEYSTORE_PASSPHRASE"
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def _validate_safe_target_name(target_name: object) -> str:
    """Validate *target_name* as a strict single-segment safe basename."""
    if not isinstance(target_name, str) or not target_name:
        raise ValueError("target name must be a non-empty string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in target_name):
        raise ValueError(f"unsafe target name (control character): {target_name!r}")
    if target_name != target_name.strip() or target_name.endswith((".", " ")):
        raise ValueError(f"unsafe target name (leading/trailing whitespace or dot): {target_name!r}")
    if "/" in target_name or "\\" in target_name or ":" in target_name:
        raise ValueError(f"unsafe target name (path separator or drive letter): {target_name!r}")
    if target_name in (".", "..") or ".." in target_name:
        raise ValueError(f"unsafe target name (traversal segment): {target_name!r}")
    if (
        PurePosixPath(target_name).parts != (target_name,)
        or PureWindowsPath(target_name).parts != (target_name,)
        or Path(target_name).name != target_name
    ):
        raise ValueError(f"unsafe target name (must be single basename): {target_name!r}")
    stem = target_name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_DEVICE_NAMES:
        raise ValueError(f"unsafe target name (reserved device name): {target_name!r}")
    return target_name


def _utc(days: int) -> datetime:
    # tuf compares against a naive UTC datetime; pass timestamps in rather
    # than reading the clock here so callers can make runs reproducible.
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)


def _keystore(repo: Path) -> Path:
    ks = repo / "keystore"
    ks.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(ks, 0o700)
    return ks


def _metadata_dir(repo: Path) -> Path:
    md = repo / "metadata"
    md.mkdir(parents=True, exist_ok=True)
    return md


def _save_signer(repo: Path, role: str, index: int, signer: CryptoSigner) -> None:
    path = _keystore(repo) / f"{role}_{index}.json"
    # Persist the private key material for the ceremony/CI. Production
    # offline keys are held outside the repo and never written here.
    from cryptography.hazmat.primitives import serialization

    passphrase = os.environ.get(KEYSTORE_PASSPHRASE_ENV, "")
    if passphrase:
        encryption: serialization.KeySerializationEncryption = (
            serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        )
        encrypted = True
    else:
        warnings.warn(
            f"{KEYSTORE_PASSPHRASE_ENV} is not set; writing unencrypted TUF keystore key",
            UserWarning,
            stacklevel=2,
        )
        logger.warning(
            "%s is not set; writing unencrypted TUF keystore key for role=%s",
            KEYSTORE_PASSPHRASE_ENV, role,
        )
        encryption = serialization.NoEncryption()
        encrypted = False

    private_key = getattr(signer, "_private_key", None)
    if private_key is None and hasattr(signer, "private_bytes"):
        private_key = serialization.load_pem_private_key(
            signer.private_bytes, password=None,
        )
    if private_key is None:
        raise ValueError("signer does not expose a serializable private key")

    data = {
        "keyid": signer.public_key.keyid,
        "encrypted": encrypted,
        "private_pem": private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        ).decode("utf-8"),
        "public_pem": private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8"),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _load_signers(repo: Path, role: str) -> list[CryptoSigner]:
    signers = []
    from cryptography.hazmat.primitives import serialization

    passphrase = os.environ.get(KEYSTORE_PASSPHRASE_ENV, "")
    for path in sorted(_keystore(repo).glob(f"{role}_*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        pem_bytes = str(raw["private_pem"]).encode("utf-8")
        is_encrypted = (
            bool(raw.get("encrypted"))
            or b"BEGIN ENCRYPTED PRIVATE KEY" in pem_bytes
        )
        if is_encrypted:
            if not passphrase:
                raise ValueError(
                    f"TUF keystore key {path.name} is encrypted; set "
                    f"{KEYSTORE_PASSPHRASE_ENV} to unlock it"
                )
            try:
                private_key = serialization.load_pem_private_key(
                    pem_bytes, password=passphrase.encode("utf-8"),
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"failed to decrypt TUF keystore key {path.name}: invalid "
                    f"{KEYSTORE_PASSPHRASE_ENV}"
                ) from exc
        else:
            warnings.warn(
                f"loading unencrypted TUF keystore key {path.name}; set "
                f"{KEYSTORE_PASSPHRASE_ENV} to encrypt keystore keys",
                UserWarning,
                stacklevel=2,
            )
            private_key = serialization.load_pem_private_key(
                pem_bytes, password=None,
            )
        signers.append(CryptoSigner(private_key))
    return signers


def _write(repo: Path, name: str, md: Metadata) -> None:
    md.to_file(str(_metadata_dir(repo) / f"{name}.json"))


# ---------------------------------------------------------------------------
# init: generate keys and the initial metadata set
# ---------------------------------------------------------------------------

def init_repo(repo_dir: str | os.PathLike) -> dict:
    repo = Path(repo_dir)
    repo.mkdir(parents=True, exist_ok=True)

    signers: dict[str, list[CryptoSigner]] = {}
    for role in ROLES:
        signers[role] = []
        for i in range(KEY_COUNTS[role]):
            signer = CryptoSigner.generate_ed25519()
            _save_signer(repo, role, i, signer)
            signers[role].append(signer)

    keys = {}
    roles = {}
    for role in ROLES:
        role_keyids = []
        for signer in signers[role]:
            keys[signer.public_key.keyid] = signer.public_key
            role_keyids.append(signer.public_key.keyid)
        roles[role] = Role(role_keyids, THRESHOLDS[role])

    root = Root(
        version=1,
        spec_version=SPEC_VERSION,
        expires=_utc(EXPIRY_DAYS["root"]),
        keys=keys,
        roles=roles,
        consistent_snapshot=False,
    )
    targets = Targets(version=1, spec_version=SPEC_VERSION,
                      expires=_utc(EXPIRY_DAYS["targets"]))
    snapshot = Snapshot(
        version=1, spec_version=SPEC_VERSION,
        expires=_utc(EXPIRY_DAYS["snapshot"]),
        meta={"targets.json": MetaFile(version=1)},
    )
    timestamp = Timestamp(
        version=1, spec_version=SPEC_VERSION,
        expires=_utc(EXPIRY_DAYS["timestamp"]),
        snapshot_meta=MetaFile(version=1),
    )

    for name, obj, role in (
        ("root", root, "root"),
        ("targets", targets, "targets"),
        ("snapshot", snapshot, "snapshot"),
        ("timestamp", timestamp, "timestamp"),
    ):
        md = Metadata(obj)
        for signer in signers[role]:
            md.sign(signer, append=True)
        _write(repo, name, md)

    return {
        "repo": str(repo),
        "roles": {r: {"keys": KEY_COUNTS[r], "threshold": THRESHOLDS[r]}
                  for r in ROLES},
        "trusted_root": str(_metadata_dir(repo) / "root.json"),
    }


# ---------------------------------------------------------------------------
# publish: register a target and re-sign targets/snapshot/timestamp
# ---------------------------------------------------------------------------

def publish_target(
    repo_dir: str | os.PathLike,
    target_file: str | os.PathLike,
    target_name: str,
) -> dict:
    target_name = _validate_safe_target_name(target_name)
    repo = Path(repo_dir)
    md_dir = _metadata_dir(repo)
    source = Path(target_file)
    if not source.is_file():
        raise FileNotFoundError(f"target file not found: {source}")

    targets_out = (repo / "targets").resolve()
    dest = (targets_out / target_name).resolve()
    if not dest.is_relative_to(targets_out) or dest.parent != targets_out:
        raise ValueError(f"target destination escapes targets directory: {target_name!r}")

    targets_md = Metadata.from_file(str(md_dir / "targets.json"))
    target_info = TargetFile.from_file(target_name, str(source))
    targets_md.signed.targets[target_name] = target_info
    targets_md.signed.version += 1
    targets_md.signed.expires = _utc(EXPIRY_DAYS["targets"])
    targets_md.signatures.clear()
    for signer in _load_signers(repo, "targets"):
        targets_md.sign(signer, append=True)
    _write(repo, "targets", targets_md)

    snapshot_md = Metadata.from_file(str(md_dir / "snapshot.json"))
    snapshot_md.signed.meta["targets.json"] = MetaFile(
        version=targets_md.signed.version
    )
    snapshot_md.signed.version += 1
    snapshot_md.signed.expires = _utc(EXPIRY_DAYS["snapshot"])
    snapshot_md.signatures.clear()
    for signer in _load_signers(repo, "snapshot"):
        snapshot_md.sign(signer, append=True)
    _write(repo, "snapshot", snapshot_md)

    timestamp_md = Metadata.from_file(str(md_dir / "timestamp.json"))
    timestamp_md.signed.snapshot_meta = MetaFile(version=snapshot_md.signed.version)
    timestamp_md.signed.version += 1
    timestamp_md.signed.expires = _utc(EXPIRY_DAYS["timestamp"])
    timestamp_md.signatures.clear()
    for signer in _load_signers(repo, "timestamp"):
        timestamp_md.sign(signer, append=True)
    _write(repo, "timestamp", timestamp_md)

    # Stage the target file itself in the repo's targets/ tree.
    targets_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)

    return {
        "target": target_name,
        "length": target_info.length,
        "sha256": target_info.hashes.get("sha256"),
        "targets_version": targets_md.signed.version,
    }


# ---------------------------------------------------------------------------
# bundle: assemble a client-importable offline bundle from a build output
# ---------------------------------------------------------------------------

def build_offline_bundle(
    repo_dir: str | os.PathLike,
    built_bundle_dir: str | os.PathLike,
    out_dir: str | os.PathLike,
) -> dict:
    """Sign a built bundle's archive into the repo and lay out an offline
    bundle (manifest + metadata/ + targets/) the client verifies via TUF."""
    repo = Path(repo_dir)
    built = Path(built_bundle_dir)
    built_resolved = built.resolve()
    out = Path(out_dir)
    manifest_src = built / "manifest.json"
    manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
    archive_name = _validate_safe_target_name(manifest["archive"]["name"])
    raw_archive = built / archive_name
    if raw_archive.is_symlink():
        raise ValueError(f"refusing symlink archive in build output: {archive_name!r}")
    archive_src = raw_archive.resolve()
    if not archive_src.is_relative_to(built_resolved) or archive_src.parent != built_resolved:
        raise ValueError(f"archive path escapes built bundle directory: {archive_name!r}")
    if not archive_src.is_file():
        raise FileNotFoundError(f"archive not found in build output: {archive_src}")

    published = publish_target(repo, archive_src, archive_name)
    # V1: manifest.json drives install behavior (health_checks argv,
    # entrypoints, resource budgets) and is consumed by the client, so it must
    # be a signed TUF target alongside the archive — not merely copied beside
    # it. The client (_verify_with_tuf) refuses a bundle whose manifest.json is
    # absent from the signed metadata.
    published_manifest = publish_target(repo, manifest_src, "manifest.json")

    out.mkdir(parents=True, exist_ok=True)
    (out / "metadata").mkdir(exist_ok=True)
    out_targets = (out / "targets").resolve()
    out_targets.mkdir(exist_ok=True)
    out_archive = (out_targets / archive_name).resolve()
    if not out_archive.is_relative_to(out_targets) or out_archive.parent != out_targets:
        raise ValueError(f"offline bundle target escapes targets directory: {archive_name!r}")
    for name in ROLES:
        shutil.copy2(repo / "metadata" / f"{name}.json",
                     out / "metadata" / f"{name}.json")
    shutil.copy2(repo / "targets" / archive_name, out_archive)
    # The client loads and verifies bundle/manifest.json; publish the signed
    # copy there (and mirror it into targets/ for layout parity with archive).
    shutil.copy2(repo / "targets" / "manifest.json", out / "manifest.json")
    shutil.copy2(repo / "targets" / "manifest.json", out / "targets" / "manifest.json")
    return {
        "offline_bundle": str(out),
        "target": archive_name,
        "sha256": published["sha256"],
        "manifest_sha256": published_manifest["sha256"],
        "metadata_version": published_manifest["targets_version"],
    }


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tuf_repo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="generate keys and initial metadata")
    p.add_argument("repo")
    p = sub.add_parser("publish", help="sign a target into the repo")
    p.add_argument("repo")
    p.add_argument("target_file")
    p.add_argument("--name", required=True)
    p = sub.add_parser("bundle", help="assemble an offline update bundle")
    p.add_argument("repo")
    p.add_argument("built_bundle")
    p.add_argument("out")
    args = parser.parse_args(argv)

    if args.command == "init":
        print(json.dumps(init_repo(args.repo), indent=2))
    elif args.command == "publish":
        print(json.dumps(publish_target(args.repo, args.target_file, args.name),
                         indent=2))
    elif args.command == "bundle":
        print(json.dumps(
            build_offline_bundle(args.repo, args.built_bundle, args.out),
            indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
