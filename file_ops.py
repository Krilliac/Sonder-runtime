"""Guarded filesystem operations for Trilobite tools.

Default policy is intentionally conservative: read/write/delete are limited to
approved roots, file sizes are bounded, and deletes dry-run unless explicitly
confirmed. Broader system access requires an explicit bypass decision by the
server layer.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import trilobite_paths


MAX_READ_BYTES = 256_000
MAX_WRITE_BYTES = 1_000_000
MAX_FIND_RESULTS = 200


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines()) or 1


def _read_text_if_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def workspace_root() -> Path:
    return Path(__file__).resolve().parent


def _split_roots(raw: str) -> list[Path]:
    roots = []
    for item in (raw or "").split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser())
    return roots


def allowed_roots(extra_roots: str = "") -> list[Path]:
    roots = [workspace_root(), Path(trilobite_paths.default_home())]
    roots.extend(_split_roots(os.environ.get("TRILOBITE_FILE_ROOTS", "")))
    roots.extend(_split_roots(extra_roots))
    out = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root.absolute()
        if resolved not in out:
            out.append(resolved)
    return out


def bypass_enabled() -> bool:
    return os.environ.get("TRILOBITE_FILE_BYPASS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def resolve_path(path: str, *, extra_roots: str = "", bypass: bool = False) -> Path:
    if not (path or "").strip():
        raise ValueError("empty path")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root() / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate.absolute()
    roots = allowed_roots(extra_roots if bypass else "")
    if bypass:
        return resolved
    if any(_is_inside(resolved, root) for root in roots):
        return resolved
    raise PermissionError(
        "path is outside allowed roots. Set TRILOBITE_FILE_ROOTS or use an approved admin/dev bypass."
    )


def policy_text(*, bypass: bool = False, extra_roots: str = "") -> str:
    lines = [
        "filesystem policy",
        "  mode: %s" % ("bypass" if bypass else "guarded"),
        "  delete default: dry-run unless confirm matches DELETE <path>",
        "  max read bytes: %d" % MAX_READ_BYTES,
        "  max write bytes: %d" % MAX_WRITE_BYTES,
        "  roots:",
    ]
    for root in allowed_roots(extra_roots if bypass else ""):
        lines.append("    - %s" % root)
    lines.extend([
        "  env bypass: TRILOBITE_FILE_BYPASS=1",
        "  approval code: TRILOBITE_FILE_APPROVAL_CODE plus approval=<code>",
        "  env extra roots: TRILOBITE_FILE_ROOTS=<path%spath>" % os.pathsep,
    ])
    return "\n".join(lines)


def find_files(
    query: str = "*",
    root: str = "",
    *,
    max_results: int = 50,
    extra_roots: str = "",
    bypass: bool = False,
) -> dict:
    root_path = resolve_path(root or ".", extra_roots=extra_roots, bypass=bypass)
    if not root_path.exists():
        raise FileNotFoundError(str(root_path))
    if not root_path.is_dir():
        raise ValueError("root is not a directory: %s" % root_path)
    pattern = (query or "*").strip() or "*"
    limit = max(1, min(MAX_FIND_RESULTS, int(max_results or 50)))
    results = []
    for base, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".pytest_cache", "venv", "__pycache__"}]
        names = dirs + files
        for name in names:
            rel = str((Path(base) / name).relative_to(root_path))
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern) or pattern.lower() in rel.lower():
                p = Path(base) / name
                results.append({
                    "path": str(p),
                    "relative": rel,
                    "type": "dir" if p.is_dir() else "file",
                    "bytes": p.stat().st_size if p.is_file() else 0,
                })
                if len(results) >= limit:
                    return {"root": str(root_path), "query": pattern, "results": results}
    return {"root": str(root_path), "query": pattern, "results": results}


def read_file(path: str, *, max_bytes: int = MAX_READ_BYTES, extra_roots: str = "", bypass: bool = False) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if not p.is_file():
        raise ValueError("path is not a file: %s" % p)
    size = p.stat().st_size
    limit = max(1, min(MAX_READ_BYTES, int(max_bytes or MAX_READ_BYTES)))
    data = p.read_bytes()[:limit]
    text = data.decode("utf-8", errors="replace")
    return {"path": str(p), "bytes": size, "truncated": size > limit, "text": text}


def write_file(
    path: str,
    content: str,
    *,
    mode: str = "create",
    extra_roots: str = "",
    bypass: bool = False,
) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    before = _read_text_if_file(p)
    before_lines = _line_count(before)
    data = (content or "").encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ValueError("content exceeds max write bytes")
    mode = (mode or "create").lower()
    if mode not in {"create", "overwrite", "append"}:
        raise ValueError("mode must be create, overwrite, or append")
    if mode == "create" and p.exists():
        raise FileExistsError(str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with p.open("a", encoding="utf-8", newline="") as f:
            f.write(content or "")
    else:
        p.write_text(content or "", encoding="utf-8", newline="")
    after_lines = _line_count(before + (content or "")) if mode == "append" else _line_count(content or "")
    if mode in {"create", "append"}:
        lines_added = _line_count(content or "")
        lines_deleted = 0
        lines_edited = 0
        action = "create" if mode == "create" else "append"
    else:
        lines_added = max(0, after_lines - before_lines)
        lines_deleted = max(0, before_lines - after_lines)
        lines_edited = min(before_lines, after_lines) if before != (content or "") else 0
        action = "overwrite"
    return {
        "path": str(p),
        "bytes": p.stat().st_size,
        "mode": mode,
        "action": action,
        "lines_before": before_lines,
        "lines_after": after_lines,
        "lines_added": lines_added,
        "lines_edited": lines_edited,
        "lines_deleted": lines_deleted,
    }


def edit_file(
    path: str,
    old: str,
    new: str,
    *,
    count: int = 1,
    extra_roots: str = "",
    bypass: bool = False,
) -> dict:
    if old == "":
        raise ValueError("old text must not be empty")
    current = read_file(path, extra_roots=extra_roots, bypass=bypass)
    if current["truncated"]:
        raise ValueError("file too large for safe text edit")
    text = current["text"]
    max_count = max(1, min(1000, int(count or 1)))
    occurrences = text.count(old)
    if occurrences == 0:
        raise ValueError("old text not found")
    next_text = text.replace(old, new or "", max_count)
    result = write_file(path, next_text, mode="overwrite", extra_roots=extra_roots, bypass=bypass)
    replacements = min(occurrences, max_count)
    result["replacements"] = min(occurrences, max_count)
    result["action"] = "edit"
    result["lines_added"] = _line_count(new or "") * replacements
    result["lines_deleted"] = _line_count(old or "") * replacements
    result["lines_edited"] = replacements
    return result


def delete_path(
    path: str,
    *,
    recursive: bool = False,
    dry_run: bool = True,
    confirm: str = "",
    extra_roots: str = "",
    bypass: bool = False,
) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    exists = p.exists()
    line_count = _line_count(_read_text_if_file(p))
    required = "DELETE %s" % p
    if dry_run or confirm != required:
        return {
            "path": str(p),
            "exists": exists,
            "dry_run": True,
            "deleted": False,
            "lines_deleted": 0,
            "would_delete_lines": line_count,
            "required_confirm": required,
        }
    if not exists:
        return {
            "path": str(p),
            "exists": False,
            "dry_run": False,
            "deleted": False,
            "lines_deleted": 0,
        }
    if p.is_dir():
        if not recursive:
            raise ValueError("directory delete requires recursive=True")
        for child in sorted(p.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        p.rmdir()
    else:
        p.unlink()
    return {
        "path": str(p),
        "exists": True,
        "dry_run": False,
        "deleted": True,
        "action": "delete",
        "lines_deleted": line_count,
    }
