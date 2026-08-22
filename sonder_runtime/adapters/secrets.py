"""API-key rotation with an overlap window (SPEC-2 section 5 / WP2).

Rotation writes a new ``SONDER_API_KEY`` into the secrets environment
file and records only the SHA-256 *hash* of the previous key together
with a mandatory expiry in ``SONDER_HOME/secrets/rotation.json`` (mode
0600).  During the overlap window the server accepts current or
previous; after expiry only current.  The plaintext previous key is
never stored, and no command ever prints a key — the operator reads the
new key from the secrets file they already control.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import time
from pathlib import Path

from sonder_runtime.platform import paths as runtime_paths

DEFAULT_OVERLAP_SECONDS = 24 * 3600
MIN_OVERLAP_SECONDS = 60
MAX_OVERLAP_SECONDS = 7 * 24 * 3600

_KEY_LINE = re.compile(r"^\s*SONDER_API_KEY\s*=", re.MULTILINE)


class RotationError(RuntimeError):
    pass


def rotation_state_path() -> Path:
    override = os.environ.get("SONDER_ROTATION_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return runtime_paths.ensure_home() / "secrets" / "rotation.json"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _create_private_if_missing(path: Path, content: str) -> bool:
    """Atomically create private state without replacing an existing value.

    This is deliberately separate from ``_write_private``.  Some secrets,
    notably the generated account-session HMAC key, must have one stable
    value across simultaneous first starts.  Replacing that file lets two
    processes each hash sessions with a different key during the race.

    Returns ``True`` only to the process that created the file.  ``O_EXCL``
    has the required create-once semantics on both POSIX and Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def load_rotation_state(path: Path | None = None) -> dict | None:
    state_path = path or rotation_state_path()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("previous_key_sha256"), str):
        return None
    try:
        expiry = float(raw.get("previous_expires_at", 0))
    except (TypeError, ValueError):
        return None
    return {
        "previous_key_sha256": raw["previous_key_sha256"],
        "previous_expires_at": expiry,
        "rotated_at": raw.get("rotated_at"),
    }


def previous_key_valid(
    presented: str, *, state_path: Path | None = None, now: float | None = None
) -> bool:
    """Constant-time check of a presented key against the previous-key hash.

    Returns False once the overlap window has expired — the mandatory
    expiration is enforced here, not by cleanup jobs.
    """
    if not presented:
        return False
    state = load_rotation_state(state_path)
    if state is None:
        return False
    current_time = time.time() if now is None else now
    if current_time >= state["previous_expires_at"]:
        return False
    return hmac.compare_digest(
        _hash_key(presented), state["previous_key_sha256"]
    )


def generate_key() -> str:
    return _secrets.token_urlsafe(32)


def rotate_api_key(
    secrets_file: str | os.PathLike,
    *,
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
    state_path: Path | None = None,
    new_key: str | None = None,
) -> dict:
    """Rotate SONDER_API_KEY in the secrets file; return a redacted report."""
    if not MIN_OVERLAP_SECONDS <= overlap_seconds <= MAX_OVERLAP_SECONDS:
        raise RotationError(
            f"overlap must be {MIN_OVERLAP_SECONDS}..{MAX_OVERLAP_SECONDS} seconds"
        )
    path = Path(secrets_file).expanduser()
    if not path.exists():
        raise RotationError(f"secrets file not found: {path}")
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise RotationError(
                f"secrets file {path} must not be group/world accessible"
            )
    content = path.read_text(encoding="utf-8")

    current = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("SONDER_API_KEY="):
            current = stripped.partition("=")[2].strip().strip("'\"")
    replacement = new_key or generate_key()
    if current and hmac.compare_digest(current, replacement):
        raise RotationError("new key must differ from the current key")

    if _KEY_LINE.search(content):
        content = _KEY_LINE.sub("SONDER_API_KEY=", content, count=0)
        # Replace only the value on each SONDER_API_KEY line.
        content = re.sub(
            r"(?m)^(\s*SONDER_API_KEY=).*$",
            lambda m: m.group(1) + replacement,
            content,
        )
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"SONDER_API_KEY={replacement}\n"
    _write_private(path, content)

    expiry = time.time() + overlap_seconds
    if current:
        _write_private(
            state_path or rotation_state_path(),
            json.dumps(
                {
                    "previous_key_sha256": _hash_key(current),
                    "previous_expires_at": expiry,
                    "rotated_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                },
                indent=2,
            ),
        )
    return {
        "secrets_file": str(path),
        "previous_accepted_until": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry)
        )
        if current
        else None,
        "note": "read the new key from the secrets file; it is never printed",
    }
