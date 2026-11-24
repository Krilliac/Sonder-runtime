"""Small authenticated-encryption vault for durable fanout prompts.

The fanout receipt database is deliberately useful for progress inspection, but
it must not become a plaintext prompt store.  This module has no database or
logging dependency: it only turns a UTF-8 prompt into a Fernet token and back.
Invalid tokens and unavailable keys intentionally produce the same generic
exception so callers never accidentally expose prompt material in diagnostics.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import sonder_paths


class PromptVaultError(RuntimeError):
    """The prompt vault could not safely encrypt or decrypt a value."""


_FERNET_CACHE: dict[str, Fernet] = {}


def key_path() -> Path:
    """Return the operator override or the per-user fanout key location."""
    override = os.environ.get("SONDER_FANOUT_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return sonder_paths.default_home() / "secrets" / "fanout.key"


def _restrict(path: Path, mode: int) -> None:
    # Windows ACL policy is deployed separately; chmod is still a useful
    # best-effort restriction on POSIX and harmless where it has no effect.
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _create_key(path: Path) -> bytes:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(path.parent, 0o700)
        try:
            with path.open("xb") as handle:
                handle.write(Fernet.generate_key())
        except FileExistsError:
            pass
        _restrict(path, 0o600)
        return path.read_bytes().strip()
    except (OSError, ValueError) as exc:
        raise PromptVaultError("fanout prompt vault is unavailable") from exc


def _fernet(*, create: bool) -> Fernet:
    path = key_path()
    try:
        cache_key = str(path.expanduser().resolve())
    except (OSError, ValueError) as exc:
        raise PromptVaultError("fanout prompt vault is unavailable") from exc
    cached = _FERNET_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        material = _create_key(path) if create else path.read_bytes().strip()
        cipher = Fernet(material)
    except (OSError, ValueError, TypeError) as exc:
        raise PromptVaultError("fanout prompt vault is unavailable") from exc
    _FERNET_CACHE[cache_key] = cipher
    return cipher


def encrypt_prompt(prompt: str) -> str:
    """Encrypt a prompt with an authenticated, per-user Fernet key."""
    if not isinstance(prompt, str):
        raise PromptVaultError("fanout prompt vault rejected the prompt")
    try:
        return _fernet(create=True).encrypt(prompt.encode("utf-8")).decode("ascii")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise PromptVaultError("fanout prompt vault rejected the prompt") from exc


def decrypt_prompt(ciphertext: str) -> str:
    """Authenticate and decrypt a vault token; fail closed on any mismatch."""
    if not isinstance(ciphertext, str):
        raise PromptVaultError("fanout prompt vault could not decrypt the prompt")
    try:
        return _fernet(create=False).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, TypeError, PromptVaultError) as exc:
        raise PromptVaultError("fanout prompt vault could not decrypt the prompt") from exc


def reset_cache_for_tests() -> None:
    """Clear process-local key state after a test changes its key path."""
    _FERNET_CACHE.clear()
