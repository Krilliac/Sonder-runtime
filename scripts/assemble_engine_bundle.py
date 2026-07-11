"""Assemble a sealed platform runtime + Ollama model bundle.

Windows can derive a lean portable Python runtime from the interpreter running
this script. Linux/macOS should pass a relocatable Python distribution with
``--python-runtime``. Ollama and model files are copied from local installations;
the resulting bundle is verified by :mod:`engine_bundle` before publication.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - exercised only by minimal host Python
    InvalidRequirement = ValueError
    Requirement = None


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine_bundle  # noqa: E402


MODEL_STORE_DEFAULT = Path.home() / ".ollama" / "models"
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_MODEL_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
_ALLOWED_OUTPUT_PARENTS = (
    Path("app/build/engine-bundles"),
    Path("dist/engine-bundles"),
)


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_regular_tree(path: Path, label: str) -> None:
    if not path.is_dir() or _is_reparse(path):
        raise ValueError(f"{label} must be a real directory")
    for directory, dirs, files in os.walk(path, followlinks=False):
        current = Path(directory)
        for name in [*dirs, *files]:
            if _is_reparse(current / name):
                raise ValueError(f"{label} contains a symlink or junction: {name}")


def validate_output(path: Path) -> Path:
    identity = engine_bundle.platform_bundle_name()
    raw = path if path.is_absolute() else ROOT / path
    lexical = Path(os.path.abspath(raw))
    allowed = tuple(Path(os.path.abspath(ROOT / parent / identity)) for parent in _ALLOWED_OUTPUT_PARENTS)
    if not any(os.path.normcase(str(lexical)) == os.path.normcase(str(item)) for item in allowed):
        choices = ", ".join((parent / identity).as_posix() for parent in _ALLOWED_OUTPUT_PARENTS)
        raise ValueError(f"--out must be exactly one of: {choices}")
    current = ROOT.resolve()
    try:
        relative = lexical.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("--out must stay inside the repository") from exc
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise ValueError("--out may not traverse a symlink or junction")
    return lexical


def _copy_tree(source: Path, target: Path, *, ignore=None) -> None:
    _assert_regular_tree(source, "runtime source")
    for directory, dirs, files in os.walk(source, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(source)
        dirs[:] = [name for name in dirs if not (ignore and ignore(relative / name, True))]
        destination = target / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            rel = relative / name
            if ignore and ignore(rel, False):
                continue
            source_file = current / name
            target_file = target / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)


def _distribution_closure(root_name: str) -> list[importlib.metadata.Distribution]:
    pending = [root_name]
    seen: set[str] = set()
    result = []
    while pending:
        requested = pending.pop()
        key = requested.casefold().replace("_", "-")
        if key in seen:
            continue
        try:
            distribution = importlib.metadata.distribution(requested)
        except importlib.metadata.PackageNotFoundError:
            if requested == root_name:
                raise ValueError(
                    f"{root_name} is not installed in {sys.executable}; run the assembler with the repo venv"
                ) from None
            continue
        seen.add(key)
        result.append(distribution)
        for requirement in distribution.requires or ():
            if Requirement is None:
                dependency, _, marker = requirement.partition(";")
                if "extra" in marker.casefold():
                    continue
                match = _REQUIREMENT_NAME.match(dependency)
                if match:
                    pending.append(match.group(1))
                continue
            try:
                parsed = Requirement(requirement)
            except InvalidRequirement:
                continue
            if parsed.marker is None or parsed.marker.evaluate({"extra": ""}):
                pending.append(parsed.name)
    return result


def _copy_distribution(distribution: importlib.metadata.Distribution, destination: Path) -> None:
    for entry in distribution.files or ():
        pure = PurePosixPath(str(entry).replace("\\", "/"))
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            continue
        if any(part.casefold() in {"test", "tests"} for part in pure.parts):
            continue
        if pure.suffix == ".pyc" or "__pycache__" in pure.parts:
            continue
        source = Path(distribution.locate_file(entry))
        if not source.is_file() or _is_reparse(source):
            continue
        target = destination / Path(*pure.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _remove_python_bytecode(root: Path) -> None:
    for path in root.rglob("*.pyc"):
        if path.is_file() and not _is_reparse(path):
            path.unlink()
    cache_dirs = sorted(
        (path for path in root.rglob("__pycache__") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in cache_dirs:
        try:
            directory.rmdir()
        except OSError:
            pass


def build_windows_python_runtime(destination: Path) -> Path:
    if os.name != "nt":
        raise ValueError("automatic Python runtime assembly is Windows-only; pass --python-runtime")
    source = Path(sys.base_prefix)
    _assert_regular_tree(source, "Python base runtime")
    destination.mkdir(parents=True, exist_ok=True)
    for pattern in ("python*.exe", "python*.dll", "vcruntime*.dll", "LICENSE.txt"):
        for item in source.glob(pattern):
            if item.is_file() and not _is_reparse(item):
                shutil.copy2(item, destination / item.name)
    _copy_tree(source / "DLLs", destination / "DLLs")

    def ignore_stdlib(relative: Path, is_dir: bool) -> bool:
        parts = {part.casefold() for part in relative.parts}
        excluded = {
            "ensurepip",
            "idlelib",
            "lib2to3",
            "msilib",
            "test",
            "tkinter",
            "turtledemo",
        }
        return (
            "site-packages" in parts
            or "__pycache__" in parts
            or bool(parts & excluded)
            or (not is_dir and relative.suffix == ".pyc")
        )

    _copy_tree(source / "Lib", destination / "Lib", ignore=ignore_stdlib)
    site_packages = destination / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    for distribution in _distribution_closure("mcp"):
        _copy_distribution(distribution, site_packages)
    python = destination / "python.exe"
    if not python.is_file():
        raise ValueError("assembled Python runtime has no python.exe")
    smoke = subprocess.run(
        [str(python), "-I", "-c", "import mcp, pydantic_core; print(mcp.__name__)"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if smoke.returncode != 0:
        detail = (smoke.stderr or smoke.stdout).strip()
        raise ValueError(f"assembled Python runtime failed isolated import: {detail}")
    return python


def _model_manifest_relative(name: str) -> Path:
    if not _MODEL_NAME.fullmatch(name):
        raise ValueError(f"invalid Ollama model name: {name}")
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        repository, tag = name[:colon], name[colon + 1 :]
    else:
        repository, tag = name, "latest"
    parts = repository.split("/")
    if len(parts) == 1:
        host, namespace, model = "registry.ollama.ai", "library", parts[0]
    elif "." in parts[0] or ":" in parts[0]:
        if len(parts) < 3:
            raise ValueError(f"fully qualified model name needs host/namespace/model: {name}")
        host, namespace, model = parts[0], "/".join(parts[1:-1]), parts[-1]
    else:
        host, namespace, model = "registry.ollama.ai", "/".join(parts[:-1]), parts[-1]
    return Path("manifests") / host / Path(*namespace.split("/")) / model / tag


def _copy_model(
    source_store: Path,
    destination_store: Path,
    name: str,
) -> Path:
    manifest_relative = _model_manifest_relative(name)
    source_manifest = source_store / manifest_relative
    if not source_manifest.is_file() or _is_reparse(source_manifest):
        raise ValueError(f"Ollama model is not installed: {name} ({source_manifest})")
    try:
        manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Ollama model manifest is invalid: {name}") from exc
    objects = [manifest.get("config"), *(manifest.get("layers") or [])]
    if manifest.get("schemaVersion") != 2 or not isinstance(manifest.get("layers"), list):
        raise ValueError(f"Ollama model manifest is incomplete: {name}")
    target_manifest = destination_store / manifest_relative
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_manifest, target_manifest)
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("digest"), str):
            raise ValueError(f"Ollama model manifest has an invalid object: {name}")
        digest = item["digest"]
        if not digest.startswith("sha256:") or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Ollama model manifest has an invalid digest: {name}")
        blob_name = digest.replace(":", "-", 1)
        source_blob = source_store / "blobs" / blob_name
        if not source_blob.is_file() or _is_reparse(source_blob):
            raise ValueError(f"Ollama model blob is missing: {blob_name}")
        if type(item.get("size")) is not int or source_blob.stat().st_size != item["size"]:
            raise ValueError(f"Ollama model blob size mismatch: {blob_name}")
        target_blob = destination_store / "blobs" / blob_name
        target_blob.parent.mkdir(parents=True, exist_ok=True)
        if not target_blob.exists():
            shutil.copy2(source_blob, target_blob)
    return Path("models") / manifest_relative


def _copy_ollama_runtime(source: Path, destination: Path) -> Path:
    _assert_regular_tree(source, "Ollama runtime")
    executable_name = "ollama.exe" if os.name == "nt" else "ollama"
    executable = source / executable_name
    if not executable.is_file() or _is_reparse(executable):
        raise ValueError(f"Ollama runtime is missing {executable_name}")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, destination / executable_name)
    library = source / "lib" / "ollama"
    if library.is_dir():
        _copy_tree(library, destination / "lib" / "ollama")
    return destination / executable_name


def _file_records(root: Path, executables: set[Path]) -> list[dict]:
    records = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == engine_bundle.MANIFEST_NAME:
            continue
        relative = path.relative_to(root)
        records.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": engine_bundle.sha256_file(path),
                "executable": relative in executables,
            }
        )
    return records


def assemble_bundle(
    output: Path,
    *,
    ollama_runtime: Path,
    model_store: Path,
    base_models: list[tuple[str, float]],
    embedding_model: str,
    python_runtime: Path | None = None,
    validate_runtime: bool = True,
) -> engine_bundle.EngineBundle:
    output = validate_output(output)
    if not base_models:
        raise ValueError("at least one base model is required")
    _assert_regular_tree(model_store, "Ollama model store")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=str(output.parent)))
    backup = output.with_name(f".{output.name}.backup-{uuid.uuid4().hex}")
    moved_existing = False
    try:
        python_target = stage / "runtime" / "python"
        if python_runtime is None:
            python_executable = build_windows_python_runtime(python_target)
        else:
            _copy_tree(python_runtime, python_target)
            candidates = (
                [python_target / "python.exe"]
                if os.name == "nt"
                else [python_target / "bin" / "python3", python_target / "python3"]
            )
            python_executable = next((item for item in candidates if item.is_file()), None)
            if python_executable is None:
                raise ValueError("provided Python runtime has no canonical executable")
            if validate_runtime:
                smoke = subprocess.run(
                    [str(python_executable), "-I", "-c", "import mcp"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                if smoke.returncode != 0:
                    raise ValueError("provided Python runtime cannot import mcp in isolated mode")

        _remove_python_bytecode(python_target)

        ollama_executable = _copy_ollama_runtime(
            ollama_runtime,
            stage / "runtime" / "ollama",
        )
        destination_store = stage / "models"
        base_specs = []
        for name, min_ram in base_models:
            manifest = _copy_model(model_store, destination_store, name)
            base_specs.append(
                {"name": name, "manifest": manifest.as_posix(), "min_ram_gb": min_ram}
            )
        embed_manifest = _copy_model(model_store, destination_store, embedding_model)
        python_relative = python_executable.relative_to(stage)
        ollama_relative = ollama_executable.relative_to(stage)
        manifest = {
            "schema": 1,
            "platform": engine_bundle.normalize_platform(),
            "architecture": engine_bundle.normalize_architecture(),
            "runtime": {
                "python": python_relative.as_posix(),
                "ollama": ollama_relative.as_posix(),
            },
            "model_store": "models",
            "base_models": base_specs,
            "embedding_model": {
                "name": embedding_model,
                "manifest": embed_manifest.as_posix(),
            },
            "files": _file_records(stage, {python_relative, ollama_relative}),
        }
        (stage / engine_bundle.MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        engine_bundle.load_engine_bundle(stage, verify_hashes=True)
        if output.exists():
            _assert_regular_tree(output, "existing engine bundle")
            output.rename(backup)
            moved_existing = True
        stage.rename(output)
        if moved_existing:
            shutil.rmtree(backup)
        return engine_bundle.load_engine_bundle(output, verify_hashes=True)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if moved_existing and backup.exists() and not output.exists():
            backup.rename(output)
        raise


def _parse_base_model(value: str) -> tuple[str, float]:
    name, separator, ram = value.rpartition("=")
    if not separator:
        return value, 0.0
    try:
        min_ram = float(ram)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("base model must be NAME or NAME=MIN_RAM_GB") from exc
    if not name or min_ram < 0:
        raise argparse.ArgumentTypeError("base model must be NAME or NAME=MIN_RAM_GB")
    return name, min_ram


def main(argv=None) -> int:
    identity = engine_bundle.platform_bundle_name()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=f"app/build/engine-bundles/{identity}")
    parser.add_argument(
        "--ollama-runtime",
        default=str(Path(shutil.which("ollama") or "ollama").parent),
        help="directory containing ollama(.exe) and optional lib/ollama",
    )
    parser.add_argument("--model-store", default=str(MODEL_STORE_DEFAULT))
    parser.add_argument("--python-runtime", default="")
    parser.add_argument(
        "--base-model",
        action="append",
        type=_parse_base_model,
        help="repeatable NAME or NAME=MIN_RAM_GB (default qwen2.5-coder:1.5b=0)",
    )
    parser.add_argument("--embedding-model", default="nomic-embed-text:latest")
    args = parser.parse_args(argv)
    try:
        bundle = assemble_bundle(
            Path(args.out),
            ollama_runtime=Path(args.ollama_runtime).expanduser(),
            model_store=Path(args.model_store).expanduser(),
            base_models=args.base_model or [("qwen2.5-coder:1.5b", 0.0)],
            embedding_model=args.embedding_model,
            python_runtime=Path(args.python_runtime).expanduser() if args.python_runtime else None,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"engine bundle assembly failed: {exc}", file=sys.stderr)
        return 2
    total = sum(record.size for record in bundle.files)
    print(bundle.root)
    print(f"verified {len(bundle.files)} files, {total / (1024 ** 3):.2f} GiB")
    print("base models: " + ", ".join(model.name for model in bundle.base_models))
    print("embedding model: " + bundle.embedding_model.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
