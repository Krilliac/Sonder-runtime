"""Verify desktop release artifacts and emit portable integrity metadata."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_license_in_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if Path(info.filename).name == "LICENSE" and not info.is_dir():
                    return True
                if info.filename.endswith("local-system.zip"):
                    if info.file_size > MAX_NESTED_ZIP_BYTES:
                        raise ValueError(f"nested local-system.zip is too large: {path.name}")
                    with archive.open(info) as nested_stream:
                        nested = nested_stream.read(MAX_NESTED_ZIP_BYTES + 1)
                    if len(nested) > MAX_NESTED_ZIP_BYTES:
                        raise ValueError(f"nested local-system.zip is too large: {path.name}")
                    with zipfile.ZipFile(io.BytesIO(nested)) as local_system:
                        if any(
                            Path(item.filename).name == "LICENSE" and not item.is_dir()
                            for item in local_system.infolist()
                        ):
                            return True
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"artifact is not a valid ZIP-compatible archive: {path.name}") from exc
    return False


def _has_license(path: Path) -> bool:
    if path.name.endswith(".tar.gz"):
        try:
            with tarfile.open(path, "r:gz") as archive:
                return any(Path(item.name).name == "LICENSE" and item.isfile() for item in archive)
        except (OSError, tarfile.TarError) as exc:
            raise ValueError(f"artifact is not a valid tar.gz archive: {path.name}") from exc
    return _has_license_in_zip(path)


def discover_artifacts(root: Path) -> list[Path]:
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
        if not _has_license(artifact):
            raise ValueError(f"required LICENSE is missing from artifact: {name}")
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
    artifacts = discover_artifacts(root)
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
                "buildType": "https://github.com/Krilliac/Sonder-runtime/.github/workflows/build-apps.yml@v1",
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
