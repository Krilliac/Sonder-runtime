"""Where the secret guard gets its secrets: the host environment and ``HKCU\\Environment``.

Both are read on the HOST (never inside the sandbox) and only to build needles for
``domain.meshgen.secret_guard``.  Names and values are never logged, printed or put in an
exception message.  A source that exists but cannot be read raises
``SecretGuardUnavailable`` so the run is refused instead of proceeding unguarded.
"""
from __future__ import annotations

import os

from ...domain.meshgen.secret_guard import SecretGuard, SecretGuardUnavailable

ALLOW_ENV = "SONDER_MESHGEN_SECRET_ALLOW"


def allowed_names(env=None) -> list[str]:
    """Variable NAMES whose values are exempt from the guard (``SONDER_MESHGEN_SECRET_ALLOW``, comma separated)."""
    raw = (os.environ if env is None else env).get(ALLOW_ENV, "")
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def read_hkcu_environment() -> list:
    """The user-level environment from the registry as (name, value) pairs (``[]`` off Windows); raises when unreadable."""
    if os.name != "nt":
        return []
    try:
        import winreg

        pairs: list = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                name, value, kind = winreg.EnumValue(key, index)
                if isinstance(value, str) and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    pairs.append((name, value))
                    if kind == winreg.REG_EXPAND_SZ:
                        pairs.append((name, os.path.expandvars(value)))
        return pairs
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SecretGuardUnavailable("cannot read the user environment from the registry (%s)"
                                     % type(exc).__name__) from None


def load_guard(env=None) -> SecretGuard:
    """Build the guard from ``os.environ`` and HKCU\\Environment; raises ``SecretGuardUnavailable``."""
    source = os.environ if env is None else env
    try:
        entries = [(str(k), str(v)) for k, v in dict(source).items()] + read_hkcu_environment()
        return SecretGuard(entries, allowed_names(source))
    except SecretGuardUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure to build the guard must refuse the run
        raise SecretGuardUnavailable("cannot build the secret guard (%s)" % type(exc).__name__) from None


__all__ = ["ALLOW_ENV", "SecretGuardUnavailable", "allowed_names", "load_guard", "read_hkcu_environment"]
