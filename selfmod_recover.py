"""Emergency selfmod recovery; intentionally imports no Trilobite modules."""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


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


def restore(manifest_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    checksum_path = manifest_path.with_name("manifest.sha256")
    if not checksum_path.is_file() or sha(manifest_path) != checksum_path.read_text(encoding="ascii").strip():
        raise RuntimeError("manifest checksum failed; refusing recovery")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(manifest["repository_root"]).resolve()
    for record in manifest["files"]:
        target = (root / record["path"]).resolve()
        if root not in target.parents:
            raise RuntimeError("manifest path escapes repository")
        if record["existed_before"]:
            backup = Path(record["backup_path"])
            if not backup.is_file() or sha(backup) != record["sha256_backup"]:
                raise RuntimeError("backup checksum failed for %s" % record["path"])
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
