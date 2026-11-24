"""Adapter integrity validation, receipt inspection, and reviewed converter staging."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from sonder_runtime.adapters.inference import ollama_endpoint


_client_environment = cast(
    Callable[..., dict[str, str]],
    ollama_endpoint.client_environment,
)


class DatasetInspectionProtocol(Protocol):
    """Minimal dataset inspection attributes consumed by manifest validation."""

    examples: Any
    content_chars: int
    file_bytes: int
    sha256: str


RunnerFn = Callable[..., subprocess.CompletedProcess[Any]]


def write_json_atomic(path: str | Path, payload: Any) -> str:
    """Atomically write pretty-printed JSON and return the UTF-8 SHA-256 hex digest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("%s.tmp-%s" % (target.name, uuid.uuid4().hex))
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with tmp.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Compute streaming SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def launch_authorization_hmac(
    token: str, plan_sha256: str, data_sha256: str
) -> str:
    """Compute the launch authorization HMAC binding token, plan, and dataset."""
    message = (
        "sonder-training-launch-v1\n%s\n%s" % (plan_sha256, data_sha256)
    ).encode("ascii")
    return hmac.new(str(token).encode("utf-8"), message, hashlib.sha256).hexdigest()


def inspect_launch_receipt(
    path: str | Path,
    *,
    plan_sha256: str,
    data_sha256: str,
    authorization_hmac: str,
) -> tuple[dict[str, Any], str]:
    """Validate a bounded training launch receipt and return ``(payload, sha256)``."""
    target = Path(path)
    try:
        with target.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > 64 * 1024:
                raise ValueError("training launch receipt is too large")
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("training launch receipt is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("training launch receipt is unreadable") from exc
    expected: dict[str, Any] = {
        "schema": 1,
        "plan_sha256": plan_sha256,
        "data_sha256": data_sha256,
        "authorization_hmac": authorization_hmac,
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("training launch receipt does not match authorization")
    try:
        consumed_ts = int(payload.get("consumed_ts") or 0)
        owner_pid = int(payload.get("owner_pid") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("training launch receipt metadata is invalid") from exc
    if consumed_ts <= 0 or owner_pid <= 0:
        raise ValueError("training launch receipt metadata is invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def inspect_export_manifest(
    path: str | Path, inspection: DatasetInspectionProtocol
) -> str:
    """Validate and hash the privacy-selection receipt beside a memory export."""
    target = Path(path)
    try:
        with target.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > 64 * 1024:
                raise ValueError("training export selection manifest is too large")
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("training export selection manifest is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("training export selection manifest is unreadable") from exc
    expected: dict[str, Any] = {
        "schema": 1,
        "format": "sonder-chat-jsonl",
        "accepted": len(inspection.examples),
        "characters": inspection.content_chars,
        "sha256": inspection.sha256,
        "privacy_policy": "exclude-shared-private-markers",
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("training export selection manifest does not match its dataset")
    return hashlib.sha256(raw).hexdigest()


def path_has_control_chars(value: object) -> bool:
    """Return whether ``value`` contains ASCII control characters."""
    return any(ord(char) < 32 or ord(char) == 127 for char in str(value))


def path_uses_symlink(path: str | Path) -> bool:
    """Return True when any existing component of *path* is a symlink."""
    resolved = Path(path).absolute()
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            return True
    return False


def json_object(path: str | Path, label: str) -> dict[str, Any]:
    """Read a JSON object from *path*, raising ``ValueError`` on failure."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected a JSON object")
    return value


def bounded_error(exc: BaseException, limit: int = 300) -> str:
    """Normalize an exception message to a single bounded line."""
    value = " ".join(str(exc).split())
    return value[:limit] or type(exc).__name__


def run_external(
    runner: RunnerFn,
    command: list[str],
    *,
    ollama_command: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run an external command with canonical Ollama endpoint enforcement."""
    if ollama_command:
        try:
            kwargs["env"] = _client_environment(kwargs.get("env"))
        except ValueError as exc:
            return subprocess.CompletedProcess(
                command, 125, stdout="", stderr="Ollama endpoint blocked: %s" % exc,
            )
    try:
        return runner(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command, 124, stdout=exc.stdout or "", stderr="command timed out",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 126, stdout="", stderr=str(exc))


def model_show_status(result: subprocess.CompletedProcess[Any], model: str) -> str:
    """Classify an ``ollama show`` result as ``exists``, ``missing``, or ``error``."""
    if result.returncode == 0:
        return "exists"
    output = "%s\n%s" % (result.stdout or "", result.stderr or "")
    lowered = output.lower()
    model_root = str(model).split(":", 1)[0].lower()
    if "not found" in lowered and model_root in lowered:
        return "missing"
    return "error"


def show_identity(result: subprocess.CompletedProcess[Any]) -> str:
    """Hash normalized ``ollama show`` stdout, or return empty on failure."""
    if result.returncode:
        return ""
    normalized = (result.stdout or "").replace("\r\n", "\n").strip()
    return (
        hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        if normalized
        else ""
    )


def ollama_executable(explicit: str = "") -> str:
    """Resolve the Ollama binary path from explicit override, env, or PATH."""
    return (
        explicit
        or os.environ.get("SONDER_OLLAMA_EXE", "").strip()
        or shutil.which("ollama")
        or "ollama"
    )


def validate_adapter(
    adapter_dir: str | Path,
    expected_base: str = "",
    *,
    model_specs: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, Any]:
    """Validate PEFT adapter artifacts, pinned model provenance, and SHA-256 hashes."""
    if path_has_control_chars(adapter_dir):
        return False, "adapter path contains control characters"
    root = Path(adapter_dir)
    config_path = root / "adapter_config.json"
    manifest_path = root / "training-manifest.json"
    weights_path = root / "adapter_model.safetensors"
    required = (config_path, manifest_path, weights_path)
    if any(not path.is_file() for path in required):
        return False, (
            "adapter_config.json, adapter_model.safetensors, and "
            "training-manifest.json are required"
        )
    if any(path.is_symlink() for path in required) or path_uses_symlink(root):
        return False, "adapter artifacts must not use symbolic links"
    try:
        config = json_object(config_path, "adapter_config.json")
        manifest = json_object(manifest_path, "training-manifest.json")
    except ValueError as exc:
        return False, str(exc)
    configured = str(config.get("base_model_name_or_path") or "").rstrip("/")
    trained = str(manifest.get("base_hf") or "").rstrip("/")
    if not configured or configured != trained:
        return False, f"adapter/base mismatch: PEFT={configured!r}, manifest={trained!r}"
    if expected_base and trained != expected_base.rstrip("/"):
        return False, f"adapter base {trained!r} does not match expected {expected_base!r}"
    if config.get("invocation_string") or config.get("alora_invocation_tokens"):
        return False, (
            "aLoRA invocation adapters are not supported by the offline pinned-base "
            "conversion path"
        )
    ollama_base = str(manifest.get("base_ollama") or "")
    size = str(manifest.get("model_size") or "")
    if size not in model_specs or ollama_base != model_specs[size]["ollama"]:
        return False, "manifest Ollama base is not the exact mapped Qwen2.5-Coder base"
    if (
        trained != model_specs[size]["hf"]
        or manifest.get("hf_revision") != model_specs[size]["hf_revision"]
    ):
        return False, "manifest Hugging Face base is not the reviewed pinned commit"
    if manifest.get("schema") != 2:
        return False, "adapter manifest schema is unsupported"
    try:
        completed = int(manifest.get("completed_ts") or 0)
        consumed = int(manifest.get("launch_consumed_ts") or 0)
    except (TypeError, ValueError):
        return False, "adapter manifest completion timestamps are invalid"
    if completed <= 0 or consumed <= 0:
        return False, "adapter manifest does not prove a completed authorized run"
    hashes = manifest.get("artifact_sha256")
    sizes = manifest.get("artifact_sizes")
    if not isinstance(hashes, dict) or not isinstance(sizes, dict):
        return False, "manifest is missing adapter artifact hashes"
    for name, path in (
        ("adapter_config.json", config_path),
        ("adapter_model.safetensors", weights_path),
    ):
        expected_hash = str(hashes.get(name) or "")
        expected_size = sizes.get(name)
        if (
            len(expected_hash) != 64
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            return False, f"manifest has invalid integrity metadata for {name}"
        if path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
            return False, f"adapter artifact integrity check failed for {name}"
    return True, manifest


def validate_deployment_adapter(
    adapter_dir: str | Path,
    state: Mapping[str, Any],
    *,
    inspect_jsonl_fn: Callable[..., DatasetInspectionProtocol],
    training_data_error_cls: type[Exception],
    validate_adapter_fn: Callable[[str | Path, str], tuple[bool, Any]],
) -> tuple[bool, Any]:
    """Bind deployment to one completed run issued by this lifecycle controller."""
    if state.get("status") not in {"trained", "deployed", "rolled_back"}:
        return False, "training state must identify a completed trusted run"
    run_id = str(state.get("run_id") or "")
    raw_run_dir = str(state.get("run_dir") or "")
    raw_state_adapter = str(state.get("adapter_dir") or "")
    raw_plan = str(state.get("plan_file") or "")
    if not run_id or any(
        not value or path_has_control_chars(value)
        for value in (raw_run_dir, raw_state_adapter, raw_plan)
    ):
        return False, "trusted training provenance is incomplete"
    try:
        requested = Path(adapter_dir).resolve(strict=True)
        run_dir = Path(raw_run_dir).resolve(strict=True)
        state_adapter = Path(raw_state_adapter).resolve(strict=True)
        plan_path = Path(raw_plan).resolve(strict=True)
    except OSError as exc:
        return False, f"trusted training provenance is unavailable: {exc}"
    if any(
        path_uses_symlink(path)
        for path in (adapter_dir, raw_run_dir, raw_state_adapter, raw_plan)
    ):
        return False, "trusted training provenance must not use symbolic links"
    if run_dir.name != run_id or requested != state_adapter or requested != run_dir / "adapter":
        return False, "adapter path does not match the completed trusted run"
    if plan_path != run_dir / "training-plan.json":
        return False, "training plan is outside the completed trusted run"
    manifest_path = requested / "training-manifest.json"
    if state.get("manifest") and Path(str(state["manifest"])).resolve() != manifest_path:
        return False, "training state manifest path does not match the trusted adapter"
    for key, path in (
        ("plan_sha256", plan_path),
        ("manifest_sha256", manifest_path),
    ):
        expected_hash = str(state.get(key) or "")
        if len(expected_hash) != 64 or sha256_file(path) != expected_hash:
            return False, f"trusted training provenance integrity failed for {path.name}"
    try:
        plan = json_object(plan_path, "training-plan.json")
        manifest = json_object(manifest_path, "training-manifest.json")
    except ValueError as exc:
        return False, str(exc)
    if plan.get("schema") != 2 or manifest.get("schema") != 2:
        return False, "training provenance schema is unsupported"
    try:
        completed = int(manifest.get("completed_ts") or 0)
        consumed = int(manifest.get("launch_consumed_ts") or 0)
    except (TypeError, ValueError):
        return False, "adapter manifest completion timestamps are invalid"
    if completed <= 0 or consumed <= 0:
        return False, "adapter manifest does not prove a completed authorized run"
    immutable = (
        "run_id", "base_hf", "hf_revision", "base_ollama", "model_size", "method",
        "data_path", "data_sha256", "data_examples", "data_bytes",
        "data_content_chars", "data_source", "source_data_path",
        "source_data_sha256", "selection_manifest_path",
        "selection_manifest_sha256",
        "adapter_dir", "gpu_index",
    )
    for key in immutable:
        if plan.get(key) != manifest.get(key):
            return False, f"adapter manifest does not match training plan field {key}"
    expected: dict[str, Any] = {
        "run_id": run_id,
        "base_hf": state.get("base_hf"),
        "hf_revision": state.get("hf_revision"),
        "base_ollama": state.get("base_ollama"),
        "adapter_dir": str(requested),
        "data_sha256": state.get("data_sha256"),
        "data_examples": state.get("data_examples"),
        "data_bytes": state.get("data_bytes"),
        "data_content_chars": state.get("data_content_chars"),
        "selection_manifest_sha256": state.get("selection_manifest_sha256"),
    }
    for key, value in expected.items():
        if str(manifest.get(key) or "") != str(value or ""):
            return False, f"training state does not match adapter field {key}"
    try:
        snapshot_path = Path(manifest["data_path"]).resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        return False, f"trusted training dataset snapshot is unavailable: {exc}"
    if snapshot_path != run_dir / "training-data.jsonl":
        return False, "trusted training dataset snapshot is outside the run"
    try:
        inspection = inspect_jsonl_fn(
            snapshot_path, expected_sha256=str(manifest.get("data_sha256") or ""),
        )
    except (OSError, training_data_error_cls):
        return False, "trusted training dataset snapshot integrity failed"
    dataset_fields = {
        "data_examples": len(inspection.examples),
        "data_bytes": inspection.file_bytes,
        "data_content_chars": inspection.content_chars,
    }
    if any(manifest.get(key) != value for key, value in dataset_fields.items()):
        return False, "trusted training dataset snapshot metadata changed"
    if manifest.get("data_source") == "memory_export":
        try:
            selection_path = Path(
                str(manifest.get("selection_manifest_path") or "")
            ).resolve(strict=True)
        except OSError:
            return False, "trusted training selection manifest is unavailable"
        if selection_path != Path(str(snapshot_path) + ".manifest.json"):
            return False, "trusted training selection manifest is outside the run"
        try:
            selection_hash = inspect_export_manifest(selection_path, inspection)
        except (OSError, ValueError):
            return False, "trusted training selection manifest integrity failed"
        if selection_hash != str(manifest.get("selection_manifest_sha256") or ""):
            return False, "trusted training selection manifest changed"
    elif (
        manifest.get("selection_manifest_path")
        or manifest.get("selection_manifest_sha256")
    ):
        return False, "explicit training data must not claim export selection evidence"
    for key in ("artifact_sha256", "artifact_sizes"):
        if state.get(key) != manifest.get(key):
            return False, f"training state does not match adapter {key}"
    ok, detail = validate_adapter_fn(requested, str(state.get("base_hf") or ""))
    if not ok:
        return False, detail
    return True, detail


def converter_path(explicit: str = "", *, repo_root: Path) -> Path | None:
    """Locate ``convert_lora_to_gguf.py`` across explicit, env, and repo paths."""
    roots: list[str | Path] = [
        explicit,
        os.environ.get("SONDER_LLAMA_CPP", ""),
        repo_root / "llama.cpp",
        repo_root / "third_party" / "llama.cpp",
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root)
        if candidate.is_file() and candidate.name == "convert_lora_to_gguf.py":
            return candidate
        candidate = candidate / "convert_lora_to_gguf.py"
        if candidate.exists():
            return candidate
    return None


def stage_reviewed_converter(
    converter: str | Path,
    destination: str | Path,
    *,
    revision: str,
    expected_tree_sha256: str,
    runner: RunnerFn = subprocess.run,
    run_external_fn: Callable[..., subprocess.CompletedProcess[Any]] = run_external,
) -> tuple[bool, Any]:
    """Export exact reviewed Git objects; never execute the mutable checkout."""
    converter_resolved = Path(converter).resolve()
    dest = Path(destination).resolve()
    git = shutil.which("git") or "git"
    git_env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    git_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    git_env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.autocrlf",
        "GIT_CONFIG_VALUE_0": "false",
    })

    def run(arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[Any]:
        return run_external_fn(
            runner,
            [git, "-C", str(converter_resolved.parent), *arguments],
            capture_output=True,
            text=True,
            env=git_env,
            timeout=timeout,
        )

    root_result = run(["rev-parse", "--show-toplevel"])
    if root_result.returncode:
        return False, "llama.cpp converter is not in a reviewed Git checkout"
    try:
        root = Path((root_result.stdout or "").strip()).resolve(strict=True)
    except OSError:
        return False, "llama.cpp checkout root could not be verified"
    if converter_resolved != root / "convert_lora_to_gguf.py":
        return False, "llama.cpp converter must be the reviewed checkout root script"

    rev_check = run(["cat-file", "-e", f"{revision}^{{commit}}"])
    if rev_check.returncode:
        return False, (
            "llama.cpp checkout does not contain reviewed converter commit "
            f"{revision}"
        )
    protected = ["--", "convert_lora_to_gguf.py", "conversion", "gguf-py"]
    tree = run(["ls-tree", "-r", "--full-tree", revision, *protected])
    if tree.returncode or not (tree.stdout or "").strip():
        return False, "llama.cpp converter dependency tree could not be sealed"
    sealed_blobs: dict[str, tuple[str, str]] = {}
    try:
        for line in (tree.stdout or "").splitlines():
            metadata, relative = line.split("\t", 1)
            mode, object_type, object_id = metadata.split()
            relative_path = Path(relative)
            if (
                object_type != "blob"
                or mode not in {"100644", "100755"}
                or not re.fullmatch(r"[0-9a-f]{40}", object_id)
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative in sealed_blobs
            ):
                raise ValueError("unsafe tree entry")
            sealed_blobs[relative_path.as_posix()] = (mode, object_id)
    except (TypeError, ValueError):
        return False, "llama.cpp converter tree listing is invalid"
    tree_sha256 = hashlib.sha256(
        (tree.stdout or "").encode("utf-8", "strict")
    ).hexdigest()
    if tree_sha256 != expected_tree_sha256:
        return False, "llama.cpp converter dependency tree seal does not match"

    archive = dest.with_name(dest.name + ".zip")
    if dest.exists() or archive.exists():
        return False, "llama.cpp converter staging path already exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    archived = run(
        ["archive", "--format=zip", f"--output={archive}", revision, *protected],
        timeout=120,
    )
    if archived.returncode or not archive.is_file():
        with contextlib.suppress(OSError):
            archive.unlink()
        return False, "reviewed llama.cpp converter archive could not be materialized"
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = member.filename.replace("\\", "/")
                parts = tuple(part for part in name.split("/") if part)
                file_type = (member.external_attr >> 16) & 0o170000
                if (
                    not parts
                    or name.startswith(("/", "\\"))
                    or ":" in parts[0]
                    or ".." in parts
                    or file_type == 0o120000
                ):
                    return False, "reviewed llama.cpp archive contains an unsafe path"
            dest.mkdir()
            bundle.extractall(dest)
    except (OSError, ValueError, zipfile.BadZipFile):
        shutil.rmtree(dest, ignore_errors=True)
        return False, "reviewed llama.cpp converter archive is invalid"
    finally:
        with contextlib.suppress(OSError):
            archive.unlink()

    staged_converter = dest / "convert_lora_to_gguf.py"
    if not staged_converter.is_file() or staged_converter.is_symlink():
        shutil.rmtree(dest, ignore_errors=True)
        return False, "reviewed llama.cpp converter is missing from staged archive"
    manifest = hashlib.sha256()
    files = sorted(
        (path for path in dest.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(dest).as_posix(),
    )
    extracted_paths = {path.relative_to(dest).as_posix() for path in files}
    if extracted_paths != set(sealed_blobs) or any(path.is_symlink() for path in files):
        shutil.rmtree(dest, ignore_errors=True)
        return False, "reviewed llama.cpp staged source manifest is incomplete"
    for path in files:
        relative = path.relative_to(dest).as_posix()
        size = path.stat().st_size
        blob_digest = hashlib.sha1()
        blob_digest.update(f"blob {size}\0".encode("ascii"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                blob_digest.update(block)
        if not hmac.compare_digest(blob_digest.hexdigest(), sealed_blobs[relative][1]):
            shutil.rmtree(dest, ignore_errors=True)
            return False, "reviewed llama.cpp staged blob does not match sealed Git object"
        manifest.update(relative.encode("utf-8", "strict") + b"\0")
        manifest.update(str(size).encode("ascii") + b"\0")
        manifest.update(sha256_file(path).encode("ascii") + b"\n")
    return True, {
        "root": str(root),
        "converter": str(staged_converter),
        "revision": revision,
        "tree_sha256": tree_sha256,
        "staged_manifest_sha256": manifest.hexdigest(),
    }
