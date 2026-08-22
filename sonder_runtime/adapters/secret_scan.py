"""Bounded, redacting workspace secret scan adapter."""
from __future__ import annotations

import time
from pathlib import Path

from ..application.security.secrets_and_fuzz import CredentialScanner
from .filesystem import file_ops


MAX_FILE_BYTES = 1_000_000
MAX_FINDINGS = 100
MAX_TIMEOUT_SECONDS = 120.0
SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".png", ".jpg", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".zip", ".gz", ".tar", ".jar",
})


def scan(root=".", *, timeout=30, extra_roots=""):
    """Scan an authorized tree without returning credential material."""
    resolved = file_ops.resolve_path(root, extra_roots=extra_roots)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    deadline = time.monotonic() + max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    scanner = CredentialScanner(max_bytes=MAX_FILE_BYTES)
    findings = []
    files_scanned = 0
    truncated = False
    for path in resolved.rglob("*"):
        if time.monotonic() >= deadline:
            truncated = True
            break
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        relative = path.relative_to(resolved)
        if any(part.startswith(".") and part != "." for part in relative.parts):
            if not str(relative).startswith(".env"):
                continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files_scanned += 1
        try:
            content = path.read_bytes()
            matches = scanner.scan(content)
        except (OSError, ValueError):
            continue
        for match in matches:
            findings.append({
                "file": str(relative), "line": match.line,
                "type": match.kind, "match": match.redacted,
            })
            if len(findings) >= MAX_FINDINGS:
                truncated = True
                break
        if truncated:
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
        lines.append("  ... (truncated at 100 findings or timeout)")
    return "\n".join(lines)


__all__ = ["format_result", "scan"]
