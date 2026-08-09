"""Strict, bounded and transactional unified text patching."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import file_ops

MAX_PATCH_BYTES = 1_000_000
MAX_FILES = 64
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 4_000_000
MAX_HUNKS = 4096
MAX_PATCH_LINES = 100_000
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


class TextPatchError(RuntimeError):
    """Rejected patch or transaction, with machine-readable evidence."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {"ok": False, "error": message}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch_path(value: str, *, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    # Timestamps and quoted paths are deliberately outside this narrow grammar.
    if not value or value != value.strip() or "\t" in value or "\\" in value:
        raise ValueError("patch paths must be unquoted relative POSIX paths")
    pure = PurePosixPath(value)
    win = PureWindowsPath(value)
    if pure.is_absolute() or win.is_absolute() or win.drive:
        raise PermissionError("patch path must be relative")
    parts = pure.parts
    if parts and parts[0] == prefix:
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PermissionError("patch path contains an empty, dot, or parent component")
    return "/".join(parts)


def _parse(patch: str) -> list[dict]:
    if not isinstance(patch, str) or not patch:
        raise ValueError("patch must be a non-empty string")
    if "\x00" in patch:
        raise ValueError("patch contains NUL/binary data")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError("patch exceeds max patch bytes")
    lines = patch.splitlines(keepends=True)
    if len(lines) > MAX_PATCH_LINES:
        raise ValueError("patch exceeds max patch lines")
    files, i, hunk_count = [], 0, 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            raise ValueError("expected strict unified-diff '---' header at line %d" % (i + 1))
        old_raw = lines[i][4:].rstrip("\r\n"); i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("expected '+++' header at line %d" % (i + 1))
        new_raw = lines[i][4:].rstrip("\r\n"); i += 1
        old = _patch_path(old_raw, prefix="a")
        new = _patch_path(new_raw, prefix="b")
        if new is None:
            raise ValueError("file deletion is not supported")
        if old is not None and old != new:
            raise ValueError("file rename is not supported")
        item = {"path": new, "action": "create" if old is None else "modify", "hunks": []}
        while i < len(lines) and lines[i].startswith("@@ "):
            match = _HUNK.match(lines[i].rstrip("\r\n"))
            if not match:
                raise ValueError("invalid hunk header at line %d" % (i + 1))
            old_start, old_count, new_start, new_count = (
                int(match.group(1)), int(match.group(2) or 1),
                int(match.group(3)), int(match.group(4) or 1),
            )
            i += 1; body = []
            while i < len(lines) and not lines[i].startswith(("@@ ", "--- ")):
                line = lines[i]
                if line.startswith("\\ No newline at end of file"):
                    if line.rstrip("\r\n") != "\\ No newline at end of file" or not body:
                        raise ValueError("invalid no-newline marker at line %d" % (i + 1))
                    if not body[-1][1].endswith(("\n", "\r")):
                        raise ValueError("duplicate no-newline marker")
                    body[-1] = (body[-1][0], body[-1][1].rstrip("\r\n"))
                elif line[:1] in {" ", "+", "-"}:
                    body.append((line[0], line[1:]))
                else:
                    raise ValueError("invalid hunk body at line %d" % (i + 1))
                i += 1
            actual_old = sum(tag != "+" for tag, _ in body)
            actual_new = sum(tag != "-" for tag, _ in body)
            if (actual_old, actual_new) != (old_count, new_count):
                raise ValueError("hunk line counts do not match header")
            item["hunks"].append((old_start, old_count, new_start, new_count, body))
            hunk_count += 1
            if hunk_count > MAX_HUNKS:
                raise ValueError("patch exceeds max hunk count")
        if not item["hunks"]:
            raise ValueError("file section must contain at least one hunk")
        files.append(item)
        if len(files) > MAX_FILES:
            raise ValueError("patch exceeds max file count")
    seen = set()
    for item in files:
        key = os.path.normcase(item["path"]).casefold()
        if key in seen:
            raise ValueError("duplicate or case-colliding patch target")
        seen.add(key)
    return files


