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

import npu_contract
import sonder_paths


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
    return Path(sonder_paths.state_path("npu-manifests", "SONDER_NPU_MANIFEST_DIR"))


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


def _file_entry(value, base_dir, label) -> dict:
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
        isinstance(size, int) and not isinstance(size, bool) and size > 0,
        "%s bytes must be a positive integer" % label,
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


def _normalize_provider_options(raw) -> dict:
    if raw in (None, ""):
        return {}
    _require(isinstance(raw, dict), "provider_options must be an object")
    options = {}
    for provider, values in raw.items():
        allowed = _PROVIDER_OPTION_KEYS.get(str(provider or "").strip().lower())
        _require(allowed, "provider_options has no options for %r" % provider)
        _require(isinstance(values, dict), "provider_options values must be objects")
        entry = {}
        for key, value in values.items():
            _require(key in allowed, "unknown option %r for %s" % (key, provider))
            text = str(value or "").strip()
            _require(
                0 < len(text) <= 260 and "\x00" not in text and "\n" not in text,
                "provider option %s.%s must be a short string" % (provider, key),
            )
            entry[key] = text
        options[str(provider).strip().lower()] = entry
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
    return {"model": model, "revision": revision}


def normalize_manifest(payload, base_dir) -> dict:
    """Validate one manifest payload into the canonical in-memory shape."""
    _require(isinstance(payload, dict), "manifest must be a JSON object")
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
    manifest = {
        "schema": SCHEMA_VERSION,
        "name": name,
        "operation": operation,
        "model": _file_entry(payload.get("model"), base_dir, "model"),
        "extra_files": [
            _file_entry(item, base_dir, "extra file")
            for item in (payload.get("extra_files") or [])
        ],
        "providers": _normalize_providers(payload.get("providers")),
        "provider_options": _normalize_provider_options(
            payload.get("provider_options")
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
        manifest["normalize"] = bool(payload.get("normalize", True))
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
            size = target.stat().st_size
            if size != entry["bytes"]:
                return "size mismatch for %s: %s" % (label, entry["path"])
            if npu_contract.sha256_file(target) != entry["sha256"]:
                return "hash drift for %s: %s" % (label, entry["path"])
        except (OSError, ValueError) as exc:
            return "unreadable %s: %s" % (
                label, npu_contract.sanitize_error(exc, 120),
            )
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
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError(
                    "manifest exceeds %d bytes" % MAX_MANIFEST_BYTES
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest = normalize_manifest(payload, base)
            row = {**manifest, "path": str(path), "error": ""}
        except (OSError, TypeError, ValueError) as exc:
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
