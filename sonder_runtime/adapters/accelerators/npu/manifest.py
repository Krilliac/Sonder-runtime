"""Accelerator model-bundle manifests: schema, hashes, and identity.

Bundles are configured by per-user JSON manifests, never downloaded by the
runtime. A manifest pins the exact model bytes (sha256 + size), the operation,
the tokenizer/pre/postprocess identity, the output dimension, a provider
allowlist, and per-model limits. File references are relative to the manifest's
directory so packaged code never carries absolute paths; hash drift disables a
bundle instead of silently serving different weights.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import sonder_runtime.adapters.accelerators.npu.contract as npu_contract
import sonder_runtime.platform.paths as runtime_paths


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 256_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,119}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POOLINGS = ("mean", "cls")
_ROUTE_POSTPROCESS = ("softmax", "none")
# Vendor session options a manifest may set, per provider. Values are plain
# bounded strings; the shared runtime policy can never reach these.
_PROVIDER_OPTION_KEYS = {
    "vitisai": ("config_file",),
    "openvino": ("device_type",),
    "qnn": ("backend_path",),
}


def manifest_dir() -> Path:
    """Per-user manifest directory; models live beside their manifests."""
    return Path(runtime_paths.state_path("npu-manifests", "SONDER_NPU_MANIFEST_DIR"))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical_space_model(value) -> str:
    model = str(value or "").strip().casefold()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    if model and ":" not in model:
        model += ":latest"
    return model


def _file_entry(
    value, base_dir, label, *, max_bytes=npu_contract.MAX_PINNED_FILE_BYTES,
) -> dict:
    _require(isinstance(value, dict), "%s must be an object" % label)
    raw_path = str(value.get("path") or "").strip()
    _require(raw_path, "%s needs a relative path" % label)
    _require(
        not os.path.isabs(raw_path)
        and not re.match(r"^[A-Za-z]:", raw_path)
        and not raw_path.startswith(("/", "\\")),
        "%s path must be relative to the manifest directory" % label,
    )
    parts = raw_path.replace("\\", "/").split("/")
    _require(
        all(part not in ("", ".", "..") for part in parts),
        "%s path must not traverse directories" % label,
    )
    base = Path(base_dir).resolve()
    resolved = (base / Path(*parts)).resolve()
    try:
        inside = os.path.commonpath([str(base), str(resolved)]) == str(base)
    except ValueError:
        inside = False
    _require(inside, "%s path escapes the manifest directory" % label)
    digest = str(value.get("sha256") or "").strip().lower()
    _require(_SHA256_RE.fullmatch(digest), "%s sha256 must be 64 hex chars" % label)
    size = value.get("bytes")
    _require(
        isinstance(size, int)
        and not isinstance(size, bool)
        and 0 < size <= max_bytes,
        "%s bytes must be between 1 and %d" % (label, max_bytes),
    )
    return {"path": "/".join(parts), "sha256": digest, "bytes": size}


def _identity(value, label) -> str:
    text = str(value or "").strip()
    _require(_IDENTITY_RE.fullmatch(text), "%s must be a short identity string" % label)
    return text


def _normalize_limits(raw, operation) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    deadline = npu_contract.clamp_deadline_ms(raw.get("deadline_ms"), operation)
    def _bounded(name, default, ceiling):
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return max(1, min(ceiling, value))
    return {
        "deadline_ms": deadline,
        "max_batch": _bounded("max_batch", 8, npu_contract.MAX_TEXT_ITEMS),
        "max_text_chars": _bounded(
            "max_text_chars", 4000, npu_contract.MAX_TEXT_CHARS,
        ),
    }


def _normalize_providers(raw) -> list:
    _require(
        isinstance(raw, (list, tuple)) and raw,
        "manifest providers must be a non-empty allowlist",
    )
    providers = []
    for item in raw:
        provider = str(item or "").strip().lower()
        _require(
            provider in npu_contract.PROVIDER_IDS,
            "unknown provider %r; valid: %s"
            % (provider, ", ".join(npu_contract.PROVIDER_IDS)),
        )
        _require(provider not in providers, "duplicate provider %r" % provider)
        providers.append(provider)
    return providers


def _normalize_provider_options(raw, base_dir, providers, extra_files) -> dict:
    if raw in (None, ""):
        raw = {}
    _require(isinstance(raw, dict), "provider_options must be an object")
    options = {}
    for provider, values in raw.items():
        provider_id = str(provider or "").strip().lower()
        _require(
            provider_id in providers,
            "provider_options entry %r is not in the provider allowlist" % provider,
        )
        allowed = _PROVIDER_OPTION_KEYS.get(provider_id)
        _require(allowed, "provider_options has no options for %r" % provider)
        _require(isinstance(values, dict), "provider_options values must be objects")
        entry = {}
        for key, value in values.items():
            _require(key in allowed, "unknown option %r for %s" % (key, provider))
            if provider_id == "openvino" and key == "device_type":
                text = str(value or "").strip().upper()
                _require(
                    text == "NPU",
                    "OpenVINO device_type must be NPU for the NPU accelerator",
                )
                entry[key] = "NPU"
                continue
            raw_path = str(value or "").strip().replace("\\", "/")
            pinned = next(
                (
                    item for item in extra_files
                    if item["path"].casefold() == raw_path.casefold()
                ),
                None,
            )
            _require(
                pinned is not None,
                "provider option %s.%s must reference a hash-pinned extra_file"
                % (provider_id, key),
            )
            if provider_id == "qnn" and key == "backend_path":
                backend = Path(pinned["path"]).name.casefold()
                _require(
                    "qnnhtp" in backend and "cpu" not in backend and "gpu" not in backend,
                    "QNN backend_path must name an HTP/NPU backend",
                )
            entry[key] = pinned
        options[provider_id] = entry
    if "qnn" in providers:
        _require(
            isinstance((options.get("qnn") or {}).get("backend_path"), dict),
            "QNN providers require a hash-pinned HTP backend_path",
        )
    return options


def _normalize_space(raw) -> dict | None:
    if raw in (None, "", {}):
        return None
    _require(isinstance(raw, dict), "space declaration must be an object")
    model = _canonical_space_model(raw.get("model"))
    _require(model and ":" in model, "space.model must be a model identity")
    revision = str(raw.get("revision") or "").strip()
    _require(
        8 <= len(revision) <= 200 and revision.isprintable(),
        "space.revision must pin the exact serving revision",
    )
    dimension = raw.get("dimension")
    _require(
        isinstance(dimension, int)
        and not isinstance(dimension, bool)
        and 1 <= dimension <= npu_contract.MAX_DIMENSION,
        "space.dimension must pin the exact vector dimension",
    )
    return {"model": model, "revision": revision, "dimension": dimension}


def normalize_manifest(payload, base_dir) -> dict:
    """Validate one manifest payload into the canonical in-memory shape."""
    _require(isinstance(payload, dict), "manifest must be a JSON object")
    npu_contract.validate_json_shape(payload)
    _require(
        payload.get("schema") == SCHEMA_VERSION,
        "manifest schema must be %d" % SCHEMA_VERSION,
    )
    name = str(payload.get("name") or "").strip()
    _require(_NAME_RE.fullmatch(name), "manifest name must be a short slug")
    operation = str(payload.get("operation") or "").strip().lower()
    _require(
        operation in npu_contract.OPERATIONS,
        "manifest operation must be one of: %s"
        % ", ".join(npu_contract.OPERATIONS),
    )
    providers = _normalize_providers(payload.get("providers"))
    raw_extra_files = payload.get("extra_files") or []
    _require(isinstance(raw_extra_files, (list, tuple)), "extra_files must be a list")
    _require(
        len(raw_extra_files) <= npu_contract.MAX_PINNED_FILES - 1,
        "manifest declares too many pinned files",
    )
    extra_files = [
        _file_entry(item, base_dir, "extra file")
        for item in raw_extra_files
    ]
    manifest = {
        "schema": SCHEMA_VERSION,
        "name": name,
        "operation": operation,
        "model": _file_entry(
            payload.get("model"), base_dir, "model",
            max_bytes=npu_contract.MAX_MODEL_BYTES,
        ),
        "extra_files": extra_files,
        "providers": providers,
        "provider_options": _normalize_provider_options(
            payload.get("provider_options"), base_dir, providers, extra_files,
        ),
        "limits": _normalize_limits(payload.get("limits"), operation),
        "tokenizer": None,
        "input": None,
        "labels": None,
        "dimension": None,
        "pooling": None,
        "normalize": False,
        "preprocess": "",
        "postprocess": "",
        "space": None,
    }
    if operation == "routing":
        raw_input = payload.get("input")
        _require(isinstance(raw_input, dict), "routing manifest needs an input object")
        input_dim = raw_input.get("dimension")
        _require(
            isinstance(input_dim, int)
            and not isinstance(input_dim, bool)
            and 1 <= input_dim <= npu_contract.MAX_FEATURES,
            "routing input dimension must be between 1 and %d"
            % npu_contract.MAX_FEATURES,
        )
        manifest["input"] = {
            "identity": _identity(raw_input.get("identity"), "input identity"),
            "dimension": input_dim,
        }
        labels = payload.get("labels")
        _require(
            isinstance(labels, (list, tuple))
            and set(str(item) for item in labels) == set(npu_contract.ROUTE_MODES)
            and len(labels) == len(npu_contract.ROUTE_MODES),
            "routing labels must be exactly: %s"
            % ", ".join(npu_contract.ROUTE_MODES),
        )
        manifest["labels"] = [str(item) for item in labels]
        postprocess = str(payload.get("postprocess") or "softmax").strip().lower()
        _require(
            postprocess in _ROUTE_POSTPROCESS,
            "routing postprocess must be one of: %s" % ", ".join(_ROUTE_POSTPROCESS),
        )
        manifest["postprocess"] = postprocess
    else:
        dimension = payload.get("dimension")
        _require(
            isinstance(dimension, int)
            and not isinstance(dimension, bool)
            and 1 <= dimension <= npu_contract.MAX_DIMENSION,
            "embedding dimension must be between 1 and %d"
            % npu_contract.MAX_DIMENSION,
        )
        manifest["dimension"] = dimension
        pooling = str(payload.get("pooling") or "mean").strip().lower()
        _require(
            pooling in _POOLINGS,
            "embedding pooling must be one of: %s" % ", ".join(_POOLINGS),
        )
        manifest["pooling"] = pooling
        normalize = payload.get("normalize", True)
        _require(
            isinstance(normalize, bool),
            "embedding normalize must be a literal boolean",
        )
        manifest["normalize"] = normalize
        manifest["preprocess"] = _identity(
            payload.get("preprocess"), "embedding preprocess identity",
        )
        postprocess = str(payload.get("postprocess") or "l2norm").strip().lower()
        manifest["postprocess"] = _identity(postprocess, "embedding postprocess")
        tokenizer = payload.get("tokenizer")
        if tokenizer not in (None, "", {}):
            _require(isinstance(tokenizer, dict), "tokenizer must be an object")
            kind = str(tokenizer.get("type") or "").strip().lower()
            _require(
                kind == "hf-tokenizers",
                "tokenizer type must be hf-tokenizers",
            )
            manifest["tokenizer"] = {
                "type": kind,
                **_file_entry(tokenizer, base_dir, "tokenizer"),
            }
        manifest["space"] = _normalize_space(payload.get("space"))
        if manifest["space"] is not None:
            _require(
                manifest["space"]["dimension"] == dimension,
                "space.dimension must equal the manifest output dimension",
            )
    pinned = [manifest["model"], *manifest["extra_files"]]
    if manifest.get("tokenizer"):
        pinned.append(manifest["tokenizer"])
    _require(
        len(pinned) <= npu_contract.MAX_PINNED_FILES,
        "manifest declares too many pinned files",
    )
    paths = [entry["path"].casefold() for entry in pinned]
    _require(len(paths) == len(set(paths)), "manifest has duplicate pinned file paths")
    _require(
        sum(entry["bytes"] for entry in pinned) <= npu_contract.MAX_BUNDLE_BYTES,
        "manifest pinned bundle exceeds %d bytes" % npu_contract.MAX_BUNDLE_BYTES,
    )
    core = {
        key: manifest[key]
        for key in sorted(manifest)
    }
    manifest["manifest_hash"] = _hash_payload(core)
    manifest["dir"] = str(Path(base_dir).resolve())
    return manifest


def _hash_payload(payload) -> str:
    import hashlib

    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_files(manifest) -> str:
    """Re-verify every pinned file; '' when intact, else a bounded reason."""
    base = Path(manifest.get("dir") or "").resolve()
    entries = [("model", manifest.get("model"))]
    entries.extend(
        ("extra file", item) for item in manifest.get("extra_files") or []
    )
    if manifest.get("tokenizer"):
        entries.append(("tokenizer", manifest["tokenizer"]))
    for label, entry in entries:
        if not entry:
            continue
        try:
            unresolved = base / entry["path"]
            if not unresolved.exists():
                return "missing %s: %s" % (label, entry["path"])
            target = unresolved.resolve(strict=True)
            inside = os.path.normcase(os.path.commonpath([str(base), str(target)]))
            if inside != os.path.normcase(str(base)) or not target.is_file():
                return "invalid %s path: %s" % (label, entry["path"])
            digest = npu_contract.sha256_file_bounded(
                target, entry["bytes"], npu_contract.MAX_PINNED_FILE_BYTES,
            )
            if digest != entry["sha256"]:
                return "hash drift for %s: %s" % (label, entry["path"])
        except (OSError, ValueError) as exc:
            return "unreadable %s: %s" % (
                label, npu_contract.sanitize_error(exc, 120),
            )
    return ""


def file_fingerprints(manifest) -> tuple:
    """Capture cheap source metadata for an already fully verified session."""
    base = Path(manifest.get("dir") or "").resolve()
    entries = [manifest.get("model"), *(manifest.get("extra_files") or [])]
    if manifest.get("tokenizer"):
        entries.append(manifest["tokenizer"])
    rows = []
    for entry in entries:
        if not entry:
            continue
        target = (base / entry["path"]).resolve(strict=True)
        inside = os.path.normcase(os.path.commonpath([str(base), str(target)]))
        if inside != os.path.normcase(str(base)) or not target.is_file():
            raise ValueError("invalid pinned path %s" % entry["path"])
        info = os.stat(target, follow_symlinks=False)
        rows.append((
            entry["path"], int(info.st_dev), int(info.st_ino),
            int(info.st_size), int(info.st_mtime_ns),
        ))
    return tuple(rows)


def fingerprint_drift(manifest, expected) -> str:
    """Cheap per-call drift check; loaded bytes/staged assets stay immutable."""
    if not isinstance(expected, tuple):
        return "load-time file fingerprint is unavailable"
    try:
        current = file_fingerprints(manifest)
    except (OSError, TypeError, ValueError) as exc:
        return "pinned file metadata unavailable: %s" % (
            npu_contract.sanitize_error(exc, 120)
        )
    if current != expected:
        return "pinned file metadata changed after session load"
    return ""


def load_manifests(directory=None) -> list:
    """Load every *.json manifest; invalid ones are flagged, never fatal."""
    base = Path(directory) if directory is not None else manifest_dir()
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.glob("*.json")):
        row = {"name": path.stem, "path": str(path), "error": ""}
        try:
            with path.open("rb") as stream:
                opened_size = os.fstat(stream.fileno()).st_size
                if opened_size > MAX_MANIFEST_BYTES:
                    raise ValueError(
                        "manifest exceeds %d bytes" % MAX_MANIFEST_BYTES
                    )
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError(
                    "manifest exceeds %d bytes" % MAX_MANIFEST_BYTES
                )
            payload = json.loads(raw.decode("utf-8"))
            npu_contract.validate_json_shape(payload)
            manifest = normalize_manifest(payload, base)
            row = {**manifest, "path": str(path), "error": ""}
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            row["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        rows.append(row)
    return rows


def active_manifest(operation, rows):
    """Deterministically select one valid manifest for an operation."""
    valid = sorted(
        (
            row for row in rows or []
            if not row.get("error") and row.get("operation") == operation
        ),
        key=lambda row: row.get("name") or "",
    )
    return valid[0] if valid else None
