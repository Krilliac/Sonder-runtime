"""Bounded, redacting workspace secret scan adapter."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from ..application.security.secrets_and_fuzz import CredentialScanner
from .filesystem import file_ops


MAX_FILE_BYTES = 1_000_000
# Separate caps: name-based credential assignments are noisy in real code, and
# a shared cap let them crowd out every key with a known shape that sorted
# later in the tree. Shape findings get their own budget.
MAX_FINDINGS = 100
MAX_HIGH_CONFIDENCE_FINDINGS = 100
HIGH_CONFIDENCE_KINDS = frozenset({
    "private_key", "aws_access_key", "github_token", "api_token",
    "url_credential", "jwt",
})
MAX_TIMEOUT_SECONDS = 120.0
SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".png", ".jpg", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".zip", ".gz", ".tar", ".jar",
})
# Heavy or vendored trees only. Credential dotdirs (.aws, .ssh, .docker, ...)
# and dotfiles (.env*, .npmrc, .netrc, .pypirc, .git-credentials) are scanned
# at every depth.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
})


_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def _is_link_or_junction(path: str) -> bool:
    """Return whether path is a symlink or a Windows directory junction.

    os.walk(followlinks=False) still descends into junctions on Windows
    (islink() is False for them), so they are detected by reparse tag. A path
    that cannot be inspected is treated as a link: never descend blind.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return _MOUNT_POINT_TAG is not None and getattr(info, "st_reparse_tag", 0) == _MOUNT_POINT_TAG


def _inside(real: str, root_real: str) -> bool:
    real, root_real = os.path.normcase(real), os.path.normcase(root_real)
    try:
        return os.path.commonpath([real, root_real]) == root_real
    except ValueError:  # different drives
        return False


def _iter_files(root: Path):
    """Yield regular files under root without leaving it.

    SKIP_DIRS, symlinked directories and junctions are pruned before
    descending; each directory must still resolve inside root and is visited
    once by (st_dev, st_ino), so a link that slips past the name check can
    neither escape the authorized tree nor loop. Symlinked files are skipped.
    """
    root_real = os.path.realpath(root)
    visited = set()
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            info = os.stat(dirpath)
        except OSError:
            dirnames[:] = []
            continue
        identity = (info.st_dev, info.st_ino)
        if not _inside(os.path.realpath(dirpath), root_real) or (info.st_ino and identity in visited):
            dirnames[:] = []
            continue
        visited.add(identity)
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not _is_link_or_junction(os.path.join(dirpath, name))
        )
        base = Path(dirpath)
        for name in sorted(filenames):
            if not os.path.islink(os.path.join(dirpath, name)):
                yield base / name


def scan(root=".", *, timeout=30, extra_roots=""):
    """Scan an authorized tree without returning credential material."""
    resolved = file_ops.resolve_path(root, extra_roots=extra_roots)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    deadline = time.monotonic() + max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    scanner = CredentialScanner(max_bytes=MAX_FILE_BYTES)
    findings = []
    counts = {True: 0, False: 0}
    caps = {True: MAX_HIGH_CONFIDENCE_FINDINGS, False: MAX_FINDINGS}
    files_scanned = 0
    truncated = False
    for path in _iter_files(resolved):
        if time.monotonic() >= deadline:
            truncated = True
            break
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        relative = path.relative_to(resolved)
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files_scanned += 1
        try:
            content = path.read_bytes()
            matches = scanner.scan(content)
        except (OSError, ValueError):
            continue
        for match in matches:
            high = match.kind in HIGH_CONFIDENCE_KINDS
            if counts[high] >= caps[high]:
                truncated = True
                continue
            counts[high] += 1
            findings.append({
                "file": str(relative), "line": match.line,
                "type": match.kind, "match": match.redacted,
            })
        if all(counts[kind] >= caps[kind] for kind in caps):
            truncated = True
            break
    return {
        "ok": True, "root": str(resolved), "findings": findings,
        "files_scanned": files_scanned, "truncated": truncated,
    }


def format_result(result):
    lines = [
        "secret scan: %d finding(s) in %d files scanned"
        % (len(result.get("findings", [])), result.get("files_scanned", 0)),
    ]
    for finding in result.get("findings", []):
        lines.append(
            "  %s:%d  [%s]  %s"
            % (finding["file"], finding["line"], finding["type"], finding["match"])
        )
    if result.get("truncated"):
        lines.append(
            "  ... (truncated at %d name-based or %d key-shaped findings, or timeout)"
            % (MAX_FINDINGS, MAX_HIGH_CONFIDENCE_FINDINGS)
        )
    return "\n".join(lines)


__all__ = ["format_result", "scan"]
