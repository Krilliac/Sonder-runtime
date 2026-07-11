"""Create the privacy-safe local-system payload shipped with desktop builds."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine_bundle  # noqa: E402

ALLOWED_DIRS = {
    "contrib",
    "docs",
    "games",
    "proposals",
    "scripts",
    "seed",
}
ALLOWED_SUFFIXES = {
    ".cmd",
    ".ini",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PRIVATE_FILES = {
    "emotion_vectors.json",
    "file_roots.local",
    "generated_tasks.jsonl",
    "lessons.jsonl",
    "memory.db",
    "memory.db-shm",
    "memory.db-wal",
    "permissions.json",
    "system_profile.md",
    "workflows.json",
}
REQUIRED_FILES = {
    "autopilot_controller.py",
    "autopilot_store.py",
    "artifact_grounding.py",
    "assetgen.py",
    "bootstrap-engine.cmd",
    "bootstrap-engine.sh",
    "bootstrap_engine.py",
    "creative_router.py",
    "engine_bundle.py",
    "endless-train.sh",
    "game_forge.py",
    "fleet_store.py",
    "learning_health.py",
    "media_assets.py",
    "model_assets.py",
    "ooxml_assets.py",
    "reloadable_mcp.py",
    "runtime_policy.py",
    "server.py",
    "setup_alias.py",
    "trilobite-headless.cmd",
    "trilobite-headless.sh",
    "trilobite_headless.py",
    "trilobite-runtime.cmd",
    "trilobite-runtime.sh",
    "web_intents.py",
    "trilobite-serve.cmd",
    "trilobite-serve.sh",
    "trilobite_serve.py",
}
EXACT_OUTPUTS = (
    Path("app/build/local-system"),
    Path("dist/local-system"),
)
EXACT_ZIP_OUTPUTS = (
    Path("app/assets/local-system.zip"),
    Path("dist/local-system.zip"),
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
)
HOME_PATH_PATTERNS = (
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]+Users[\\/]+"
        r"[A-Za-z0-9._-]+(?:[\\/]|$)"
    ),
    re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+(?:/|$)"),
)


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _assert_no_reparse(path: Path, stop: Path) -> None:
    try:
        relative = path.relative_to(stop)
    except ValueError as exc:
        raise ValueError("package path must stay inside the repository") from exc
    current = stop
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise ValueError("package paths may not traverse symlinks or junctions")


def _assert_no_reparse_tree(path: Path, label: str) -> None:
    if _is_reparse(path):
        raise ValueError(f"{label} may not be a symlink or junction")
    if not path.exists():
        return
    pending = [path]
    while pending:
        current = pending.pop()
        for entry in os.scandir(current):
            child = Path(entry.path)
            if _is_reparse(child):
                raise ValueError(f"{label} contains a symlink or junction: {child.name}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)


def _validate_destination(
    path: Path,
    allowed_paths: tuple[Path, ...],
    label: str,
) -> Path:
    root = ROOT.resolve()
    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError(f"{label} may not contain parent traversal")
    if not raw.is_absolute():
        raw = root / raw
    lexical = Path(os.path.abspath(raw))
    allowed = tuple(Path(os.path.abspath(root / relative)) for relative in allowed_paths)
    if not any(_same_path(lexical, candidate) for candidate in allowed):
        names = ", ".join(relative.as_posix() for relative in allowed_paths)
        raise ValueError(f"{label} must be exactly one of: {names}")
    _assert_no_reparse(lexical, root)
    resolved = lexical.resolve(strict=False)
    if not _same_path(resolved, lexical):
        raise ValueError(f"{label} may not traverse a symlink or junction")
    return lexical


def validate_output_path(path: Path) -> Path:
    return _validate_destination(path, EXACT_OUTPUTS, "--out")


def validate_zip_path(path: Path) -> Path:
    return _validate_destination(path, EXACT_ZIP_OUTPUTS, "--zip")


def _included(rel: Path) -> bool:
    parts = rel.parts
    if not parts or any(part.startswith(".") for part in parts):
        return False
    name = rel.name.lower()
    if name in PRIVATE_FILES or name.startswith("modelfile."):
        return False
    if name.endswith((".db", ".db-wal", ".db-shm", ".local", ".json", ".jsonl")):
        return False
    if rel.suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    return len(parts) == 1 or parts[0] in ALLOWED_DIRS


def _tracked_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("git ls-files is required to build a fail-closed payload") from exc
    paths = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(os.fsdecode(raw))
        if not _included(rel):
            continue
        source = ROOT / rel
        _assert_no_reparse(source, ROOT.resolve())
        if not source.exists() or not source.is_file():
            raise ValueError(f"tracked payload file is missing or not a file: {rel}")
        resolved = source.resolve(strict=True)
        if ROOT.resolve() not in resolved.parents:
            raise ValueError(f"tracked payload file escapes repository: {rel}")
        paths.append(source)
    return sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix())


def _privacy_scan_binary(path: Path) -> None:
    home = str(Path.home())
    needles = []
    if len(home) > 3:
        needles.extend(
            (
                home.encode("utf-8", errors="ignore").lower(),
                home.encode("utf-16-le", errors="ignore").lower(),
            )
        )
    overlap = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sample = overlap + chunk
            lowered = sample.lower()
            if any(needle and needle in lowered for needle in needles):
                raise ValueError(f"payload binary contains an absolute user-home path: {path.name}")
            overlap = sample[-512:]


def _privacy_scan(path: Path, *, allow_binary: bool = False) -> None:
    data = path.read_bytes() if path.stat().st_size <= 16 * 1024 * 1024 else b""
    if not data or b"\0" in data:
        if allow_binary:
            _privacy_scan_binary(path)
            return
        if not data:
            raise ValueError(f"payload text is too large to inspect safely: {path.name}")
        raise ValueError(f"payload text contains NUL bytes: {path.name}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if allow_binary:
            _privacy_scan_binary(path)
            return
        raise ValueError(f"payload text is not valid UTF-8: {path.name}") from exc
    home = str(Path.home())
    if len(home) > 3 and home.casefold() in text.casefold():
        raise ValueError(f"payload contains an absolute user-home path: {path.name}")
    if not allow_binary:
        for pattern in HOME_PATH_PATTERNS:
            if pattern.search(text):
                raise ValueError(f"payload contains an absolute user-home path: {path.name}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"payload contains a secret-like value: {path.name}")


def _engine_executables(stage: Path) -> set[str]:
    executable_paths = set()
    engine_root = stage / "engine"
    if not engine_root.is_dir():
        return executable_paths
    for manifest_path in engine_root.glob(f"*/{engine_bundle.MANIFEST_NAME}"):
        bundle = engine_bundle.load_engine_bundle(manifest_path.parent, verify_hashes=False)
        prefix = manifest_path.parent.relative_to(stage)
        for record in bundle.files:
            if record.executable:
                executable_paths.add((prefix / record.relative).as_posix())
    return executable_paths


def _write_manifest(stage: Path) -> None:
    engine_executables = _engine_executables(stage)
    files = []
    for path in sorted(stage.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == "PACKAGE-MANIFEST.json":
            continue
        relative = path.relative_to(stage)
        allow_binary = bool(relative.parts and relative.parts[0] == "engine")
        _privacy_scan(path, allow_binary=allow_binary)
        mode = 0o755 if relative.suffix == ".sh" or relative.as_posix() in engine_executables else 0o644
        files.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": engine_bundle.sha256_file(path),
                "mode": mode,
            }
        )
    manifest = {"schema": 1, "files": files}
    (stage / "PACKAGE-MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_engine_bundle(source: Path, stage: Path) -> None:
    bundle = engine_bundle.load_engine_bundle(source, verify_hashes=True)
    target_root = stage / "engine" / bundle.identity
    target_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(bundle.manifest_path, target_root / engine_bundle.MANIFEST_NAME)
    for record in bundle.files:
        source_path = bundle.root / record.relative
        target = target_root / record.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)


def copy_payload(dest: Path, engine_bundle_source: Path | None = None) -> None:
    dest = validate_output_path(dest)
    if dest.exists():
        if not dest.is_dir():
            raise ValueError("existing package destination is not a safe directory")
        _assert_no_reparse_tree(dest, "existing package destination")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = validate_output_path(dest)
    stage = Path(tempfile.mkdtemp(prefix=f".{dest.name}.stage-", dir=str(dest.parent)))
    backup = dest.with_name(f".{dest.name}.backup-{uuid.uuid4().hex}")
    moved_existing = False
    try:
        source_root = ROOT.resolve()
        stage_root = stage.resolve()
        for path in _tracked_files():
            try:
                rel = path.relative_to(ROOT)
            except ValueError as exc:
                raise ValueError(f"tracked path escapes repository: {path}") from exc
            if rel.is_absolute() or ".." in rel.parts or not _included(rel):
                continue
            if _is_reparse(path):
                raise ValueError(f"tracked payload file is a symlink or junction: {rel}")
            source = path.resolve(strict=True)
            if source_root not in source.parents:
                raise ValueError(f"tracked payload file escapes repository: {rel}")
            target = (stage / rel).resolve(strict=False)
            if stage_root not in target.parents:
                raise ValueError(f"payload destination escapes staging root: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if engine_bundle_source is not None:
            _copy_engine_bundle(engine_bundle_source, stage)
        (stage / "BUNDLED_SYSTEM_README.txt").write_text(
            "This folder is the bundled trilobite local system.\n"
            "Run bootstrap-engine.cmd for one-click local model setup on Windows.\n"
            "Run ./bootstrap-engine.sh on Linux or macOS.\n"
            "Run trilobite-headless.cmd or ./trilobite-headless.sh for the managed server.\n"
            "If engine/<platform>-<architecture>/ exists, setup verifies and uses its sealed\n"
            "Python, Ollama, and model payload without network access. Code-only packages use\n"
            "host runtimes and may install mcp or pull missing models on first setup.\n",
            encoding="utf-8",
        )
        missing = sorted(name for name in REQUIRED_FILES if not (stage / name).is_file())
        if missing:
            raise RuntimeError("payload is missing required files: " + ", ".join(missing))
        _write_manifest(stage)
        if dest.exists():
            if not dest.is_dir():
                raise ValueError("existing package destination is not a safe directory")
            _assert_no_reparse_tree(dest, "existing package destination")
            dest.rename(backup)
            moved_existing = True
        stage.rename(dest)
        if moved_existing:
            shutil.rmtree(backup)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if moved_existing and backup.exists() and not dest.exists():
            backup.rename(dest)
        raise


def _manifest_relative(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError("package manifest contains an unsafe path")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise ValueError("package manifest contains an unsafe path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ":" in pure.parts[0]:
        raise ValueError("package manifest contains an unsafe path")
    return Path(*pure.parts)


def _verified_manifest_files(src: Path) -> tuple[bytes, list[tuple[Path, Path, int]]]:
    manifest_path = src / "PACKAGE-MANIFEST.json"
    _assert_no_reparse(manifest_path, src)
    if not manifest_path.is_file() or _is_reparse(manifest_path):
        raise ValueError("package manifest is missing or unsafe")
    _privacy_scan(manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("package manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("package manifest has an unsupported schema")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("package manifest files must be a list")

    verified = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("package manifest contains an invalid record")
        rel = _manifest_relative(record.get("path"))
        key = rel.as_posix()
        if key == "PACKAGE-MANIFEST.json":
            raise ValueError("package manifest may not list itself")
        if key in seen:
            raise ValueError("package manifest contains duplicate paths")
        seen.add(key)
        target = src / rel
        _assert_no_reparse(target, src)
        if not target.is_file() or _is_reparse(target):
            raise ValueError(f"manifest-listed file is missing or unsafe: {key}")
        allow_binary = bool(rel.parts and rel.parts[0] == "engine")
        _privacy_scan(target, allow_binary=allow_binary)
        expected_size = record.get("size")
        expected_hash = record.get("sha256")
        mode = record.get("mode", 0o644)
        if mode not in (0o644, 0o755):
            raise ValueError(f"manifest mode is invalid: {key}")
        if type(expected_size) is not int or expected_size != target.stat().st_size:
            raise ValueError(f"manifest size mismatch: {key}")
        if (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or engine_bundle.sha256_file(target) != expected_hash
        ):
            raise ValueError(f"manifest hash mismatch: {key}")
        verified.append((rel, target, mode))
    missing = sorted(REQUIRED_FILES - seen)
    if missing:
        raise ValueError("package manifest is missing required files: " + ", ".join(missing))
    return manifest_bytes, verified


def zip_payload(src: Path, zip_path: Path) -> None:
    src = validate_output_path(src)
    if not src.is_dir():
        raise ValueError("package source must be an existing exact package output")
    _assert_no_reparse_tree(src, "package source")
    manifest_bytes, verified = _verified_manifest_files(src)
    zip_path = validate_zip_path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path = validate_zip_path(zip_path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{zip_path.name}.", dir=str(zip_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            manifest_info = zipfile.ZipInfo(
                (Path(src.name) / "PACKAGE-MANIFEST.json").as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(manifest_info, manifest_bytes)
            for rel, source, mode in sorted(verified, key=lambda item: item[0].as_posix()):
                arcname = (Path(src.name) / rel).as_posix()
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                with source.open("rb") as incoming, zf.open(info, "w") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        os.replace(temp_path, zip_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="app/build/local-system")
    parser.add_argument("--zip", default="")
    parser.add_argument(
        "--engine-bundle",
        default=os.environ.get("TRILOBITE_ENGINE_BUNDLE_SOURCE", ""),
        help="optional sealed platform engine bundle to include",
    )
    args = parser.parse_args()

    if Path(args.out).is_absolute():
        raise SystemExit("--out must be repository-relative")
    try:
        out = validate_output_path(ROOT / args.out)
        bundle_source = Path(args.engine_bundle).expanduser() if args.engine_bundle else None
        copy_payload(out, bundle_source)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.zip:
        if Path(args.zip).is_absolute():
            raise SystemExit("--zip must be repository-relative")
        try:
            zip_payload(out, ROOT / args.zip)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
