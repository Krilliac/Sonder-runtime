"""Emergency selfmod recovery; intentionally imports no Sonder modules."""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from pathlib import PurePosixPath


MAX_RECOVERY_FILES = 256
MAX_RECOVERY_PATH_LENGTH = 4096
MAX_RECOVERY_FILE_BYTES = 512 * 1024 * 1024


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source, target, mode=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=target.name + ".emergency-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temp, int(mode))
        os.replace(temp, target)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _within(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_manifest(manifest_path, manifest):
    """Validate every recovery input before changing the repository.

    Emergency recovery must not partially restore a bundle that fails on a
    later record.  The manifest checksum authenticates the bytes as a bundle,
    while this bounded structural pass prevents malformed or out-of-bundle
    paths from becoming filesystem operations.
    """
    if not isinstance(manifest, dict):
        raise RuntimeError("manifest must be an object")
    root_value = manifest.get("repository_root")
    records = manifest.get("files")
    if not isinstance(root_value, str) or not root_value:
        raise RuntimeError("manifest repository root is invalid")
    if not isinstance(records, list) or not records:
        raise RuntimeError("manifest records no files; refusing recovery")
    if len(records) > MAX_RECOVERY_FILES:
        raise RuntimeError("manifest exceeds recovery file bound")

    root = Path(root_value).expanduser().resolve()
    bundle_root = manifest_path.parent.resolve()
    if not root.is_dir():
        raise RuntimeError("manifest repository root is not a directory")

    validated = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("manifest contains a malformed file record")
        relative = record.get("path")
        existed = record.get("existed_before")
        if not isinstance(relative, str) or not relative or len(relative) > MAX_RECOVERY_PATH_LENGTH:
            raise RuntimeError("manifest contains an invalid path")
        if "\\" in relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise RuntimeError("manifest path is not a relative repository path")
        if relative in seen:
            raise RuntimeError("manifest contains duplicate path %s" % relative)
        seen.add(relative)
        if not isinstance(existed, bool):
            raise RuntimeError("manifest existence flag is invalid for %s" % relative)
        target = (root / relative).resolve()
        if not _within(target, root):
            raise RuntimeError("manifest path escapes repository")

        if existed:
            backup_value = record.get("backup_path")
            backup_digest = record.get("sha256_backup")
            before_digest = record.get("sha256_before")
            size_before = record.get("size_before")
            if not isinstance(backup_value, str) or not isinstance(backup_digest, str) or not isinstance(before_digest, str):
                raise RuntimeError("manifest backup metadata is invalid for %s" % relative)
            if not isinstance(size_before, int) or size_before < 0 or size_before > MAX_RECOVERY_FILE_BYTES:
                raise RuntimeError("manifest file size is outside recovery bounds for %s" % relative)
            backup = Path(backup_value).expanduser().resolve()
            if not _within(backup, bundle_root) or backup == bundle_root or not backup.is_file():
                raise RuntimeError("backup path escapes recovery bundle for %s" % relative)
            if backup.stat().st_size != size_before:
                raise RuntimeError("backup size mismatch for %s" % relative)
            if sha(backup) != backup_digest or backup_digest != before_digest:
                raise RuntimeError("backup checksum failed for %s" % relative)
        else:
            if any(record.get(key) not in (None, "") for key in ("backup_path", "sha256_backup", "sha256_before")):
                raise RuntimeError("new file record has backup metadata for %s" % relative)
        validated.append((record, target))
    return root, validated


def restore(manifest_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    checksum_path = manifest_path.with_name("manifest.sha256")
    if not checksum_path.is_file() or sha(manifest_path) != checksum_path.read_text(encoding="ascii").strip():
        raise RuntimeError("manifest checksum failed; refusing recovery")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root, validated = _validated_manifest(manifest_path, manifest)
    for record, target in validated:
        if record["existed_before"]:
            backup = Path(record["backup_path"])
            atomic_copy(backup, target, record.get("mode_before"))
            if sha(target) != record["sha256_before"]:
                raise RuntimeError("restored checksum failed for %s" % record["path"])
        elif target.exists():
            target.unlink()
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="absolute path to backup manifest.json")
    args = parser.parse_args(argv)
    root = restore(args.manifest)
    print("Restored exact selfmod backup into %s" % root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
