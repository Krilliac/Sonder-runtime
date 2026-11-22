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
    "bearer_token", "url_credential", "jwt",
})
MAX_TIMEOUT_SECONDS = 120.0
SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".png", ".jpg", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".zip", ".gz", ".tar", ".jar",
})
# Heavy or vendored trees only, pruned by name at every depth. Every other
# dot-directory and dotfile is walked (``.github/workflows``, ``.vscode``,
# ``.circleci``, ``.config/<tool>``): ordinary repository files a token is
# often hard-coded into. Credential stores among them (``.env*``, ``.aws``,
# ``.ssh``, ``.npmrc``, ``.git-credentials``, ...) are still counted, never
# opened -- see ``file_ops.is_credential_store``.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
})


def _confined(path, root):
    """The real location of *path* when the scan may open or enter it, else None.

    A symlink or junction is refused outright, whatever it points at: followed,
    it reaches a credential store under a harmless name or a tree outside the
    allowed roots. The entry is then resolved and its real location must still
    lie inside the resolved scan root, so a link the attribute check cannot see
    does not carry the walk out either. Fails closed on any metadata error.
    """
    try:
        if file_ops.is_reparse_point(path):
            return None
        real = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if real != root and root not in real.parents:
        return None
    return real


# Public name for the other secret_scan entry point (harness_tools.secret_scan).
confined_path = _confined


def _scan_resolved(resolved: Path, *, timeout=30):
    """Scan an already-authorized, resolved directory without returning secrets."""
    bounded_timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    deadline = time.monotonic() + bounded_timeout
    scanner = CredentialScanner(max_bytes=MAX_FILE_BYTES)
    findings = []
    counts = {True: 0, False: 0}
    caps = {True: MAX_HIGH_CONFIDENCE_FINDINGS, False: MAX_FINDINGS}
    files_scanned = 0
    credential_files_skipped = 0
    links_skipped = 0
    truncated = False
    timed_out = False
    # os.walk with followlinks=False, and every directory re-checked before it
    # is entered: Path.rglob descended Windows junctions, and is_file/read_bytes
    # followed a file symlink to its target.
    for base, dirs, files in os.walk(resolved, followlinks=False):
        base_path = Path(base)
        entered = []
        for name in sorted(dirs):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if name in SKIP_DIRS:
                continue
            if _confined(base_path / name, resolved) is None:
                links_skipped += 1
                continue
            entered.append(name)
        if timed_out:
            dirs[:] = []
            truncated = True
            break
        dirs[:] = entered
        for name in sorted(files):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            path = base_path / name
            relative = path.relative_to(resolved)
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            real = _confined(path, resolved)
            if real is None:
                links_skipped += 1
                continue
            if file_ops.is_credential_store(path) or file_ops.is_credential_store(real):
                # A credential store (a key pair, ``.env``, cloud credentials) is
                # one file_read refuses whatever the token: a finding's file, line
                # and kind would fingerprint it, so it is counted, never opened.
                # Judged on the walked name and on the real location.
                credential_files_skipped += 1
                continue
            try:
                info = real.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    continue
                content = real.read_bytes()
                matches = scanner.scan(content)
                files_scanned += 1
            except (OSError, ValueError):
                continue
            for match in matches:
                high = match.kind in HIGH_CONFIDENCE_KINDS
                if counts[high] >= caps[high]:
                    # This class is full; the other may still have room.
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
        if not timed_out and time.monotonic() >= deadline:
            timed_out = True
        if timed_out or all(counts[kind] >= caps[kind] for kind in caps):
            truncated = True
            break
    return {
        "ok": True, "root": str(resolved), "findings": findings,
        "files_scanned": files_scanned, "truncated": truncated,
        "timed_out": timed_out, "timeout": timeout,
        "credential_files_skipped": credential_files_skipped,
        "links_skipped": links_skipped,
    }


def scan(root=".", *, timeout=30, extra_roots=""):
    """Scan an authorized tree without returning credential material."""
    resolved = file_ops.resolve_path(root, extra_roots=extra_roots)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    return _scan_resolved(resolved, timeout=timeout)


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
    if result.get("credential_files_skipped"):
        lines.append(
            "  skipped %d credential-store file(s) (not opened)"
            % result["credential_files_skipped"]
        )
    if result.get("links_skipped"):
        lines.append(
            "  skipped %d symlink or junction entr%s (not followed)"
            % (result["links_skipped"], "y" if result["links_skipped"] == 1 else "ies")
        )
    if result.get("truncated"):
        lines.append(
            "  ... (truncated at %d name-based or %d key-shaped findings, or timeout)"
            % (MAX_FINDINGS, MAX_HIGH_CONFIDENCE_FINDINGS)
        )
    return "\n".join(lines)


__all__ = ["format_result", "scan"]
