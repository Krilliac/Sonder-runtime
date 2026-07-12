"""Verified, platform-specific runtime bundles for offline Trilobite installs.

An engine bundle is optional. Lightweight source/app packages continue to use
the host's Python and Ollama installations, while a sealed bundle can provide
both runtimes and a complete Ollama model-store subset without network access.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform as platform_module
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "ENGINE-BUNDLE.json"
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
ENGINE_BUNDLE_ENV = "TRILOBITE_ENGINE_BUNDLE"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?")


@dataclass(frozen=True)
class BundleFile:
    relative: Path
    size: int
    sha256: str
    executable: bool = False


@dataclass(frozen=True)
class BundleModel:
    name: str
    manifest: Path
    min_ram_gb: float = 0.0


@dataclass(frozen=True)
class EngineBundle:
    root: Path
    manifest_path: Path
    platform: str
    architecture: str
    python_executable: Path
    ollama_executable: Path
    model_store: Path
    base_models: tuple[BundleModel, ...]
    embedding_model: BundleModel
    files: tuple[BundleFile, ...]
    manifest_sha256: str

    @property
    def identity(self) -> str:
        return f"{self.platform}-{self.architecture}"


def normalize_platform(value: str | None = None) -> str:
    raw = (value or platform_module.system()).strip().casefold()
    aliases = {
        "win32": "windows",
        "win64": "windows",
        "windows": "windows",
        "linux": "linux",
        "darwin": "macos",
        "mac": "macos",
        "macos": "macos",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError(f"unsupported engine-bundle platform: {value or raw}") from exc


def normalize_architecture(value: str | None = None) -> str:
    raw = (value or platform_module.machine()).strip().casefold().replace("-", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError(f"unsupported engine-bundle architecture: {value or raw}") from exc


def platform_bundle_name(
    platform_name: str | None = None,
    architecture: str | None = None,
) -> str:
    return f"{normalize_platform(platform_name)}-{normalize_architecture(architecture)}"


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _relative_path(raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError(f"{label} must be a non-empty POSIX relative path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"{label} contains an unsafe path")
    if ":" in pure.parts[0]:
        raise ValueError(f"{label} contains an unsafe drive or URI prefix")
    return Path(*pure.parts)


def _assert_safe_source(root: Path, target: Path, label: str) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the engine bundle") from exc
    current = root
    if _is_reparse(current):
        raise ValueError("engine bundle root may not be a symlink or junction")
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise ValueError(f"{label} traverses a symlink or junction")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> tuple[dict, bytes]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"engine bundle manifest is unavailable: {path}") from exc
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ValueError("engine bundle manifest has an unsafe size")
    data = path.read_bytes()
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("engine bundle manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("engine bundle manifest must be a JSON object")
    return value, data


def _model_spec(raw: object, label: str) -> BundleModel:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _MODEL_NAME_RE.fullmatch(name):
        raise ValueError(f"{label}.name is invalid")
    manifest = _relative_path(raw.get("manifest"), f"{label}.manifest")
    ram = raw.get("min_ram_gb", 0)
    if type(ram) not in (int, float) or not math.isfinite(ram):
        raise ValueError(f"{label}.min_ram_gb must be a finite number")
    ram = float(ram)
    if ram < 0 or ram > 1024:
        raise ValueError(f"{label}.min_ram_gb is outside the supported range")
    return BundleModel(name=name, manifest=manifest, min_ram_gb=ram)


def _validate_ollama_model(
    bundle_root: Path,
    model_store_relative: Path,
    model: BundleModel,
    records: dict[str, BundleFile],
) -> None:
    store_prefix = model_store_relative.parts
    if model.manifest.parts[: len(store_prefix)] != store_prefix:
        raise ValueError(f"model manifest for {model.name} is outside model_store")
    key = model.manifest.as_posix()
    if key not in records:
        raise ValueError(f"model manifest is not sealed in files: {key}")
    path = bundle_root / model.manifest
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Ollama model manifest is invalid: {model.name}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError(f"Ollama model manifest has an unsupported schema: {model.name}")
    objects = [manifest.get("config"), *(manifest.get("layers") or [])]
    if not objects or not isinstance(manifest.get("layers"), list):
        raise ValueError(f"Ollama model manifest is incomplete: {model.name}")
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError(f"Ollama model manifest has an invalid object: {model.name}")
        digest = item.get("digest")
        size = item.get("size")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError(f"Ollama model manifest has an invalid digest: {model.name}")
        digest_hex = digest.removeprefix("sha256:")
        if not _SHA256_RE.fullmatch(digest_hex):
            raise ValueError(f"Ollama model manifest has an invalid digest: {model.name}")
        blob = model_store_relative / "blobs" / f"sha256-{digest_hex}"
        blob_record = records.get(blob.as_posix())
        if blob_record is None:
            raise ValueError(f"Ollama model blob is not sealed in files: {blob.as_posix()}")
        if blob_record.sha256 != digest_hex:
            raise ValueError(f"Ollama model blob digest disagrees with manifest: {blob.as_posix()}")
        if type(size) is not int or size != blob_record.size:
            raise ValueError(f"Ollama model blob size disagrees with manifest: {blob.as_posix()}")


def load_engine_bundle(
    path: str | os.PathLike[str],
    *,
    verify_hashes: bool = True,
    expected_platform: str | None = None,
    expected_architecture: str | None = None,
) -> EngineBundle:
    """Load and fail-closed validate an engine bundle directory or manifest."""
    supplied = Path(path).expanduser().absolute()
    manifest_path = supplied if supplied.name == MANIFEST_NAME else supplied / MANIFEST_NAME
    root = manifest_path.parent.absolute()
    _assert_safe_source(root, manifest_path, "engine bundle manifest")
    if not manifest_path.is_file() or _is_reparse(manifest_path):
        raise ValueError("engine bundle manifest is missing or unsafe")
    raw, manifest_bytes = _read_manifest(manifest_path)
    if raw.get("schema") != SCHEMA_VERSION:
        raise ValueError("engine bundle manifest has an unsupported schema")

    if not isinstance(raw.get("platform"), str) or not raw["platform"].strip():
        raise ValueError("engine bundle platform must be a string")
    if not isinstance(raw.get("architecture"), str) or not raw["architecture"].strip():
        raise ValueError("engine bundle architecture must be a string")
    platform_name = normalize_platform(raw["platform"])
    architecture = normalize_architecture(raw["architecture"])
    wanted_platform = normalize_platform(expected_platform)
    wanted_architecture = normalize_architecture(expected_architecture)
    if platform_name != wanted_platform or architecture != wanted_architecture:
        raise ValueError(
            "engine bundle targets "
            f"{platform_name}-{architecture}, not {wanted_platform}-{wanted_architecture}"
        )

    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("engine bundle runtime must be an object")
    python_relative = _relative_path(runtime.get("python"), "runtime.python")
    ollama_relative = _relative_path(runtime.get("ollama"), "runtime.ollama")
    model_store_relative = _relative_path(raw.get("model_store"), "model_store")

    base_raw = raw.get("base_models")
    if not isinstance(base_raw, list) or not base_raw:
        raise ValueError("engine bundle base_models must be a non-empty list")
    base_models = tuple(
        _model_spec(item, f"base_models[{index}]")
        for index, item in enumerate(base_raw)
    )
    names = [model.name for model in base_models]
    if len(names) != len(set(names)):
        raise ValueError("engine bundle contains duplicate base model names")
    embedding_model = _model_spec(raw.get("embedding_model"), "embedding_model")

    files_raw = raw.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise ValueError("engine bundle files must be a non-empty list")
    records: dict[str, BundleFile] = {}
    for index, item in enumerate(files_raw):
        if not isinstance(item, dict):
            raise ValueError(f"files[{index}] must be an object")
        relative = _relative_path(item.get("path"), f"files[{index}].path")
        key = relative.as_posix()
        if key == MANIFEST_NAME or key in records:
            raise ValueError("engine bundle contains a duplicate or self-referential file path")
        size = item.get("size")
        digest = item.get("sha256")
        executable = item.get("executable", False)
        if type(size) is not int or size < 0:
            raise ValueError(f"files[{index}].size must be a non-negative integer")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"files[{index}].sha256 must be lowercase SHA-256")
        if type(executable) is not bool:
            raise ValueError(f"files[{index}].executable must be a boolean")
        record = BundleFile(relative, size, digest, executable)
        target = root / relative
        _assert_safe_source(root, target, f"engine bundle file {key}")
        if not target.is_file() or _is_reparse(target):
            raise ValueError(f"engine bundle file is missing or unsafe: {key}")
        if target.stat().st_size != size:
            raise ValueError(f"engine bundle size mismatch: {key}")
        if verify_hashes and sha256_file(target) != digest:
            raise ValueError(f"engine bundle hash mismatch: {key}")
        records[key] = record

    for label, relative in (
        ("runtime.python", python_relative),
        ("runtime.ollama", ollama_relative),
    ):
        record = records.get(relative.as_posix())
        if record is None:
            raise ValueError(f"{label} is not sealed in files")
        if not record.executable:
            raise ValueError(f"{label} must be marked executable")

    store_key = model_store_relative.as_posix() + "/"
    model_records = {key for key in records if key.startswith(store_key)}
    if not model_records:
        raise ValueError("engine bundle model_store has no sealed files")
    actual_model_files = set()
    model_store_path = root / model_store_relative
    if not model_store_path.is_dir() or _is_reparse(model_store_path):
        raise ValueError("engine bundle model_store is missing or unsafe")
    for directory, dirs, files in os.walk(model_store_path, followlinks=False):
        directory_path = Path(directory)
        for name in dirs:
            if _is_reparse(directory_path / name):
                raise ValueError("engine bundle model_store contains a symlink or junction")
        for name in files:
            target = directory_path / name
            if _is_reparse(target):
                raise ValueError("engine bundle model_store contains a symlink or junction")
            actual_model_files.add(target.relative_to(root).as_posix())
    if actual_model_files != model_records:
        extras = sorted(actual_model_files - model_records)
        missing = sorted(model_records - actual_model_files)
        detail = extras[0] if extras else missing[0]
        raise ValueError(f"engine bundle model_store is not sealed exactly: {detail}")

    for model in (*base_models, embedding_model):
        _validate_ollama_model(root, model_store_relative, model, records)

    return EngineBundle(
        root=root,
        manifest_path=manifest_path,
        platform=platform_name,
        architecture=architecture,
        python_executable=root / python_relative,
        ollama_executable=root / ollama_relative,
        model_store=model_store_path,
        base_models=base_models,
        embedding_model=embedding_model,
        files=tuple(records.values()),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def discover_engine_bundle(
    system_root: Path,
    *,
    verify_hashes: bool = False,
) -> EngineBundle | None:
    """Find an explicit or conventionally bundled runtime for this platform."""
    candidates: list[Path] = []
    override = os.environ.get(ENGINE_BUNDLE_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    identity = platform_bundle_name()
    candidates.extend((system_root / "engine" / identity, system_root / "engine"))
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        manifest = candidate if candidate.name == MANIFEST_NAME else candidate / MANIFEST_NAME
        if manifest.is_file():
            return load_engine_bundle(candidate, verify_hashes=verify_hashes)
    return None


def select_base_model(
    bundle: EngineBundle,
    ram_gb: float,
    requested: str = "",
    preferred: str = "",
) -> str:
    if requested:
        for model in bundle.base_models:
            if model.name == requested:
                return model.name
        raise ValueError(
            f"requested model {requested!r} is not in offline bundle; available: "
            + ", ".join(model.name for model in bundle.base_models)
        )
    if preferred:
        for model in bundle.base_models:
            if model.name == preferred and model.min_ram_gb <= ram_gb:
                return model.name
    eligible = [model for model in bundle.base_models if model.min_ram_gb <= ram_gb]
    if not eligible:
        return min(bundle.base_models, key=lambda item: item.min_ram_gb).name
    return max(eligible, key=lambda item: item.min_ram_gb).name


def default_trilobite_home() -> Path:
    configured = os.environ.get("TRILOBITE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(root or Path.home()) / "trilobite"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "trilobite"
    return Path.home() / ".local" / "share" / "trilobite"


def _assert_safe_destination(root: Path, target: Path) -> None:
    root_absolute = root.absolute()
    target_absolute = target.absolute()
    try:
        relative = target_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError("model-store destination escapes Trilobite home") from exc
    current = root_absolute
    if current.exists() and _is_reparse(current):
        raise ValueError("Trilobite home may not be a symlink or junction")
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise ValueError("model-store destination traverses a symlink or junction")


def install_model_store(
    bundle: EngineBundle,
    home: Path | None = None,
) -> tuple[Path, int, int]:
    """Copy the sealed model subset into writable shared state atomically."""
    home = (home or default_trilobite_home()).absolute()
    destination = home / "ollama-models"
    _assert_safe_destination(home, destination)
    destination.mkdir(parents=True, exist_ok=True)
    model_prefix = bundle.model_store.relative_to(bundle.root).parts
    copied = 0
    reused = 0
    for record in bundle.files:
        if record.relative.parts[: len(model_prefix)] != model_prefix:
            continue
        suffix = Path(*record.relative.parts[len(model_prefix) :])
        source = bundle.root / record.relative
        target = destination / suffix
        _assert_safe_destination(home, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == record.size:
            if sha256_file(target) == record.sha256:
                reused += 1
                continue
        elif target.exists():
            raise ValueError(f"model-store destination is not a regular file: {suffix.as_posix()}")
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with source.open("rb") as incoming, temp_path.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            if temp_path.stat().st_size != record.size or sha256_file(temp_path) != record.sha256:
                raise ValueError(f"copied model file failed verification: {suffix.as_posix()}")
            os.replace(temp_path, target)
            copied += 1
        finally:
            if temp_path.exists():
                temp_path.unlink()

    receipt = {
        "schema": 1,
        "bundle": bundle.identity,
        "manifest_sha256": bundle.manifest_sha256,
        "base_models": [model.name for model in bundle.base_models],
        "embedding_model": bundle.embedding_model.name,
    }
    receipt_path = destination / "TRILOBITE-BUNDLE-RECEIPT.json"
    fd, temp_name = tempfile.mkstemp(prefix=".receipt.", dir=str(destination))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, receipt_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return destination, copied, reused


def runtime_environment(bundle: EngineBundle, model_store: Path) -> dict[str, str]:
    current_path = os.environ.get("PATH", "")
    ollama_dir = str(bundle.ollama_executable.parent)
    path_value = ollama_dir if not current_path else ollama_dir + os.pathsep + current_path
    return {
        "OLLAMA_MODELS": str(model_store),
        "OLLAMA_NO_CLOUD": "1",
        "PATH": path_value,
        "TRILOBITE_OLLAMA_EXE": str(bundle.ollama_executable),
        "TRILOBITE_EMBED_MODEL": bundle.embedding_model.name,
    }
