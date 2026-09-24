"""Verify desktop release artifacts and emit portable integrity metadata."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

EXPECTED_ARTIFACTS = (
    "sonder-runtime-android.apk",
    "sonder-runtime-linux-x64.tar.gz",
    "sonder-runtime-windows-x64.zip",
    "sonder-runtime-macos.zip",
)
OUTPUTS = (
    "sonder-runtime-sbom.cdx.json",
    "sonder-runtime-provenance.intoto.json",
    "SHA256SUMS",
)
MAX_NESTED_ZIP_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_BUILD_STAMP_BYTES = 64 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRY_BYTES = 512 * 1024 * 1024
REQUIRED_SYSTEM_FILES = frozenset({
    "LICENSE", "sonder_build.json", "server.py", "bootstrap-engine.sh",
    "bootstrap-engine.cmd", "requirements-runtime.txt",
})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str, *, tar: bool = False) -> str:
    if tar:
        name = name.removeprefix("./")
    name = name.rstrip("/")
    if (
        not name or name.startswith("/") or "\\" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
        or ":" in name.split("/")[0]
    ):
        raise ValueError(f"unsafe release archive path: {name!r}")
    return name


def _validate_symlink(name: str, target: str) -> str:
    if not target or target.startswith("/") or "\\" in target or ":" in target:
        raise ValueError(f"unsafe release archive symlink: {name}")
    destination = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    # macOS's Flutter frameworks have legitimate relative symlinks; they must
    # still resolve lexically inside their application bundle.
    if not destination.startswith("Sonder Runtime.app/") or "\0" in target:
        raise ValueError(f"release archive symlink escapes application: {name}")
    return destination


def _zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    links: dict[str, str] = {}
    total = 0
    for info in archive.infolist():
        name = _safe_name(info.filename)
        if name in entries or len(entries) >= MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"duplicate or excessive release archive entries: {name}")
        total += info.file_size
        if info.file_size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
            raise ValueError(f"release archive exceeds size bound: {name}")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            if info.file_size > 512:
                raise ValueError(f"release archive symlink is too large: {name}")
            links[name] = _validate_symlink(name, archive.read(info).decode("utf-8"))
        elif kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"unsupported release archive entry type: {name}")
        entries[name] = info
    if not entries:
        raise ValueError("release archive has no entries")
    for name, destination in links.items():
        for _ in range(16):
            parts = destination.split("/")
            redirect = next(
                ("/".join(parts[:index]) for index in range(1, len(parts) + 1)
                 if "/".join(parts[:index]) in links),
                None,
            )
            if redirect is None:
                break
            suffix = destination[len(redirect):].lstrip("/")
            destination = posixpath.normpath(posixpath.join(links[redirect], suffix))
            if not destination.startswith("Sonder Runtime.app/"):
                raise ValueError(f"release archive symlink escapes application: {name}")
        else:
            raise ValueError(f"release archive symlink cycle: {name}")
        if destination not in entries and not any(item.startswith(destination + "/") for item in entries):
            raise ValueError(f"release archive symlink target is missing: {name}")
    return entries


def _tar_entries(archive: tarfile.TarFile) -> tuple[dict[str, tarfile.TarInfo], dict[str, tuple[int, str]], dict[str, bytes]]:
    entries: dict[str, tarfile.TarInfo] = {}
    digests: dict[str, tuple[int, str]] = {}
    samples: dict[str, bytes] = {}
    total = 0
    for info in archive:
        # GNU tar emits a harmless root-directory header for `tar -czf ... .`.
        if info.name in (".", "./") and info.isdir():
            continue
        name = _safe_name(info.name, tar=True)
        if name in entries or len(entries) >= MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"duplicate or excessive release archive entries: {name}")
        if not (info.isfile() or info.isdir()):
            raise ValueError(f"unsupported release tar entry type: {name}")
        total += info.size
        if info.size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
            raise ValueError(f"release archive exceeds size bound: {name}")
        entries[name] = info
        if info.isfile():
            digest = hashlib.sha256()
            size = 0
            captured = bytearray()
            capture = name in {
                "local-system/PACKAGE-MANIFEST.json", "local-system/sonder_build.json",
                "data/flutter_assets/assets/local-system.zip",
            }
            limit = MAX_NESTED_ZIP_BYTES if name.endswith("local-system.zip") else MAX_MANIFEST_BYTES
            with archive.extractfile(info) as incoming:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    if capture:
                        if len(captured) + len(chunk) > limit:
                            raise ValueError(f"release archive embedded payload exceeds size bound: {name}")
                        captured.extend(chunk)
            if size != info.size:
                raise ValueError(f"truncated release archive member: {name}")
            digests[name] = (size, digest.hexdigest())
            if capture:
                samples[name] = bytes(captured)
    if not entries:
        raise ValueError("release archive has no entries")
    return entries, digests, samples


def _require_file(entries: dict, name: str) -> None:
    info = entries.get(name)
    if info is None:
        raise ValueError(f"required release file is missing: {name}")
    if isinstance(info, zipfile.ZipInfo):
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"required release file is not regular: {name}")
        size = info.file_size
    else:
        if not info.isfile():
            raise ValueError(f"required release file is not regular: {name}")
        size = info.size
    if size <= 0:
        raise ValueError(f"required release file is empty: {name}")


def _digest_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(chunk)
        if size > MAX_ENTRY_BYTES:
            raise ValueError("release archive member exceeds size bound")
        digest.update(chunk)
    return size, digest.hexdigest()


def _verify_manifest(
    entries: dict,
    read: Callable,
    *,
    prefix: str,
    version: str | None,
    revision: str | None,
    digests: dict[str, tuple[int, str]] | None = None,
) -> None:
    manifest_name = prefix + "PACKAGE-MANIFEST.json"
    _require_file(entries, manifest_name)
    if (entries[manifest_name].file_size if isinstance(entries[manifest_name], zipfile.ZipInfo)
        else entries[manifest_name].size) > MAX_MANIFEST_BYTES:
        raise ValueError("local-system package manifest exceeds size bound")
    try:
        manifest = json.loads(read(manifest_name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("local-system package manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise ValueError("local-system package manifest schema is unsupported")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("local-system package manifest files are invalid")
    described = set()
    for record in records:
        if not isinstance(record, dict):
            # The API accepts archive bytes; this is malformed external data.
            raise ValueError("invalid local-system package manifest record")  # noqa: TRY004
        name = _safe_name(record.get("path") if isinstance(record.get("path"), str) else "")
        if name in described or name == "PACKAGE-MANIFEST.json":
            raise ValueError(f"duplicate or self-listed package manifest entry: {name}")
        described.add(name)
        full_name = prefix + name
        if name in REQUIRED_SYSTEM_FILES:
            _require_file(entries, full_name)
        if full_name not in entries:
            raise ValueError(f"manifest-listed release file is missing: {full_name}")
        info = entries[full_name]
        if (isinstance(info, zipfile.ZipInfo) and (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16))) or (isinstance(info, tarfile.TarInfo) and not info.isfile()):
            raise ValueError(f"manifest-listed release file is not regular: {full_name}")
        expected_size = record.get("size")
        expected_hash = record.get("sha256")
        if (type(expected_size) is not int or expected_size < 0 or
            not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise ValueError(f"invalid manifest digest or size: {full_name}")
        if digests is not None:
            size, digest = digests[full_name]
        else:
            with read(full_name, stream=True) as stream:
                size, digest = _digest_stream(stream)
        if size != expected_size or digest != expected_hash:
            raise ValueError(f"local-system manifest mismatch: {full_name}")
    if not REQUIRED_SYSTEM_FILES <= described:
        missing = sorted(REQUIRED_SYSTEM_FILES - described)
        raise ValueError(f"local-system required file or LICENSE is missing: {', '.join(missing)}")
    actual = {
        name.removeprefix(prefix) for name, info in entries.items()
        if name.startswith(prefix) and name != manifest_name
        and not (info.is_dir() if isinstance(info, zipfile.ZipInfo) else info.isdir())
    }
    if described != actual:
        raise ValueError("local-system package manifest does not cover every payload file")
    if version is not None or revision is not None:
        stamp_name = prefix + "sonder_build.json"
        stamp_info = entries[stamp_name]
        if (stamp_info.file_size if isinstance(stamp_info, zipfile.ZipInfo)
            else stamp_info.size) > MAX_BUILD_STAMP_BYTES:
            raise ValueError("local-system build identity exceeds size bound")
        try:
            stamp = json.loads(read(stamp_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("local-system build identity is invalid") from exc
        if not isinstance(stamp, dict) or (version is not None and stamp.get("version") != version) or (revision is not None and stamp.get("commit_sha") != revision):
            raise ValueError("local-system build identity does not match release")


def _verify_nested(archive: zipfile.ZipFile, entries: dict, name: str,
                   *, version: str | None, revision: str | None) -> None:
    _require_file(entries, name)
    info = entries[name]
    if info.file_size > MAX_NESTED_ZIP_BYTES:
        raise ValueError(f"nested local-system.zip is too large: {name}")
    try:
        with zipfile.ZipFile(io.BytesIO(archive.read(info))) as nested:
            nested_entries = _zip_entries(nested)
            bad = nested.testzip()
            if bad:
                raise ValueError(f"corrupt nested local-system.zip member: {bad}")
            _verify_manifest(nested_entries, lambda key, stream=False: nested.open(nested_entries[key]) if stream else nested.read(key),
                             prefix="local-system/", version=version, revision=revision)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"nested local-system.zip is invalid: {name}") from exc


def _verify_zip(path: Path, *, version: str | None, revision: str | None) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = _zip_entries(archive)
        if path.name.endswith(".apk"):
            for name in ("AndroidManifest.xml", "classes.dex"):
                _require_file(entries, name)
            if not any(name.startswith("lib/") and name.endswith("/libapp.so") and info.file_size > 0
                       for name, info in entries.items()):
                raise ValueError("required Android libapp.so is missing")
            _verify_nested(archive, entries, "assets/flutter_assets/assets/local-system.zip",
                           version=version, revision=revision)
        elif "windows" in path.name:
            for name in ("sonder.exe", "flutter_windows.dll"):
                _require_file(entries, name)
            _verify_manifest(entries, lambda key, stream=False: archive.open(entries[key]) if stream else archive.read(key),
                             prefix="local-system/", version=version, revision=revision)
            _verify_nested(archive, entries, "data/flutter_assets/assets/local-system.zip",
                           version=version, revision=revision)
        else:
            prefix = "Sonder Runtime.app/Contents/"
            for name in ("Info.plist", "MacOS/sonder", "Frameworks/App.framework/Versions/A/App"):
                _require_file(entries, prefix + name)
            _verify_manifest(entries, lambda key, stream=False: archive.open(entries[key]) if stream else archive.read(key),
                             prefix=prefix + "Resources/local-system/", version=version, revision=revision)
            _verify_nested(archive, entries, prefix + "Frameworks/App.framework/Versions/A/Resources/flutter_assets/assets/local-system.zip",
                           version=version, revision=revision)
        bad = archive.testzip()
        if bad:
            raise ValueError(f"corrupt release archive member: {bad}")


def _verify_linux(path: Path, *, version: str | None, revision: str | None) -> None:
    # Compressed tar permits expensive backward seeks; process every member
    # exactly once in stream order and retain only small metadata and payloads.
    with tarfile.open(path, "r|gz") as archive:
        entries, digests, samples = _tar_entries(archive)
    # tarfile stops at tar's zero block; gzip's CRC and trailer may remain
    # unread. A bounded sequential drain validates the entire compressed file.
    with gzip.open(path, "rb") as stream:
        total = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES + MAX_ARCHIVE_ENTRIES * 4096:
                raise ValueError("release archive expands beyond size bound")
    for name in ("sonder", "lib/libapp.so"):
        _require_file(entries, name)
    def read(name: str, *, stream: bool = False):
        if stream:
            raise ValueError("sequential tar entries cannot be reopened")
        return samples[name]
    _verify_manifest(entries, read, prefix="local-system/", version=version,
                     revision=revision, digests=digests)
    nested_name = "data/flutter_assets/assets/local-system.zip"
    _require_file(entries, nested_name)
    try:
        with zipfile.ZipFile(io.BytesIO(samples[nested_name])) as nested:
            nested_entries = _zip_entries(nested)
            bad = nested.testzip()
            if bad:
                raise ValueError(f"corrupt nested local-system.zip member: {bad}")
            _verify_manifest(nested_entries, lambda key, stream=False: nested.open(nested_entries[key]) if stream else nested.read(key),
                             prefix="local-system/", version=version, revision=revision)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"nested local-system.zip is invalid: {nested_name}") from exc


def _verify_artifact(path: Path, *, version: str | None = None,
                     revision: str | None = None) -> None:
    try:
        if path.name.endswith(".tar.gz"):
            _verify_linux(path, version=version, revision=revision)
        else:
            _verify_zip(path, version=version, revision=revision)
    except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, UnicodeDecodeError) as exc:
        raise ValueError(f"artifact is not a valid release archive: {path.name}") from exc


def discover_artifacts(root: Path, *, version: str | None = None,
                       revision: str | None = None) -> list[Path]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("artifact root must be a directory")
    artifacts = []
    for name in EXPECTED_ARTIFACTS:
        matches = [
            path for path in root.rglob(name)
            if path.is_file() and not path.is_symlink()
        ]
        if len(matches) != 1:
            raise ValueError(f"required artifact {name} found {len(matches)} times; expected exactly one")
        artifact = matches[0].resolve(strict=True)
        if root not in artifact.parents:
            raise ValueError(f"artifact escapes release root: {name}")
        _verify_artifact(artifact, version=version, revision=revision)
        artifacts.append(artifact)
    return artifacts


def _json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _subjects(artifacts: list[Path]) -> list[dict]:
    return [
        {"name": path.name, "digest": {"sha256": sha256_file(path)}}
        for path in sorted(artifacts, key=lambda item: item.name)
    ]


def generate_metadata(
    root: Path,
    *,
    version: str,
    revision: str,
    source_uri: str,
    workflow_uri: str,
    invocation_id: str,
    created: str | None = None,
) -> dict[str, Path]:
    version = version.strip()
    revision = revision.strip().lower()
    if not version or any(ord(char) < 32 for char in version):
        raise ValueError("version must be a non-empty printable value")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full 40-character Git commit SHA")
    artifacts = discover_artifacts(root, version=version, revision=revision)
    subjects = _subjects(artifacts)
    created = created or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    serial_seed = "\n".join(item["digest"]["sha256"] for item in subjects)

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "timestamp": created,
            "component": {
                "type": "application",
                "name": "Sonder Runtime desktop release",
                "version": version,
                "properties": [
                    {"name": "sonder:source_revision", "value": revision},
                    {"name": "sonder:license_verified", "value": "true"},
                ],
            },
        },
        "components": [
            {
                "type": "file",
                "name": item["name"],
                "version": version,
                "hashes": [{"alg": "SHA-256", "content": item["digest"]["sha256"]}],
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            }
            for item in subjects
        ],
    }
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": (
                    source_uri.rstrip("/")
                    + "/.github/workflows/build-apps.yml@"
                    + revision
                ),
                "externalParameters": {"version": version, "source": source_uri},
                "internalParameters": {"revision": revision, "workflow": workflow_uri},
                "resolvedDependencies": [
                    {"uri": source_uri, "digest": {"gitCommit": revision}}
                ],
            },
            "runDetails": {
                "builder": {"id": workflow_uri},
                "metadata": {"invocationId": invocation_id},
            },
        },
    }

    root = root.resolve()
    staged = {
        OUTPUTS[0]: _json_bytes(sbom),
        OUTPUTS[1]: _json_bytes(provenance),
    }
    checksummed = [(item["name"], item["digest"]["sha256"]) for item in subjects]
    checksummed.extend((name, hashlib.sha256(data).hexdigest()) for name, data in staged.items())
    staged[OUTPUTS[2]] = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(checksummed)
    ).encode("ascii")

    temp_dir = Path(tempfile.mkdtemp(prefix=".release-integrity-", dir=str(root)))
    try:
        for name, data in staged.items():
            (temp_dir / name).write_bytes(data)
        for name in OUTPUTS:
            os.replace(temp_dir / name, root / name)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return {name: root / name for name in OUTPUTS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--workflow-uri", required=True)
    parser.add_argument("--invocation-id", required=True)
    args = parser.parse_args()
    try:
        outputs = generate_metadata(
            args.root,
            version=args.version,
            revision=args.revision,
            source_uri=args.source_uri,
            workflow_uri=args.workflow_uri,
            invocation_id=args.invocation_id,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for path in outputs.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