def _apply_hunks(source: str, hunks: list, *, create: bool) -> tuple[str, int, int]:
    source_lines = source.splitlines(keepends=True)
    # Unified diffs conventionally use LF even when the target is CRLF. Treat
    # line terminators as transport syntax while preserving the target's
    # established newline convention; all non-newline text remains exact.
    eol = next(("\r\n" if line.endswith("\r\n") else "\n"
                for line in source_lines if line.endswith(("\n", "\r\n"))), "\n")
    def target_line(text: str) -> str:
        if text.endswith("\r\n"):
            return text[:-2] + eol
        if text.endswith("\n") or text.endswith("\r"):
            return text[:-1] + eol
        return text
    output, cursor, added, removed = [], 0, 0, 0
    for old_start, old_count, new_start, new_count, body in hunks:
        # Unified-diff zero-length ranges name the line *before* the insertion
        # or deletion point; non-empty ranges name their first one-based line.
        expected = old_start if old_count == 0 else old_start - 1
        if expected < cursor or expected > len(source_lines):
            raise ValueError("hunk old range is out of order or out of bounds")
        if create and (old_start != 0 or old_count != 0):
            raise ValueError("create hunks must use old range -0,0")
        output.extend(source_lines[cursor:expected]); cursor = expected
        # New coordinates must describe the output position at this point.
        expected_new_index = new_start if new_count == 0 else new_start - 1
        if expected_new_index != len(output):
            raise ValueError("hunk new range is inconsistent with prior hunks")
        for tag, text in body:
            text = target_line(text)
            if tag in {" ", "-"}:
                if cursor >= len(source_lines) or source_lines[cursor] != text:
                    raise ValueError("hunk context does not exactly match source")
                cursor += 1
            if tag in {" ", "+"}:
                output.append(text)
            if tag == "+": added += 1
            elif tag == "-": removed += 1
    output.extend(source_lines[cursor:])
    return "".join(output), added, removed


def _safe_relative(root: Path, relative: str, *, extra_roots: str, developer_authorized: bool) -> Path:
    requested = (root / Path(*PurePosixPath(relative).parts)).absolute()
    file_ops._require_no_reparse_components(requested)
    target = file_ops.resolve_repository_read_path(
        str(requested),
        reject_sensitive=True, extra_roots=extra_roots,
    )
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("patch target escapes project root") from exc
    file_ops._require_no_reparse_components(target)
    file_ops._require_mutation_access(target, developer_authorized)
    return target


def _stage(path: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".sonder-patch-", dir=str(path.parent))
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        return staged
    except BaseException:
        try: staged.unlink()
        except OSError: pass
        raise


