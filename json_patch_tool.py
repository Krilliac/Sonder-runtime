"""Guarded, bounded RFC 6902 subset for local JSON project files."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

import file_ops


MAX_DOCUMENT_BYTES = 256_000
MAX_OPERATIONS_BYTES = 128_000
MAX_OPERATIONS = 100
MAX_JSON_DEPTH = 64
MAX_POINTER_SEGMENTS = 64
MAX_OUTPUT_BYTES = 384_000
_ALLOWED_OPS = frozenset({"add", "remove", "replace", "test"})
_PATCH_LOCK = threading.RLock()


class JsonPatchError(RuntimeError):
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


def _reject_constant(value):
    raise ValueError("non-finite JSON number is not allowed: %s" % value)


def _reject_duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def _loads_strict(text: str, label: str):
    try:
        return json.loads(
            text, parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_object,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be strict UTF-8 JSON: %s" % (label, exc)) from exc


def _json_depth(value) -> int:
    stack = [(value, 1)]
    maximum = 0
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting exceeds max depth (%d)" % MAX_JSON_DEPTH)
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return maximum


def _requested_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _guard_target(path: str, *, extra_roots: str, bypass: bool, apply: bool):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if file_ops._foreign_absolute(path.strip()):
        raise PermissionError("path uses a non-native absolute form")
    requested = _requested_path(path)
    file_ops._require_no_reparse_components(requested)
    resolved = file_ops.require_read_access(
        path, extra_roots=extra_roots, bypass=bypass,
    )
    roots = file_ops.allowed_roots(extra_roots if bypass else "")
    root = next((item for item in roots if resolved == item or file_ops._is_inside(resolved, item)), None)
    if root is None:
        raise PermissionError("JSON patch target is outside every authorized root")
    if (
        file_ops._is_protected_read_path(resolved)
        or file_ops._is_protected_mutation_path(resolved)
        or any(
            part.casefold() in file_ops.SENSITIVE_READ_DIRECTORIES
            for part in resolved.parts
        )
    ):
        raise PermissionError("JSON patch target is secret or control state")
    if apply:
        file_ops._require_mutation_access(resolved, False)
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("JSON patch target must be an existing regular file")
    if file_ops._is_reparse_point(resolved):
        raise PermissionError("refusing JSON patch through a symlink or junction")
    return requested, resolved


def _read_snapshot(path: Path) -> tuple[bytes, tuple[int, int], int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("JSON patch target is not a regular file")
        if metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("JSON document exceeds max bytes (%d)" % MAX_DOCUMENT_BYTES)
        chunks = []
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError("JSON document exceeds max bytes (%d)" % MAX_DOCUMENT_BYTES)
        return raw, (metadata.st_dev, metadata.st_ino), stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _parse_operations(operations):
    if isinstance(operations, str):
        encoded = operations.encode("utf-8")
        if len(encoded) > MAX_OPERATIONS_BYTES:
            raise ValueError("patch operations exceed max input bytes (%d)" % MAX_OPERATIONS_BYTES)
        operations = _loads_strict(operations, "operations_json")
    else:
        try:
            encoded = json.dumps(
                operations, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("patch operations must be strict JSON: %s" % exc) from exc
        if len(encoded) > MAX_OPERATIONS_BYTES:
            raise ValueError("patch operations exceed max input bytes (%d)" % MAX_OPERATIONS_BYTES)
        # A caller may reuse a Python-list patch. Parse the serialized form so
        # nested values are owned by this invocation instead of being inserted
        # into the document by reference and mutated by later operations.
        operations = _loads_strict(encoded.decode("utf-8"), "operations_json")
    if not isinstance(operations, list) or not operations:
        raise ValueError("patch operations must be a non-empty JSON array")
    if len(operations) > MAX_OPERATIONS:
        raise ValueError("patch exceeds max operations (%d)" % MAX_OPERATIONS)
    _json_depth(operations)
    normalized = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError("operation %d must be an object" % index)
        op = operation.get("op")
        path = operation.get("path")
        if op not in _ALLOWED_OPS:
            raise ValueError("operation %d op must be add, remove, replace, or test" % index)
        required = {"op", "path", "value"} if op in {"add", "replace", "test"} else {"op", "path"}
        if set(operation) != required:
            raise ValueError(
                "operation %d must contain exactly: %s" % (index, ", ".join(sorted(required)))
            )
        tokens = _pointer_tokens(path, index)
        normalized.append((op, tokens, operation.get("value")))
    return normalized


def _pointer_tokens(pointer, index: int) -> list[str]:
    if not isinstance(pointer, str):
        raise ValueError("operation %d path must be a JSON Pointer string" % index)
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("operation %d path must be empty or start with /" % index)
    raw_tokens = pointer[1:].split("/")
    if len(raw_tokens) > MAX_POINTER_SEGMENTS:
        raise ValueError("operation %d JSON Pointer exceeds max segments" % index)
    tokens = []
    for raw in raw_tokens:
        position = 0
        decoded = []
        while position < len(raw):
            if raw[position] != "~":
                decoded.append(raw[position])
                position += 1
                continue
            if position + 1 >= len(raw) or raw[position + 1] not in "01":
                raise ValueError("operation %d path has invalid JSON Pointer escape" % index)
            decoded.append("~" if raw[position + 1] == "0" else "/")
            position += 2
        tokens.append("".join(decoded))
    return tokens


def _array_index(token: str, length: int, *, allow_end: bool, allow_dash: bool) -> int:
    if token == "-":
        if allow_dash:
            return length
        raise ValueError("'-' is only valid for add to an array")
    if not token or (len(token) > 1 and token.startswith("0")) or not token.isascii() or not token.isdigit():
        raise ValueError("array index must be 0 or a non-zero decimal without leading zeros")
    index = int(token)
    maximum = length if allow_end else length - 1
    if index > maximum:
        raise ValueError("array index is out of bounds")
    return index


def _locate_parent(document, tokens):
    if not tokens:
        return None, None
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                raise ValueError("JSON Pointer parent does not exist")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, len(current), allow_end=False, allow_dash=False)]
        else:
            raise ValueError("JSON Pointer traverses a scalar value")
    return current, tokens[-1]


def _value_at(document, tokens):
    if not tokens:
        return document
    parent, token = _locate_parent(document, tokens)
    if isinstance(parent, dict):
        if token not in parent:
            raise ValueError("JSON Pointer target does not exist")
        return parent[token]
    if isinstance(parent, list):
        return parent[_array_index(token, len(parent), allow_end=False, allow_dash=False)]
    raise ValueError("JSON Pointer target parent is a scalar value")


def _json_equal(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool) or left is None or right is None:
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    return left == right


def _apply_operations(document, operations):
    for index, (op, tokens, value) in enumerate(operations):
        try:
            if op == "test":
                if not _json_equal(_value_at(document, tokens), value):
                    raise ValueError("test precondition failed")
                continue
            if not tokens:
                if op == "remove":
                    raise ValueError("removing the document root is not supported")
                document = value
                if not isinstance(document, (dict, list)):
                    raise ValueError("document root must remain an object or array")
                continue
            parent, token = _locate_parent(document, tokens)
            if isinstance(parent, dict):
                if op in {"remove", "replace"} and token not in parent:
                    raise ValueError("JSON Pointer target does not exist")
                if op == "remove":
                    del parent[token]
                else:
                    parent[token] = value
            elif isinstance(parent, list):
                position = _array_index(
                    token, len(parent), allow_end=op == "add", allow_dash=op == "add",
                )
                if op == "add":
                    parent.insert(position, value)
                elif op == "remove":
                    del parent[position]
                else:
                    parent[position] = value
            else:
                raise ValueError("JSON Pointer target parent is a scalar value")
            _json_depth(document)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("operation %d failed: %s" % (index, exc)) from exc
    return document


def _write_temp(directory: Path, payload: bytes, mode: int) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".sonder-json-patch-", suffix=".tmp", dir=directory)
    temp = Path(raw_path)
    try:
        os.chmod(temp, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temp
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _verify_written(path: Path, expected: bytes) -> None:
    if path.read_bytes() != expected:
        raise OSError("atomic JSON patch verification mismatch")


def _atomic_apply(requested: Path, resolved: Path, original: bytes, output: bytes,
                  identity: tuple[int, int], mode: int, *, path: str,
                  extra_roots: str, bypass: bool) -> None:
    replacement = None
    replaced = False
    committed_identity = None
    try:
        replacement = _write_temp(resolved.parent, output, mode)
        file_ops._require_no_reparse_components(requested)
        if file_ops.resolve_path(path, extra_roots=extra_roots, bypass=bypass) != resolved:
            raise PermissionError("JSON patch target resolution changed before commit")
        current_bytes, current_identity, _current_mode = _read_snapshot(resolved)
        if current_identity != identity:
            raise PermissionError("JSON patch target identity changed before commit")
        if current_bytes != original:
            raise PermissionError("JSON patch target content changed before commit")
        os.replace(replacement, resolved)
        replacement = None
        replaced = True
        committed = resolved.stat()
        committed_identity = (committed.st_dev, committed.st_ino)
        _verify_written(resolved, output)
    except Exception as exc:
        rollback_error = ""
        if replaced:
            rollback = None
            try:
                file_ops._require_no_reparse_components(requested)
                if file_ops.resolve_path(
                    path, extra_roots=extra_roots, bypass=bypass,
                ) != resolved:
                    raise PermissionError(
                        "JSON patch target resolution changed before rollback"
                    )
                current_bytes, current_identity, _current_mode = _read_snapshot(resolved)
                if current_identity != committed_identity:
                    raise PermissionError(
                        "JSON patch target identity changed before rollback"
                    )
                if current_bytes != output:
                    raise PermissionError(
                        "JSON patch target content changed before rollback"
                    )
                rollback = _write_temp(resolved.parent, original, mode)
                os.replace(rollback, resolved)
                rollback = None
                _verify_written(resolved, original)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            finally:
                if rollback is not None:
                    try:
                        rollback.unlink()
                    except OSError:
                        pass
        report = {
            "ok": False,
            "transaction": "rolled_back" if replaced and not rollback_error else "not_committed" if not replaced else "rollback_failed",
            "error": str(exc),
            "rollback_error": rollback_error,
        }
        raise JsonPatchError("atomic JSON patch failed", report) from exc
    finally:
        if replacement is not None:
            try:
                replacement.unlink()
            except OSError:
                pass


def patch_json(path: str, operations, *, mode: str = "preview",
               extra_roots: str = "", bypass: bool = False) -> dict:
    mode = str(mode or "preview").strip().lower()
    if mode not in {"preview", "apply"}:
        raise ValueError("mode must be preview or apply")
    apply = mode == "apply"
    parsed_operations = _parse_operations(operations)
    with _PATCH_LOCK:
        requested, resolved = _guard_target(
            path, extra_roots=extra_roots, bypass=bypass, apply=apply,
        )
        original, identity, file_mode = _read_snapshot(resolved)
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("JSON patch target must be strict UTF-8") from exc
        document = _loads_strict(text, "target document")
        if not isinstance(document, (dict, list)):
            raise ValueError("JSON patch target root must be an object or array")
        _json_depth(document)
        document = _apply_operations(document, parsed_operations)
        _json_depth(document)
        output = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode("utf-8")
        if len(output) > MAX_DOCUMENT_BYTES:
            raise ValueError("patched JSON document exceeds max bytes (%d)" % MAX_DOCUMENT_BYTES)
        report = {
            "ok": True,
            "mode": mode,
            "applied": apply,
            "path": str(resolved),
            "operations": len(parsed_operations),
            "bytes_before": len(original),
            "bytes_after": len(output),
            "sha256_before": hashlib.sha256(original).hexdigest(),
            "sha256_after": hashlib.sha256(output).hexdigest(),
            "document": document,
        }
        rendered = json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True,
        ).encode("utf-8")
        if len(rendered) > MAX_OUTPUT_BYTES:
            raise ValueError("JSON patch result exceeds max output bytes (%d)" % MAX_OUTPUT_BYTES)
        if apply:
            _atomic_apply(
                requested, resolved, original, output, identity, file_mode,
                path=path, extra_roots=extra_roots, bypass=bypass,
            )
        return report