def text_patch(root: str, patch: str, *, apply: bool = False,
               extra_roots: str = "", developer_authorized: bool = False) -> dict:
    requested_root = Path(root).expanduser()
    if not requested_root.is_absolute():
        requested_root = file_ops.workspace_root() / requested_root
    file_ops._require_no_reparse_components(requested_root.absolute())
    project = file_ops.resolve_repository_read_path(
        root, allow_workspace_root=True, reject_sensitive=True, extra_roots=extra_roots,
    )
    if not project.is_dir():
        raise ValueError("root must be an existing directory")
    parsed = _parse(patch)
    prepared, total_before, total_after = [], 0, 0
    for item in parsed:
        target = _safe_relative(project, item["path"], extra_roots=extra_roots,
                                developer_authorized=developer_authorized)
        exists = target.exists()
        if item["action"] == "create" and exists:
            raise FileExistsError("create target already exists: %s" % item["path"])
        if item["action"] == "modify" and not exists:
            raise FileNotFoundError("modify target does not exist: %s" % item["path"])
        if exists and (not target.is_file() or file_ops._is_reparse_point(target)):
            raise ValueError("target is not a regular non-link file: %s" % item["path"])
        if not exists and not target.parent.is_dir():
            raise FileNotFoundError("create target parent does not exist: %s" % item["path"])
        original = target.read_bytes() if exists else b""
        if len(original) > MAX_FILE_BYTES or b"\x00" in original:
            raise ValueError("source exceeds cap or contains binary data: %s" % item["path"])
        try: source = original.decode("utf-8")
        except UnicodeDecodeError as exc: raise ValueError("source is not UTF-8: %s" % item["path"]) from exc
        result, added, removed = _apply_hunks(source, item["hunks"], create=not exists)
        encoded = result.encode("utf-8")
        if b"\x00" in encoded or len(encoded) > MAX_FILE_BYTES:
            raise ValueError("patched file exceeds cap or contains binary data: %s" % item["path"])
        total_before += len(original); total_after += len(encoded)
        if total_before > MAX_TOTAL_BYTES or total_after > MAX_TOTAL_BYTES:
            raise ValueError("patch exceeds aggregate byte cap")
        stat_sig = None if not exists else (target.stat().st_dev, target.stat().st_ino,
                                             target.stat().st_size, target.stat().st_mtime_ns)
        prepared.append({**item, "target": target, "original": original, "output": encoded,
                         "before_sha256": _sha(original) if exists else None,
                         "after_sha256": _sha(encoded), "stat_sig": stat_sig,
                         "additions": added, "deletions": removed})
    report_files = [{k: row[k] for k in ("path", "action", "before_sha256", "after_sha256",
                                          "additions", "deletions")} |
                    {"bytes_before": len(row["original"]), "bytes_after": len(row["output"])}
                    for row in prepared]
    if not apply:
        return {"ok": True, "applied": False, "root": str(project), "files": report_files,
                "limits": {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
                           "max_total_bytes": MAX_TOTAL_BYTES, "max_patch_bytes": MAX_PATCH_BYTES}}

    staged, published = {}, []
    try:
        # Revalidate the complete transaction immediately before staging/publication.
        for row in prepared:
            target = row["target"]
            _safe_relative(project, row["path"], extra_roots=extra_roots,
                           developer_authorized=developer_authorized)
            if row["action"] == "create":
                if target.exists(): raise RuntimeError("create target changed after preflight")
            else:
                st = target.stat()
                sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                if sig != row["stat_sig"] or _sha(target.read_bytes()) != row["before_sha256"]:
                    raise RuntimeError("modify target changed after preflight")
        for row in prepared: staged[row["path"]] = _stage(row["target"], row["output"])
        for row in prepared:
            temp, target = staged[row["path"]], row["target"]
            if row["action"] == "create":
                file_ops._require_no_reparse_components(target.parent)
                os.link(temp, target); temp.unlink()
            else:
                file_ops._require_no_reparse_components(target)
                if file_ops._is_reparse_point(target):
                    raise RuntimeError("modify target became a link before publication")
                st = target.stat()
                current_sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                if current_sig != row["stat_sig"] or _sha(target.read_bytes()) != row["before_sha256"]:
                    raise RuntimeError("modify target changed before publication")
                os.replace(temp, target)
            published.append(row)
        return {"ok": True, "applied": True, "root": str(project), "files": report_files,
                "transaction": "committed"}
    except BaseException as exc:
        rollback_errors = []
        for row in reversed(published):
            target = row["target"]
            try:
                if not target.is_file() or file_ops._is_reparse_point(target) or _sha(target.read_bytes()) != row["after_sha256"]:
                    raise RuntimeError("target changed; refusing unsafe rollback")
                if row["action"] == "create": target.unlink()
                else: os.replace(_stage(target, row["original"]), target)
            except BaseException as rollback_exc:
                rollback_errors.append({"path": row["path"], "error": str(rollback_exc)})
        raise TextPatchError("text patch transaction failed: %s" % exc, {
            "ok": False, "applied": False, "transaction": "rollback_incomplete" if rollback_errors else "rolled_back",
            "error": str(exc), "rollback_errors": rollback_errors, "files": report_files,
        }) from exc
    finally:
        for temp in staged.values():
            try: temp.unlink(missing_ok=True)
            except OSError: pass
