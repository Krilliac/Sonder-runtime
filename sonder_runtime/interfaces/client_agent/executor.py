"""Workspace-contained client tool executor with client-local PermissionMode."""
from __future__ import annotations

import asyncio
import errno
import fnmatch
import itertools
import math
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

MAX_OUTPUT_CHARS = 65_536
MAX_ERROR_CHARS = 4_096
_MAX_FILE_READ_CHARS = 65_536
_MAX_FILE_READ_SCAN_BYTES = 8 * 1024 * 1024
_MAX_FILE_WRITE_CHARS = 2 * 1024 * 1024
_MAX_EDIT_FILE_BYTES = 4 * 1024 * 1024
_MAX_STDIN_BYTES = 1 * 1024 * 1024
_MAX_CAPTURE_BYTES = 256 * 1024
_MAX_TEST_PATTERN_CHARS = 512
_MAX_SEARCH_MATCHES = 100
_MAX_FIND_MATCHES = 200
_MAX_TREE_ENTRIES = 200
_MAX_WALK_DEPTH = 64
_MAX_WALK_DIRS = 5_000
_KILL_WAIT_TIMEOUT_SECONDS = 5.0

_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_TOKEN_HEADER_SECRET_RE = re.compile(
    r"(?i)(X-Sonder-Client-Agent-Token\s*[:=]\s*)[^\s,;\"']+"
)
_EXACT_SCRUBBED_ENV_KEYS: frozenset[str] = frozenset({
    "SONDER_CLIENT_AGENT_TOKEN",
    "SONDER_API_KEY",
    "SONDER_AUTH_TOKEN",
    "SONDER_MCP_TOKEN",
    "SONDER_INFERENCE_API_KEY",
    "SONDER_REMOTE_TOKEN",
})
_SONDER_SECRET_ENV_RE = re.compile(r"^SONDER_[A-Z0-9_]*(?:TOKEN|API_KEY|SECRET)$")

CLIENT_AGENT_MODES: tuple[str, ...] = (
    "plan",
    "manual",
    "acceptEdits",
    "auto",
    "bypass",
)

CLIENT_AGENT_MODE_RANK: dict[str, int] = {
    "plan": 0,
    "manual": 1,
    "acceptEdits": 2,
    "auto": 3,
    "bypass": 4,
}


class ModeCeilingRefused(PermissionError):
    """A requested mode exceeds the companion policy ceiling or disabled bypass."""

_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "file_find",
    "directory_tree",
    "text_search",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
})

_WRITE_TOOLS: frozenset[str] = frozenset({
    "file_write",
    "file_edit",
    "file_delete",
    "directory_create",
})

_EXECUTE_TOOLS: frozenset[str] = frozenset({
    "run_code",
    "run_program",
    "test_run",
})

CLIENT_DELEGATABLE_TOOLS: frozenset[str] = _READ_ONLY_TOOLS | _WRITE_TOOLS | _EXECUTE_TOOLS


def _bound_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[truncated]"


def _sanitize_error_text(text: str, *, limit: int = MAX_ERROR_CHARS) -> str:
    """Strip control chars, redact auth tokens, and bound executor error messages."""
    cleaned = "".join(
        ch if (ord(ch) >= 32 and ord(ch) != 127) or ch in ("\n", "\t") else " "
        for ch in str(text or "")
    )
    cleaned = _BEARER_SECRET_RE.sub("Bearer [redacted]", cleaned)
    cleaned = _TOKEN_HEADER_SECRET_RE.sub(r"\1[redacted]", cleaned)
    env_token = os.environ.get("SONDER_CLIENT_AGENT_TOKEN", "").strip()
    if len(env_token) >= 6:
        cleaned = cleaned.replace(env_token, "[redacted]")
    return _bound_text(cleaned, limit)


def _parse_bounded_int(
    raw: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    """Parse an integer argument strictly (rejecting bools, NaN/Inf, and non-numeric types)."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"'{name}' must be an integer, not boolean")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            raise ValueError(f"'{name}' must be a finite integer")
        value = int(raw)
    elif isinstance(raw, str):
        clean = raw.strip()
        if not clean:
            return default
        try:
            value = int(clean, 10)
        except ValueError as exc:
            raise ValueError(f"'{name}' must be an integer") from exc
    else:
        raise TypeError(f"'{name}' must be an integer")
    return max(minimum, min(maximum, value))


def _scrubbed_process_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the process environment stripped of Sonder companion/host tokens."""
    source = os.environ if base_env is None else base_env
    cleaned: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _EXACT_SCRUBBED_ENV_KEYS or _SONDER_SECRET_ENV_RE.match(upper):
            continue
        cleaned[key] = value
    return cleaned


def tool_risk_of(tool: str) -> str:
    if tool in _READ_ONLY_TOOLS:
        return "read_only"
    if tool in _WRITE_TOOLS:
        return "write"
    return "execute"


def mode_allows_without_prompt(mode: str, risk: str) -> bool:
    """Return whether client-local ``mode`` allows ``risk`` without explicit approval."""
    if mode == "bypass":
        return True
    if mode == "auto":
        return risk in {"read_only", "write", "execute"}
    if mode == "acceptEdits":
        return risk in {"read_only", "write"}
    return risk == "read_only"


@dataclass(frozen=True)
class ClientToolExecutionResult:
    ok: bool
    output: str = ""
    error: str = ""
    denied: bool = False
    needs_approval: bool = False
    mode: str = "manual"
    risk: str = "execute"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": _bound_text(self.output, MAX_OUTPUT_CHARS),
            "error": _bound_text(self.error, MAX_ERROR_CHARS),
            "denied": self.denied,
            "needs_approval": self.needs_approval,
            "mode": self.mode,
            "risk": self.risk,
        }


_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?:$|[^/\\])")
_LOCAL_EXTENDED_DRIVE_RE = re.compile(r"^\\\\\?\\[A-Za-z]:(?:$|[/\\])")
_MAX_SYMLINK_HOPS = 16
_WINDOWS_RESERVED_DEVICE_NAMES: frozenset[str] = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


def _is_reserved_device_segment(segment: str, *, index: int) -> bool:
    """True when ``segment`` names a Windows reserved DOS device (CON, NUL, COM1, etc.)."""
    if not segment or segment in (".", ".."):
        return False
    if index == 0 and len(segment) == 2 and segment[1] == ":" and segment[0].isalpha():
        return False
    cleaned = segment.split(":", 1)[0].rstrip(" .")
    if not cleaned:
        return False
    base = cleaned.split(".", 1)[0].rstrip(" ").casefold()
    if base in _WINDOWS_RESERVED_DEVICE_NAMES:
        return True
    try:
        pure = PureWindowsPath(segment)
        is_reserved = getattr(pure, "is_reserved", None)
        if callable(is_reserved) and is_reserved():
            return True
    except ValueError:
        pass
    return False


def _reject_unsafe_path_spelling(raw: str, *, allow_local_extended: bool = False) -> None:
    """Lexically reject UNC, device, extended, NT-namespace, control-char, reserved-name, and drive-relative paths.

    Runs BEFORE any ``Path.resolve()`` or filesystem call so hostile spellings
    cannot trigger outbound SMB/NTLM lookups or escape into NT device namespaces
    (in every mode, including ``bypass``).
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise PermissionError(f"path {raw!r} contains disallowed control characters")
    text = raw.strip()
    if not text:
        return
    if allow_local_extended and _LOCAL_EXTENDED_DRIVE_RE.match(text):
        text = text[4:]
    if (
        text.startswith("\\\\")
        or text.startswith("//")
        or text.startswith("\\/")
        or text.startswith("/\\")
    ):
        raise PermissionError(f"UNC, device, or extended path is not allowed: {raw!r}")
    if text in ("\\??", "/??") or (
        len(text) > 3 and text[:3] in ("\\??", "/??") and text[3] in ("\\", "/")
    ):
        raise PermissionError(f"NT namespace path is not allowed: {raw!r}")
    if _DRIVE_RELATIVE_RE.match(text):
        raise PermissionError(f"Windows drive-relative path is not allowed: {raw!r}")
    for idx, seg in enumerate(re.split(r"[/\\]", text)):
        if _is_reserved_device_segment(seg, index=idx):
            raise PermissionError(f"Windows reserved device name is not allowed in path: {raw!r}")


def _is_junction(path: Path | str) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction):
        try:
            return bool(is_junction(path))
        except OSError:
            return True
    return False


def _is_reparse_or_link(path: Path | str) -> bool:
    """True when ``path`` is a symlink, Windows NTFS junction, or other reparse point."""
    if os.path.islink(path):
        return True
    if _is_junction(path):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_flag:
        try:
            st = os.lstat(path)
            if int(getattr(st, "st_file_attributes", 0)) & reparse_flag:
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        Path(os.path.normcase(str(path))).relative_to(Path(os.path.normcase(str(root))))
        return True
    except ValueError:
        return False


def _reject_link_components(
    candidate: Path,
    root: Path,
    *,
    allow_escape: bool,
    raw_text: str,
) -> None:
    """Reject any symlink, NTFS junction, or reparse-point component along ``candidate``.

    Walks components below ``root`` when ``candidate`` is lexically under ``root``
    (so OS-level symlinks above ``root`` such as ``/var -> /private/var`` do not
    cause false positives), and also checks the leaf itself when ``allow_escape``
    targets a path outside ``root``.
    """
    norm_root = os.path.normcase(str(root)).rstrip("\\/")
    norm_cand = os.path.normcase(str(candidate))
    under_root = (
        norm_cand == norm_root
        or norm_cand.startswith(norm_root + os.sep)
        or (os.altsep is not None and norm_cand.startswith(norm_root + os.altsep))
    )
    if under_root:
        rel_parts = candidate.parts[len(root.parts):]
        cur = root
        for part in rel_parts:
            if part in ("", "."):
                continue
            if part == "..":
                cur = cur.parent
                continue
            cur = cur / part
            if _is_reparse_or_link(cur):
                if not allow_escape:
                    try:
                        resolved_target = cur.resolve()
                        if not _path_is_within_root(resolved_target, root):
                            raise PermissionError(
                                f"path '{raw_text}' via symlink/junction escapes workspace root '{root}'"
                            )
                    except PermissionError:
                        raise
                    except OSError:
                        pass
                raise PermissionError(
                    f"symlink or junction traversal is not permitted in path '{raw_text}' (at '{cur}')"
                )
        return
    if _is_reparse_or_link(candidate):
        if not allow_escape:
            raise PermissionError(
                f"path '{raw_text}' via symlink/junction escapes workspace root '{root}'"
            )
        raise PermissionError(
            f"symlink or junction traversal is not permitted in path '{raw_text}'"
        )


def _windows_final_path_for_fd(fd: int) -> Path | None:
    """Resolve the kernel final path of an already-opened Windows file descriptor."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.windll.kernel32
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final_path.restype = ctypes.c_uint32

    buf_len = 512
    for _ in range(4):
        buf = ctypes.create_unicode_buffer(buf_len)
        needed = int(get_final_path(ctypes.c_void_p(handle), buf, buf_len, 0))
        if needed == 0:
            raise PermissionError("unable to verify final path of opened file handle")
        if needed < buf_len:
            raw_final = buf.value
            _reject_unsafe_path_spelling(raw_final, allow_local_extended=True)
            clean = _strip_local_extended_prefix(raw_final)
            return Path(clean)
        buf_len = needed + 1
    raise PermissionError("opened file handle path exceeds verification buffer")


def _verify_opened_fd(
    fd: int,
    expected_path: Path,
    root: Path,
    *,
    allow_escape: bool,
    for_write: bool,
) -> None:
    """Post-open handle checks: regular file, inside root, non-secret, no TOCTOU swap."""
    st_fd = os.fstat(fd)
    if not stat.S_ISREG(st_fd.st_mode):
        raise PermissionError(f"refusing to open non-regular file '{expected_path}'")
    if sys.platform == "win32":
        final_path = _windows_final_path_for_fd(fd)
        if final_path is None:
            raise PermissionError("unable to verify final path of opened file handle")
        resolved_final = final_path.resolve()
        _reject_unsafe_path_spelling(str(resolved_final))
        if not allow_escape and not _path_is_within_root(resolved_final, root):
            raise PermissionError(
                f"opened file handle '{resolved_final}' escapes workspace root '{root}'"
            )
        refusal = (
            _write_refusal(str(expected_path), resolved_final, root)
            if for_write
            else _read_refusal(str(expected_path), resolved_final, root)
        )
        if refusal is not None:
            raise refusal
    if _is_reparse_or_link(expected_path):
        raise PermissionError(f"symlink or junction detected at '{expected_path}' after open")
    try:
        st_path = os.lstat(expected_path)
    except OSError as exc:
        raise PermissionError(f"path '{expected_path}' disappeared during open") from exc
    if not os.path.samestat(st_fd, st_path):
        raise PermissionError(f"path '{expected_path}' changed during open (TOCTOU detected)")


def _open_regular_file_no_follow(
    path: Path,
    root: Path,
    *,
    flags: int,
    allow_escape: bool,
    for_write: bool,
    mode: int = 0o600,
) -> int:
    """Open ``path`` without following symlinks/junctions and verify the opened descriptor."""
    _reject_link_components(path, root, allow_escape=allow_escape, raw_text=str(path))
    extra_flags = getattr(os, "O_CLOEXEC", 0)
    if sys.platform == "win32":
        extra_flags |= getattr(os, "O_BINARY", 0)
    else:
        extra_flags |= getattr(os, "O_NOFOLLOW", 0)

    fd: int | None = None
    try:
        if (
            sys.platform != "win32"
            and not allow_escape
            and _path_is_within_root(path, root)
            and os.open in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
        ):
            rel = path.relative_to(root)
            parts = rel.parts
            if not parts:
                raise PermissionError(f"refusing to open workspace root directory '{path}'")
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            step_flags = dir_flags | getattr(os, "O_NOFOLLOW", 0)
            dir_fd = os.open(root, dir_flags)
            try:
                for part in parts[:-1]:
                    next_fd = os.open(part, step_flags, dir_fd=dir_fd)
                    os.close(dir_fd)
                    dir_fd = next_fd
                fd = os.open(parts[-1], flags | extra_flags, mode, dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        else:
            fd = os.open(path, flags | extra_flags, mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(
                f"symlink or junction traversal is not permitted in path '{path}'"
            ) from exc
        raise

    try:
        _verify_opened_fd(
            fd,
            path,
            root,
            allow_escape=allow_escape,
            for_write=for_write,
        )
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _mkdir_no_links(target_dir: Path, root: Path, *, allow_escape: bool) -> None:
    """Create ``target_dir`` step-by-step while rejecting any symlink/junction component."""
    _reject_link_components(target_dir, root, allow_escape=allow_escape, raw_text=str(target_dir))
    if _path_is_within_root(target_dir, root):
        rel = Path(os.path.normcase(str(target_dir))).relative_to(Path(os.path.normcase(str(root))))
        # Preserve original casing from target_dir parts below root.
        rel_parts = target_dir.parts[len(root.parts):] if len(target_dir.parts) >= len(root.parts) else rel.parts
        cur = root
        for part in rel_parts:
            if part in ("", "."):
                continue
            cur = cur / part
            if os.path.lexists(cur):
                if _is_reparse_or_link(cur):
                    raise PermissionError(
                        f"symlink or junction traversal is not permitted in directory '{cur}'"
                    )
                if not cur.is_dir():
                    raise OSError(f"path component is not a directory: {cur}")
            else:
                try:
                    os.mkdir(cur)
                except FileExistsError:
                    if _is_reparse_or_link(cur):
                        raise PermissionError(
                            f"symlink or junction traversal is not permitted in directory '{cur}'"
                        ) from None
                    if not cur.is_dir():
                        raise
        return
    if not allow_escape:
        raise PermissionError(f"path '{target_dir}' escapes workspace root '{root}'")
    # Bypass mode outside workspace root: walk missing tail components and refuse links.
    missing: list[Path] = []
    probe = target_dir
    while not os.path.lexists(probe):
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    if os.path.lexists(probe) and _is_reparse_or_link(probe):
        raise PermissionError(f"symlink or junction traversal is not permitted in directory '{probe}'")
    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            if _is_reparse_or_link(directory):
                raise PermissionError(
                    f"symlink or junction traversal is not permitted in directory '{directory}'"
                ) from None
            if not directory.is_dir():
                raise


def _strip_local_extended_prefix(target: str) -> str:
    if _LOCAL_EXTENDED_DRIVE_RE.match(target):
        return target[4:]
    return target


def _reject_unsafe_reparse_ancestors(candidate: Path, *, max_hops: int = _MAX_SYMLINK_HOPS) -> None:
    """Walk ``candidate`` component-by-component, following up to ``max_hops`` local symlinks/junctions.

    Refuses any symlink/junction targeting UNC/device/NT/reserved-device paths
    BEFORE ``Path.resolve()`` is ever called, and refuses chains deeper than
    ``max_hops`` (including loops).
    """
    current = candidate
    hops = 0
    while True:
        parts = current.parts
        if not parts:
            return
        cur = Path(parts[0])
        followed = False
        for idx, part in enumerate(parts[1:], start=1):
            if part in ("", "."):
                continue
            if part == "..":
                cur = cur.parent
                continue
            cur = cur / part
            if _is_reparse_or_link(cur):
                hops += 1
                if hops > max_hops:
                    raise PermissionError(
                        f"too many symlink/junction levels (>{max_hops}) while resolving '{candidate}'"
                    )
                try:
                    target = os.readlink(cur)
                except OSError as exc:
                    raise PermissionError(f"unreadable link/junction at '{cur}'") from exc
                _reject_unsafe_path_spelling(target, allow_local_extended=True)
                clean_target = _strip_local_extended_prefix(target.strip())
                target_path = Path(clean_target)
                if not target_path.is_absolute():
                    target_path = cur.parent / target_path
                remaining = parts[idx + 1 :]
                current = target_path.joinpath(*remaining) if remaining else target_path
                followed = True
                break
        if not followed:
            return


def _entry_within_root(entry: Path, root: Path) -> bool:
    """True when ``entry`` is neither a reparse point/link nor resolved outside ``root``."""
    if _is_reparse_or_link(entry):
        return False
    try:
        resolved = entry.resolve()
        _reject_unsafe_path_spelling(str(resolved))
        resolved.relative_to(root)
    except (OSError, ValueError, PermissionError):
        return False
    return True


class ClientAgentPolicy:
    """Mutable client-local mode & workspace policy owned by the local PC user."""

    def __init__(
        self,
        *,
        workspace_root: Path | str | None = None,
        mode: str = "manual",
        max_mode: str | None = None,
        can_bypass: bool | None = None,
    ) -> None:
        if workspace_root is not None:
            raw_root = str(workspace_root)
            _reject_unsafe_path_spelling(raw_root)
            resolved_root = Path(raw_root)
            _reject_unsafe_reparse_ancestors(resolved_root)
        else:
            resolved_root = Path.cwd()
        self.workspace_root: Path = resolved_root.resolve()
        _reject_unsafe_path_spelling(str(self.workspace_root))
        if mode not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown client permission mode: {mode!r}")
        if max_mode is not None:
            clean_max = max_mode.strip()
            if clean_max not in CLIENT_AGENT_MODES:
                raise ValueError(f"unknown client max_mode: {max_mode!r}")
        else:
            clean_max = None
        if can_bypass is None:
            if clean_max is not None:
                effective_can_bypass = clean_max == "bypass"
            else:
                effective_can_bypass = mode == "bypass"
        else:
            effective_can_bypass = bool(can_bypass)
        if mode == "bypass" and not effective_can_bypass:
            raise ModeCeilingRefused("bypass mode is not enabled on this client companion")
        if clean_max is None:
            clean_max = "bypass" if effective_can_bypass else "auto"
        elif clean_max == "bypass" and not effective_can_bypass:
            raise ModeCeilingRefused("max_mode 'bypass' requires can_bypass=True")
        if CLIENT_AGENT_MODE_RANK[mode] > CLIENT_AGENT_MODE_RANK[clean_max]:
            raise ValueError(f"initial mode {mode!r} exceeds max_mode {clean_max!r}")
        self.mode: str = mode
        self.max_mode: str = clean_max
        self.can_bypass: bool = effective_can_bypass and (clean_max == "bypass")

    def set_mode(self, mode: str, *, confirm_bypass: bool = False) -> str:
        clean = mode.strip()
        if clean not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown mode '{clean}'; valid modes: {', '.join(CLIENT_AGENT_MODES)}")
        if clean == "bypass" and not self.can_bypass:
            raise ModeCeilingRefused(
                "bypass mode is not enabled on this client companion (restart with --confirm-bypass)"
            )
        if CLIENT_AGENT_MODE_RANK[clean] > CLIENT_AGENT_MODE_RANK[self.max_mode]:
            raise ModeCeilingRefused(
                f"mode '{clean}' exceeds companion max_mode '{self.max_mode}'"
            )
        if clean == "bypass" and not confirm_bypass:
            raise PermissionError("bypass mode requires explicit confirmation (confirm_bypass: true)")
        self.mode = clean
        return self.mode


def _resolve_path(raw: object, root: Path, *, allow_escape: bool) -> Path:
    raw_str = str(raw or "")
    _reject_unsafe_path_spelling(raw_str)
    text = raw_str.strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    _reject_unsafe_reparse_ancestors(candidate)
    _reject_link_components(candidate, root, allow_escape=allow_escape, raw_text=text)
    resolved = candidate.resolve()
    _reject_unsafe_path_spelling(str(resolved))
    if not allow_escape and not _path_is_within_root(resolved, root):
        raise PermissionError(f"path '{text}' escapes workspace root '{root}'")
    return resolved


# Git runs programs named by repository metadata as a side effect of the
# read-only git tools: ``core.fsmonitor`` on every status/diff, hooks, and the
# filter/diff/merge drivers that ``.git/config`` defines and
# ``.gitattributes`` / ``.git/info/attributes`` select. A write there would
# turn a write-class call (no prompt in acceptEdits) into execution with no
# execute prompt, so the write tools refuse these paths in every mode. A user
# who really needs to change them can do it through run_program, which is
# execute-class.
_GIT_METADATA_DIRECTORY = ".git"
_GIT_CONTROL_FILES: frozenset[str] = frozenset({".gitattributes", ".gitmodules", ".gitconfig"})


class ClientPathRefused(PermissionError):
    """A client tool targeted a path no client mode may read or change."""


class WritePathRefused(ClientPathRefused):
    """A write-class tool targeted a path no client mode may change."""


class ReadPathRefused(ClientPathRefused):
    """A read-class tool targeted a credential store or secret config."""


class GitControlPathRefused(WritePathRefused):
    """A write-class tool targeted git metadata or a git-executable config file."""


class CredentialPathRefused(WritePathRefused):
    """A write-class tool targeted a credential store."""


# Credential stores, restated from the secret-name rules of the server's
# mutation guard (``sonder_runtime.adapters.filesystem.file_ops``:
# SECRET_FILES, CREDENTIAL_READ_* and ``_is_secret_path``); the architecture
# rules keep the interfaces layer from importing adapters. A write there is a
# swapped token or a program a later command runs (``~/.ssh/config``
# ProxyCommand, an ``authorized_keys`` entry, an ``.npmrc`` script-shell), so
# the write tools refuse it in every mode, bypass included.
_CREDENTIAL_DIRECTORIES: frozenset[str] = frozenset({
    ".ssh", ".aws", ".azure", ".gcloud", ".gnupg", ".kube",
})
_CREDENTIAL_FILES: frozenset[str] = frozenset({
    ".credentials.json", ".netrc", "_netrc", ".token", "auth.json",
    "credentials.json", "secrets.json", "token.json", ".git-credentials", ".pgpass",
    "id_dsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_rsa",
    ".npmrc", ".pypirc", "kubeconfig", ".terraformrc", "terraform.rc",
    "credentials.tfrc.json", ".boto", ".s3cfg", ".htpasswd", ".vault-token",
    "service-account.json", "service_account.json", "serviceaccount.json",
    "application_default_credentials.json",
})
_CREDENTIAL_SUFFIXES: frozenset[str] = frozenset({".key", ".p12", ".pem", ".pfx", ".kdbx"})
# A config file is a credential store when its name says so (``db_secret.yaml``).
_NAMED_CREDENTIAL_CONFIG_SUFFIXES: frozenset[str] = frozenset({
    ".cfg", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml",
})
_CREDENTIAL_PAIRS: frozenset[tuple[str, str]] = frozenset({
    (".docker", "config.json"), (".cargo", "credentials.toml"), (".cargo", "credentials"),
    (".gem", "credentials"), (".m2", "settings.xml"), (".m2", "settings-security.xml"),
    ("github cli", "hosts.yml"),
})
_CREDENTIAL_DIRECTORY_PAIRS: frozenset[tuple[str, str]] = frozenset({
    (".config", "gcloud"), ("roaming", "gcloud"),
})
_CREDENTIAL_TRIPLES: frozenset[tuple[str, str, str]] = frozenset({
    (".config", "gh", "hosts.yml"), (".config", "glab-cli", "config.yml"),
})


def _is_credential_name(name: str) -> bool:
    """``name`` (already canonical) is a credential file by name alone."""
    dot = name.rfind(".")
    suffix = name[dot:] if dot > 0 else ""
    if name in _CREDENTIAL_FILES or suffix in _CREDENTIAL_SUFFIXES:
        return True
    if name in {".env", ".envrc"} or name.startswith(".env.") or suffix == ".env":
        return True
    return suffix in _NAMED_CREDENTIAL_CONFIG_SUFFIXES and ("credential" in name or "secret" in name)


def _is_credential_path(names: list[str], context: list[str]) -> bool:
    """``names`` (canonical components below the root, leaf last) name a credential store.

    ``context`` holds every canonical component of the resolved workspace
    root (empty for a path outside the root, whose ``names`` are then the
    whole path). Every rule that names a directory runs on ``context`` plus
    ``names``, so a root that is, or sits anywhere inside, a credential store
    (``~/.aws``, ``~/.aws/sso``, ``~/.config/gcloud/configurations``) makes
    every write under it a credential write. The leaf-name rule still judges
    only the target's own name.
    """
    if not names:
        return False
    window = [*context, *names]
    if any(name in _CREDENTIAL_DIRECTORIES for name in window):
        return True
    if any(pair in _CREDENTIAL_DIRECTORY_PAIRS for pair in itertools.pairwise(window)):
        return True
    if _is_credential_name(names[-1]):
        return True
    if len(window) >= 2 and (window[-2], window[-1]) in _CREDENTIAL_PAIRS:
        return True
    return len(window) >= 3 and (window[-3], window[-2], window[-1]) in _CREDENTIAL_TRIPLES


def _canonical_component(part: str) -> str:
    """The name Windows opens for ``part``, case-folded.

    NTFS ignores trailing dots and spaces and treats ``name::$INDEX_ALLOCATION``
    (or any ``name:stream``) as ``name``. These spellings are folded on every
    platform: on POSIX they are distinct names, and refusing them there only
    errs toward refusal.
    """
    return part.split(":", 1)[0].rstrip(" .").casefold()


def _components_under(path: Path, root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the root's components, ``path``'s components below the root).

    A path outside the root yields no root context and all its components.
    """
    try:
        return root.parts, path.relative_to(root).parts
    except ValueError:
        return (), path.parts


def _is_git_config_path(names: list[str], context: list[str]) -> bool:
    """True when ``names`` (with ``context``) targets a git config file that may hold tokens or commands."""
    if not names:
        return False
    window = [*context, *names]
    if names[-1] == ".gitconfig":
        return True
    if len(window) >= 2 and (window[-2], window[-1]) == (".git", "config"):
        return True
    return len(window) >= 3 and (window[-3], window[-2], window[-1]) == (".config", "git", "config")


def _is_secret_read_path(names: list[str], context: list[str]) -> bool:
    """True when ``names`` (with ``context``) names a credential store or git config secret."""
    return _is_credential_path(names, context) or _is_git_config_path(names, context)


def _is_credential_dir_components(
    parts: tuple[str, ...],
    context_parts: tuple[str, ...] = (),
) -> bool:
    """True when any component or directory pair in ``context_parts + parts`` is a credential directory."""
    window = [_canonical_component(part) for part in (*context_parts, *parts)]
    if any(name in _CREDENTIAL_DIRECTORIES for name in window):
        return True
    return any(pair in _CREDENTIAL_DIRECTORY_PAIRS for pair in itertools.pairwise(window))


def _write_refusal(text: str, resolved: Path, root: Path) -> WritePathRefused | None:
    """The refusal for a write to ``text`` (resolving to ``resolved``), or ``None``.

    The lexical spelling and the resolved path are both checked, so neither a
    ``..`` detour nor a symlink or 8.3 short name (which ``resolve`` expands
    for existing directories) reaches ``.git`` or a credential store.
    The git rules do not inspect components above the workspace root, so a
    workspace that itself sits under a directory named ``.git`` is not refused
    wholesale; a path outside the root (bypass) is inspected in full. The
    credential rules also see every component of the root (see
    ``_is_credential_path``).
    """
    lexical = Path(text)
    lexical_split = (
        _components_under(lexical, root) if lexical.is_absolute() else (root.parts, lexical.parts)
    )
    for context_parts, parts in (lexical_split, _components_under(resolved, root)):
        names = [_canonical_component(part) for part in parts]
        context = [_canonical_component(part) for part in context_parts]
        window = [*context, *names]
        if _GIT_METADATA_DIRECTORY in names:
            return GitControlPathRefused(
                f"refusing to modify git repository metadata '{text}': hooks and config there "
                "name programs git runs; use run_program (execute approval) if this is intended"
            )
        if (
            (names and names[-1] in _GIT_CONTROL_FILES)
            or (len(window) >= 3 and (window[-3], window[-2], window[-1]) == (".config", "git", "config"))
        ):
            return GitControlPathRefused(
                f"refusing to modify git control file '{text}': it selects programs git runs; "
                "use run_program (execute approval) if this is intended"
            )
        if _is_credential_path(names, context):
            return CredentialPathRefused(
                f"refusing to modify credential store '{text}' from a client tool; "
                "use run_program (execute approval) if this is intended"
            )
    return None


def _read_refusal(text: str, resolved: Path, root: Path) -> ReadPathRefused | None:
    """The refusal for reading ``text`` (resolving to ``resolved``), or ``None``."""
    lexical = Path(text)
    lexical_split = (
        _components_under(lexical, root) if lexical.is_absolute() else (root.parts, lexical.parts)
    )
    for context_parts, parts in (lexical_split, _components_under(resolved, root)):
        names = [_canonical_component(part) for part in parts]
        context = [_canonical_component(part) for part in context_parts]
        if _is_secret_read_path(names, context):
            return ReadPathRefused(
                f"refusing to read credential or secret path '{text}' from a client tool"
            )
    return None


def _resolve_read_path(
    raw: object,
    root: Path,
    *,
    allow_escape: bool,
    workspace_root: Path | None = None,
) -> Path:
    """``_resolve_path`` for read-class tools, refusing credential stores and git configs."""
    text = str(raw or "").strip() or "."
    resolved = _resolve_path(raw, root, allow_escape=allow_escape)
    refusal = _read_refusal(text, resolved, workspace_root or root)
    if refusal is not None:
        raise refusal
    return resolved


def _resolve_write_path(raw: object, root: Path, *, allow_escape: bool) -> Path:
    """``_resolve_path`` for write-class tools, refusing git-executable config and credential stores."""
    text = str(raw or "").strip() or "."
    resolved = _resolve_path(text, root, allow_escape=allow_escape)
    refusal = _write_refusal(text, resolved, root)
    if refusal is not None:
        raise refusal
    return resolved


class _WindowsProcessJob:
    """Win32 Job Object with ``KILL_ON_JOB_CLOSE`` for atomic process-tree cleanup."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self.attached: bool = False

    def attach(self, pid: int) -> bool:
        if sys.platform != "win32" or pid <= 0:
            return False
        import ctypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BasicLimitInfo(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _ExtendedLimitInfo(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInfo),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return False
        # PROCESS_SET_QUOTA (0x0100) | PROCESS_TERMINATE (0x0001)
        proc_handle = kernel32.OpenProcess(0x0101, 0, pid)
        if not proc_handle:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return False
        try:
            ok = bool(kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(proc_handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(proc_handle))
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return False
        self._handle = int(job)
        self.attached = True
        return True

    def terminate_and_close(self) -> None:
        handle = self._handle
        if handle is None or sys.platform != "win32":
            return
        self._handle = None
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        try:
            kernel32.TerminateJobObject(ctypes.c_void_p(handle), 1)
        except OSError:
            pass
        try:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        except OSError:
            pass


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    win_job: _WindowsProcessJob | None,
) -> None:
    """Force-terminate ``proc`` and all descendants on both Windows and POSIX."""
    if sys.platform == "win32":
        if win_job is not None and win_job.attached:
            win_job.terminate_and_close()
        elif proc.pid > 0:
            # Fallback when Job Object assignment was unavailable.
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=_KILL_WAIT_TIMEOUT_SECONDS)
            except Exception:
                pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    else:
        if proc.pid > 0:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        else:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_WAIT_TIMEOUT_SECONDS)
    except Exception:
        pass


async def _read_stream_bounded(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """Read an async subprocess pipe keeping at most ``max_bytes`` in memory."""
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    kept = 0
    truncated = False
    while True:
        piece = await stream.read(16_384)
        if not piece:
            break
        if kept < max_bytes:
            room = max_bytes - kept
            if len(piece) <= room:
                chunks.append(piece)
                kept += len(piece)
            else:
                chunks.append(piece[:room])
                kept += room
                truncated = True
        else:
            truncated = True
    return b"".join(chunks), truncated


async def _write_stdin_safely(proc: asyncio.subprocess.Process, stdin_bytes: bytes | None) -> None:
    if stdin_bytes is None or proc.stdin is None:
        return
    try:
        proc.stdin.write(stdin_bytes)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str = "",
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text else None
    if stdin_bytes is not None and len(stdin_bytes) > _MAX_STDIN_BYTES:
        raise ValueError(f"stdin exceeds {_MAX_STDIN_BYTES} bytes")
    effective_env = _scrubbed_process_env(env)
    try:
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP (0x00000200) isolates console signals.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=effective_env,
                creationflags=0x00000200,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=effective_env,
                start_new_session=True,
            )
    except FileNotFoundError as exc:
        exe = argv[0] if argv else ""
        raise FileNotFoundError(f"executable not found: {exe}") from exc

    win_job = _WindowsProcessJob() if sys.platform == "win32" else None
    if win_job is not None:
        win_job.attach(proc.pid)

    stdin_task = asyncio.create_task(_write_stdin_safely(proc, stdin_bytes))
    stdout_task = asyncio.create_task(_read_stream_bounded(proc.stdout, _MAX_CAPTURE_BYTES))
    stderr_task = asyncio.create_task(_read_stream_bounded(proc.stderr, _MAX_CAPTURE_BYTES))
    wait_task = asyncio.create_task(proc.wait())
    try:
        _, (stdout_b, out_trunc), (stderr_b, err_trunc), _ = await asyncio.wait_for(
            asyncio.gather(stdin_task, stdout_task, stderr_task, wait_task),
            timeout=float(timeout),
        )
    except asyncio.TimeoutError as exc:
        await _terminate_process_tree(proc, win_job)
        for task in (stdin_task, stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdin_task, stdout_task, stderr_task, wait_task, return_exceptions=True)
        raise TimeoutError(f"command timed out after {timeout}s") from exc
    except BaseException:
        await _terminate_process_tree(proc, win_job)
        for task in (stdin_task, stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdin_task, stdout_task, stderr_task, wait_task, return_exceptions=True)
        raise
    finally:
        # Clean up any orphaned background descendants left behind by the child.
        if sys.platform == "win32":
            if win_job is not None:
                win_job.terminate_and_close()
        elif proc.pid > 0:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    stdout_s = stdout_b.decode("utf-8", errors="replace")
    if out_trunc:
        stdout_s = stdout_s + "\n...[truncated]"
    stderr_s = stderr_b.decode("utf-8", errors="replace")
    if err_trunc:
        stderr_s = stderr_s + "\n...[truncated]"
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout_s, stderr_s


def _run_process_sync(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str = "",
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    return asyncio.run(_run_process(argv, cwd=cwd, timeout=timeout, stdin_text=stdin_text, env=env))


def _format_proc_output(rc: int, stdout_s: str, stderr_s: str) -> tuple[bool, str, str]:
    combined = stdout_s
    if stderr_s:
        combined = f"{combined}\n{stderr_s}".strip() if combined else stderr_s
    if rc == 0:
        return True, _bound_text(combined or "(no output)", MAX_OUTPUT_CHARS), ""
    err = _bound_text(combined or f"exit code {rc}", MAX_ERROR_CHARS)
    return False, _bound_text(stdout_s, MAX_OUTPUT_CHARS), err


def _exec_file_read(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_read_path(args.get("path"), root, allow_escape=bypass)
    try:
        st_pre = os.lstat(path)
    except FileNotFoundError:
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if _is_reparse_or_link(path):
        raise PermissionError(f"symlink or junction traversal is not permitted in path '{path}'")
    if not stat.S_ISREG(st_pre.st_mode):
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    offset = _parse_bounded_int(
        args.get("offset"),
        default=1,
        minimum=1,
        maximum=10_000_000,
        name="offset",
    )
    limit = _parse_bounded_int(
        args.get("limit"),
        default=200,
        minimum=1,
        maximum=500,
        name="limit",
    )
    try:
        fd = _open_regular_file_no_follow(
            path,
            root,
            flags=os.O_RDONLY,
            allow_escape=bypass,
            for_write=False,
        )
    except FileNotFoundError:
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    # Stream to the requested window with bounded per-line and total scan caps
    # so a huge single-line or multi-GB file cannot exhaust memory.
    slice_lines: list[str] = []
    scanned_chars = 0
    collected_chars = 0
    line_cap = _MAX_FILE_READ_CHARS + 1
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
        lineno = 0
        end_line = offset - 1 + limit
        while lineno < end_line:
            chunk = fh.readline(line_cap)
            if not chunk:
                break
            scanned_chars += len(chunk)
            if lineno < offset - 1 and scanned_chars > _MAX_FILE_READ_SCAN_BYTES:
                return ClientToolExecutionResult(
                    ok=False,
                    error=f"file_read exceeded {_MAX_FILE_READ_SCAN_BYTES} byte scan budget before offset {offset}",
                )
            # Drain remainder of an overlong line without buffering it.
            truncated_line = False
            while not chunk.endswith("\n"):
                tail = fh.readline(line_cap)
                if not tail:
                    break
                truncated_line = True
                scanned_chars += len(tail)
                if scanned_chars > _MAX_FILE_READ_SCAN_BYTES:
                    break
            lineno += 1
            if lineno >= offset:
                line_text = chunk.rstrip("\r\n")
                if truncated_line:
                    line_text = _bound_text(line_text, _MAX_FILE_READ_CHARS)
                slice_lines.append(line_text)
                collected_chars += len(line_text) + 16
                if collected_chars > _MAX_FILE_READ_CHARS + 1024:
                    break
    rendered = "\n".join(f"{offset + idx}: {line}" for idx, line in enumerate(slice_lines))
    return ClientToolExecutionResult(ok=True, output=_bound_text(rendered, _MAX_FILE_READ_CHARS))


def _exec_file_write(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    raw_content = args.get("content")
    content = "" if raw_content is None else str(raw_content)
    if len(content) > _MAX_FILE_WRITE_CHARS:
        return ClientToolExecutionResult(
            ok=False,
            error=f"file_write content exceeds {_MAX_FILE_WRITE_CHARS} characters",
        )
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"create", "overwrite", "append"}:
        return ClientToolExecutionResult(ok=False, error=f"unsupported file_write mode: {mode}")
    if mode == "create" and os.path.lexists(path):
        return ClientToolExecutionResult(ok=False, error=f"file already exists: {path}")
    _mkdir_no_links(path.parent, root, allow_escape=bypass)
    if mode == "create":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    elif mode == "append":
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    else:
        # Never pass O_TRUNC before post-open verification on Windows so a raced
        # symlink/junction target cannot be truncated before handle checks run.
        flags = os.O_WRONLY | os.O_CREAT
    try:
        fd = _open_regular_file_no_follow(
            path,
            root,
            flags=flags,
            allow_escape=bypass,
            for_write=True,
        )
    except FileExistsError:
        return ClientToolExecutionResult(ok=False, error=f"file already exists: {path}")
    if mode == "overwrite":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return ClientToolExecutionResult(ok=True, output=f"wrote {len(content)} chars to {path}")


def _exec_file_edit(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    try:
        st_pre = os.lstat(path)
    except FileNotFoundError:
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if _is_reparse_or_link(path):
        raise PermissionError(f"symlink or junction traversal is not permitted in path '{path}'")
    if not stat.S_ISREG(st_pre.st_mode):
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if st_pre.st_size > _MAX_EDIT_FILE_BYTES:
        return ClientToolExecutionResult(
            ok=False,
            error=f"file exceeds maximum editable size of {_MAX_EDIT_FILE_BYTES} bytes",
        )
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    if not old:
        return ClientToolExecutionResult(ok=False, error="'old' text must be non-empty")
    if len(new) > _MAX_FILE_WRITE_CHARS:
        return ClientToolExecutionResult(
            ok=False,
            error=f"file_edit replacement exceeds {_MAX_FILE_WRITE_CHARS} characters",
        )
    try:
        fd = _open_regular_file_no_follow(
            path,
            root,
            flags=os.O_RDWR,
            allow_escape=bypass,
            for_write=True,
        )
    except FileNotFoundError:
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    # Perform read and in-place write through the same verified descriptor.
    with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as fh:
        if os.fstat(fh.fileno()).st_size > _MAX_EDIT_FILE_BYTES:
            return ClientToolExecutionResult(
                ok=False,
                error=f"file exceeds maximum editable size of {_MAX_EDIT_FILE_BYTES} bytes",
            )
        text = fh.read(_MAX_EDIT_FILE_BYTES + 1)
        if len(text) > _MAX_EDIT_FILE_BYTES:
            return ClientToolExecutionResult(
                ok=False,
                error=f"file exceeds maximum editable size of {_MAX_EDIT_FILE_BYTES} bytes",
            )
        count = text.count(old)
        if count == 0:
            return ClientToolExecutionResult(ok=False, error="target string not found in file")
        if count > 1:
            return ClientToolExecutionResult(ok=False, error=f"target string matched {count} times; must be unique")
        updated = text.replace(old, new, 1)
        fh.seek(0)
        fh.truncate(0)
        fh.write(updated)
    return ClientToolExecutionResult(ok=True, output=f"edited {path}")


def _exec_file_delete(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    raw = args.get("path")
    raw_str = str(raw or "")
    _reject_unsafe_path_spelling(raw_str)
    text = raw_str.strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    if os.path.normcase(str(candidate)) == os.path.normcase(str(root)):
        return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
    if candidate.name not in ("", ".", ".."):
        parent = candidate.parent
        _reject_unsafe_reparse_ancestors(parent)
        _reject_link_components(parent, root, allow_escape=bypass, raw_text=text)
        resolved_parent = parent.resolve()
        _reject_unsafe_path_spelling(str(resolved_parent))
        if not bypass and not _path_is_within_root(resolved_parent, root):
            raise PermissionError(f"path '{text}' escapes workspace root '{root}'")
        link_path = resolved_parent / candidate.name
        if _is_reparse_or_link(link_path):
            refusal = _write_refusal(text, link_path, root)
            if refusal is not None:
                raise refusal
            if _is_junction(link_path):
                return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
            # Unlink the symlink itself without resolving or deleting its target.
            link_path.unlink()
            return ClientToolExecutionResult(ok=True, output=f"deleted {link_path}")
    path = _resolve_write_path(raw, root, allow_escape=bypass)
    if os.path.normcase(str(path)) == os.path.normcase(str(root)):
        return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
    if not os.path.lexists(path):
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if _is_reparse_or_link(path):
        if _is_junction(path):
            return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
        path.unlink()
        return ClientToolExecutionResult(ok=True, output=f"deleted {path}")
    if path.is_dir():
        return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
    path.unlink()
    return ClientToolExecutionResult(ok=True, output=f"deleted {path}")


def _exec_directory_create(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    _mkdir_no_links(path, root, allow_escape=bypass)
    return ClientToolExecutionResult(ok=True, output=f"created directory {path}")


def _resolve_scan_base(raw: object, root: Path, *, bypass: bool) -> Path:
    text = str(raw if raw is not None else ".").strip() or "."
    base = _resolve_path(text, root, allow_escape=bypass)
    if _is_credential_dir_components(base.parts):
        raise ReadPathRefused(f"refusing to scan credential directory '{text}' from a client tool")
    refusal = _read_refusal(text, base, root)
    if refusal is not None:
        raise refusal
    return base


def _scandir_classify(
    entry: os.DirEntry[str],
    root: Path,
    *,
    bypass: bool,
    skip_dirs: frozenset[str],
    skip_all_names: frozenset[str] = frozenset(),
) -> tuple[bool, bool, Path]:
    """Classify one ``os.scandir`` entry without following symlinks/junctions unsafely.

    Returns ``(include, is_dir, entry_path)``. Never calls ``entry.is_dir()`` with
    ``follow_symlinks=True`` so hostile symlinks/junctions cannot trigger outbound
    SMB/NTLM lookups.
    """
    entry_path = Path(entry.path)
    if entry.name in skip_all_names:
        return False, False, entry_path
    try:
        is_link = entry.is_symlink() or _is_reparse_or_link(entry.path)
    except OSError:
        return False, False, entry_path
    if is_link:
        if not bypass:
            return False, False, entry_path
        try:
            _reject_unsafe_reparse_ancestors(entry_path)
            resolved = entry_path.resolve()
            _reject_unsafe_path_spelling(str(resolved))
        except (PermissionError, OSError):
            return False, False, entry_path
        if _is_credential_dir_components(resolved.parts) or _read_refusal(entry.name, resolved, root) is not None:
            return False, False, entry_path
        # Do not descend into directory links even in bypass mode.
        return True, False, entry_path
    try:
        is_dir = entry.is_dir(follow_symlinks=False)
    except OSError:
        return False, False, entry_path
    if is_dir and entry.name in skip_dirs:
        return False, False, entry_path
    context_parts, parts = _components_under(entry_path, root)
    if is_dir:
        if _is_credential_dir_components(parts, context_parts):
            return False, False, entry_path
    else:
        names = [_canonical_component(p) for p in parts]
        context = [_canonical_component(p) for p in context_parts]
        if _is_secret_read_path(names, context):
            return False, False, entry_path
    if not bypass and not _path_is_within_root(entry_path, root):
        return False, False, entry_path
    return True, is_dir, entry_path


_SCAN_SKIP_DIRS: frozenset[str] = frozenset({".git", "__pycache__", ".dart_tool", "node_modules"})
_TREE_SKIP_NAMES: frozenset[str] = frozenset({".git", "__pycache__"})


def _exec_file_find(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_scan_base(args.get("root", "."), root, bypass=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    pattern = str(args.get("pattern") or "*").strip() or "*"
    matches: list[str] = []
    visited_dirs = 0

    def walk(cur: Path, depth: int) -> None:
        nonlocal visited_dirs
        if len(matches) >= _MAX_FIND_MATCHES or depth > _MAX_WALK_DEPTH or visited_dirs >= _MAX_WALK_DIRS:
            return
        visited_dirs += 1
        dirs: list[tuple[str, Path]] = []
        files: list[tuple[str, Path]] = []
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    include, is_dir, entry_path = _scandir_classify(
                        entry, root, bypass=bypass, skip_dirs=_SCAN_SKIP_DIRS,
                    )
                    if not include:
                        continue
                    if is_dir:
                        dirs.append((entry.name, entry_path))
                    else:
                        files.append((entry.name, entry_path))
        except OSError:
            return
        for name, fpath in sorted(files, key=lambda pair: pair[0]):
            rel = fpath.relative_to(base).as_posix()
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
                if len(matches) >= _MAX_FIND_MATCHES:
                    return
        for _, dpath in sorted(dirs, key=lambda pair: pair[0]):
            walk(dpath, depth + 1)
            if len(matches) >= _MAX_FIND_MATCHES or visited_dirs >= _MAX_WALK_DIRS:
                return

    walk(base, 0)
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_directory_tree(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_scan_base(args.get("path", "."), root, bypass=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    depth = _parse_bounded_int(
        args.get("depth"),
        default=2,
        minimum=1,
        maximum=5,
        name="depth",
    )
    lines: list[str] = []

    def walk(cur: Path, level: int) -> None:
        if level > depth or len(lines) >= _MAX_TREE_ENTRIES:
            return
        items: list[tuple[str, Path, bool]] = []
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    include, is_dir, entry_path = _scandir_classify(
                        entry,
                        root,
                        bypass=bypass,
                        skip_dirs=_TREE_SKIP_NAMES,
                        skip_all_names=_TREE_SKIP_NAMES,
                    )
                    if include:
                        items.append((entry.name, entry_path, is_dir))
        except OSError:
            return
        items.sort(key=lambda item: (not item[2], item[0].lower()))
        for name, entry_path, is_dir in items:
            indent = "  " * (level - 1)
            suffix = "/" if is_dir else ""
            lines.append(f"{indent}{name}{suffix}")
            if len(lines) >= _MAX_TREE_ENTRIES:
                return
            if is_dir and level < depth:
                walk(entry_path, level + 1)

    walk(base, 1)
    return ClientToolExecutionResult(ok=True, output="\n".join(lines) or "(empty)")


def _exec_text_search(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    query = str(args.get("query") or "")
    if not query:
        return ClientToolExecutionResult(ok=False, error="query is required")
    base = _resolve_scan_base(args.get("root", "."), root, bypass=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    glob_pat = str(args.get("glob") or "").strip()
    matches: list[str] = []
    visited_dirs = 0

    def walk(cur: Path, depth: int) -> None:
        nonlocal visited_dirs
        if len(matches) >= _MAX_SEARCH_MATCHES or depth > _MAX_WALK_DEPTH or visited_dirs >= _MAX_WALK_DIRS:
            return
        visited_dirs += 1
        dirs: list[tuple[str, Path]] = []
        files: list[tuple[str, Path]] = []
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    include, is_dir, entry_path = _scandir_classify(
                        entry, root, bypass=bypass, skip_dirs=_SCAN_SKIP_DIRS,
                    )
                    if not include:
                        continue
                    if is_dir:
                        dirs.append((entry.name, entry_path))
                    else:
                        files.append((entry.name, entry_path))
        except OSError:
            return
        for name, fpath in sorted(files, key=lambda pair: pair[0]):
            if glob_pat and not fnmatch.fnmatch(name, glob_pat):
                continue
            try:
                fd = _open_regular_file_no_follow(
                    fpath,
                    root,
                    flags=os.O_RDONLY,
                    allow_escape=bypass,
                    for_write=False,
                )
            except (OSError, PermissionError):
                continue
            try:
                with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
                    if os.fstat(fh.fileno()).st_size > 512 * 1024:
                        continue
                    content = fh.read(512 * 1024 + 1)
            except OSError:
                continue
            if len(content) > 512 * 1024:
                continue
            rel = fpath.relative_to(base).as_posix()
            for lineno, line in enumerate(content.splitlines(), 1):
                if query in line:
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return
        for _, dpath in sorted(dirs, key=lambda pair: pair[0]):
            walk(dpath, depth + 1)
            if len(matches) >= _MAX_SEARCH_MATCHES or visited_dirs >= _MAX_WALK_DIRS:
                return

    walk(base, 0)
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_run_code(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    code = str(args.get("code") or "")
    if not code.strip():
        return ClientToolExecutionResult(ok=False, error="code is required")
    if len(code) > _MAX_FILE_WRITE_CHARS:
        return ClientToolExecutionResult(
            ok=False,
            error=f"code exceeds {_MAX_FILE_WRITE_CHARS} characters",
        )
    language = str(args.get("language") or "python").strip().lower()
    stdin_text = str(args.get("stdin") or "")
    timeout = _parse_bounded_int(
        args.get("timeout"),
        default=15,
        minimum=1,
        maximum=60,
        name="timeout",
    )
    cwd = root
    if language in {"python", "py"}:
        rc, out, err = _run_process_sync(
            [sys.executable, "-c", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    if language in {"js", "javascript", "node"}:
        rc, out, err = _run_process_sync(
            ["node", "-e", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    if language in {"powershell", "ps1"}:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tmp:
            tmp.write(code)
            tmp_path = Path(tmp.name)
        try:
            rc, out, err = _run_process_sync(
                ["powershell", "-NoProfile", "-File", str(tmp_path)],
                cwd=cwd,
                timeout=timeout,
                stdin_text=stdin_text,
            )
            ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
            return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
        finally:
            tmp_path.unlink(missing_ok=True)
    if language in {"bash", "sh"}:
        rc, out, err = _run_process_sync(
            ["bash", "-c", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    return ClientToolExecutionResult(ok=False, error=f"unsupported language on client: {language}")


def _extract_raw_executable_token(command: str) -> str:
    """Extract the leading executable token without POSIX backslash unescaping.

    On POSIX, ``shlex.split(..., posix=True)`` interprets backslashes as escape
    characters (turning ``\\\\host\\share\\evil.exe`` into ``\\hostshareevil.exe``
    and ``\\??\\C:\\cmd.exe`` into ``??C:cmd.exe``) before executable-path
    validation runs. Stripping only quote delimiters up to the first unquoted
    whitespace preserves backslash prefixes so ``_reject_unsafe_path_spelling``
    rejects UNC, extended, device, and NT-namespace spellings consistently on
    Linux, macOS, and Windows.
    """
    quote: str | None = None
    chars: list[str] = []
    for ch in command.strip():
        if quote is None:
            if ch.isspace():
                break
            if ch in ('"', "'"):
                quote = ch
            else:
                chars.append(ch)
        elif ch == quote:
            quote = None
        else:
            chars.append(ch)
    return "".join(chars)


def _exec_run_program(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ClientToolExecutionResult(ok=False, error="command is required")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in command):
        return ClientToolExecutionResult(ok=False, error="command contains disallowed control characters")
    cwd = _resolve_path(args.get("cwd") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"working directory not found: {cwd}")
    timeout = _parse_bounded_int(
        args.get("timeout"),
        default=30,
        minimum=1,
        maximum=120,
        name="timeout",
    )
    raw_exe = _extract_raw_executable_token(command)
    if raw_exe:
        _reject_unsafe_path_spelling(raw_exe)
    try:
        raw_tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return ClientToolExecutionResult(ok=False, error=f"invalid command syntax: {exc}")
    argv = [
        tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
        for tok in raw_tokens
    ]
    if not argv or not argv[0].strip():
        return ClientToolExecutionResult(ok=False, error="command is required")
    _reject_unsafe_path_spelling(argv[0])
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=timeout)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def _exec_test_run(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_path(args.get("root") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    timeout = _parse_bounded_int(
        args.get("timeout"),
        default=120,
        minimum=1,
        maximum=120,
        name="timeout",
    )
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    rel_path = str(args.get("path") or "").strip()
    if rel_path:
        target = _resolve_path(rel_path, cwd, allow_escape=bypass)
        argv.append(str(target))
    pattern = str(args.get("pattern") or "").strip()
    if pattern:
        if len(pattern) > _MAX_TEST_PATTERN_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in pattern):
            return ClientToolExecutionResult(ok=False, error="invalid test_run pattern")
        argv.extend(["-k", pattern])
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=timeout)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


_GIT_TIMEOUT_SECONDS = 30

# Command-line ``-c`` values beat every config file (and reach child gits in
# submodules through GIT_CONFIG_PARAMETERS), so these hold even for a
# repository whose config the tools did not write. This mirrors
# ``sonder_runtime.adapters.git_program_guard.static_overrides``; the
# architecture rules keep the interfaces layer from importing adapters, so
# the list is restated here.
#   core.hooksPath / core.fsmonitor / core.sshCommand: programs git would run.
#   submodule/diff/status settings: never descend into a submodule, whose own
#     config the driver probe below does not see.
#   safe.bareRepository=explicit: a directory the write tools filled with
#     HEAD/objects/refs/config (no ``.git`` component, so the write guard
#     allows it) is not discovered as a repository.
#   protocol.allow=never (and GIT_NO_LAZY_FETCH): a promisor remote's
#     upload-pack command never runs for a lazy fetch.
#   log.showSignature=false: ``git log`` never runs gpg.program.
_GIT_STATIC_OVERRIDES: tuple[str, ...] = (
    "-c", "core.hooksPath=" + os.devnull,
    "-c", "core.fsmonitor=false",
    "-c", "core.sshCommand=",
    "-c", "submodule.recurse=false",
    "-c", "diff.ignoreSubmodules=all",
    "-c", "diff.submodule=short",
    "-c", "status.submoduleSummary=false",
    "-c", "fetch.recurseSubmodules=false",
    "-c", "safe.bareRepository=explicit",
    "-c", "protocol.allow=never",
    "-c", "log.showSignature=false",
)
# ``git config`` arguments listing every configured filter/merge/diff driver
# key (git's own regex syntax; Python never compiles it). ``git config``
# launches no program.
_GIT_DRIVER_PROBE: tuple[str, ...] = (
    "config", "--null", "--name-only", "--get-regexp",
    r"^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver|diff\..*\.textconv)$",
)
_GIT_DRIVER_LEAVES: Mapping[str, frozenset[str]] = {
    "filter": frozenset({"clean", "smudge", "process", "required"}),
    "merge": frozenset({"driver"}),
    "diff": frozenset({"textconv"}),
}
_BUILTIN_MERGE_DRIVER = "git merge-file --marker-size=%L %A %O %B"
# Driver names a ``-c`` override can name exactly; ASCII only, matched without
# IGNORECASE so no Unicode case-fold (U+212A, U+017F) slips in. Restated from
# git_program_guard._SAFE_DRIVER_NAME_RE (interfaces may not import adapters).
_SAFE_GIT_DRIVER_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Environment variables that name a program git would run.
_GIT_PROGRAM_ENV_VARS: tuple[str, ...] = (
    "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS",
)


def _git_environment(root: Path | None = None, *, bypass: bool = False) -> dict[str, str]:
    env = {
        key: value
        for key, value in _scrubbed_process_env().items()
        if key.upper() not in _GIT_PROGRAM_ENV_VARS
    }
    env.update({
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if not bypass and root is not None:
        ceiling = root.parent
        if ceiling != root:
            env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return env


def _git_executable(root: Path, cwd: Path) -> str:
    """Absolute path of the git on PATH; never one the write tools could have planted."""
    # On Windows a ``git.cmd``/``git.bat`` earlier in PATHEXT order would run
    # through cmd.exe; ask for the executable itself.
    found = shutil.which("git.exe" if os.name == "nt" else "git")
    if not found:
        raise FileNotFoundError("git is not installed or not on PATH")
    resolved = Path(found).resolve()
    for base in (root, cwd):
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        raise PermissionError(f"refusing to run a git executable found inside the workspace: {resolved}")
    return str(resolved)


def _git_driver_overrides(probe_stdout: str) -> list[str]:
    """Neutralising overrides for every driver key ``_GIT_DRIVER_PROBE`` listed.

    A key of an unexpected shape, or a driver name ``-c`` cannot express,
    raises ``ValueError`` (fail closed).
    """
    overrides: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw_key in probe_stdout.split("\x00"):
        key = raw_key.strip()
        if not key:
            continue
        section, _, rest = key.partition(".")
        name, _, leaf = rest.rpartition(".")
        section = section.lower()
        if not name or leaf.lower() not in _GIT_DRIVER_LEAVES.get(section, frozenset()):
            raise ValueError("unexpected git driver configuration key; refusing to run git")
        if _SAFE_GIT_DRIVER_NAME_RE.fullmatch(name) is None:
            # git splits ``-c`` at the first '=': ``-c filter.a=b.process=``
            # would set ``filter.a`` and leave the driver named ``a=b`` live.
            # Any other character outside the allowlist (a quote, space,
            # control or non-ASCII character, U+FFFD from bad UTF-8) may not
            # round-trip either. Same charset as git_program_guard.
            raise ValueError(
                f"git driver name {name!r} is outside [A-Za-z0-9_.-]; refusing to run git"
            )
        if (section, name) in seen:
            continue
        seen.add((section, name))
        prefix = f"{section}.{name}."
        if section == "filter":
            # Empty ``process``/``clean``/``smudge`` make git run no filter
            # program at all (content passes through unchanged); with
            # ``required=false`` the missing filter is not an error.
            overrides.extend([
                "-c", prefix + "process=",
                "-c", prefix + "clean=",
                "-c", prefix + "smudge=",
                "-c", prefix + "required=false",
            ])
        elif section == "merge":
            overrides.extend(["-c", prefix + "driver=" + _BUILTIN_MERGE_DRIVER])
        else:
            overrides.extend(["-c", prefix + "textconv=cat"])
    return overrides


def _hardened_git_prefix(git: str, cwd: Path, env: Mapping[str, str]) -> list[str]:
    """``git`` plus every override that keeps repository config from launching a program.

    The static overrides cover hooks, fsmonitor and the rest; a ``git config``
    probe (which launches nothing) then lists the configured filter, merge and
    diff drivers, and each is neutralised: filters run no program, merge
    drivers become git's built-in ``merge-file`` and textconv becomes ``cat``
    (the diff and log tools also pass ``--no-textconv``). A driver whose name
    ``-c`` cannot express exactly (anything outside ``[A-Za-z0-9_.-]``)
    refuses. A probe that fails (including ``safe.bareRepository``
    refusing an embedded bare repository) or times out raises, and no further
    git command runs: fail closed.
    """
    base = [git, "--no-pager", "--no-optional-locks", *_GIT_STATIC_OVERRIDES]
    try:
        rc, out, err = _run_process_sync(
            [*base, *_GIT_DRIVER_PROBE], cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS, env=env,
        )
    except TimeoutError as exc:
        raise TimeoutError("git driver configuration probe timed out; refusing to run git") from exc
    if rc not in (0, 1):  # 1 is git's "no matching key"
        detail = _bound_text((err or out).strip() or f"exit code {rc}", MAX_ERROR_CHARS)
        raise ValueError(f"git driver configuration could not be read; refusing to run git: {detail}")
    return [*base, *_git_driver_overrides(out)]


_DIFF_GIT_HEADER_RE = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')


def _diff_path_is_secret(rel_path: str, cwd: Path, root: Path) -> bool:
    clean = rel_path.strip()
    if not clean:
        return False
    candidate = cwd / clean
    context_parts, parts = _components_under(candidate, root)
    names = [_canonical_component(p) for p in parts]
    context = [_canonical_component(p) for p in context_parts]
    return _is_secret_read_path(names, context)


def _filter_git_diff_secrets(diff_text: str, cwd: Path, root: Path) -> str:
    if not diff_text:
        return diff_text
    kept: list[str] = []
    skipping = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            match = _DIFF_GIT_HEADER_RE.match(line.rstrip("\r\n"))
            if match and (
                _diff_path_is_secret(match.group(1), cwd, root)
                or _diff_path_is_secret(match.group(2), cwd, root)
            ):
                skipping = True
                continue
            skipping = False
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _exec_git(tool: str, args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_scan_base(args.get("cwd") or ".", root, bypass=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    subcommand: list[str]
    rel_path = ""
    if tool == "git_status":
        subcommand = ["status", "--short"]
    elif tool == "git_diff":
        subcommand = ["diff", "--no-ext-diff", "--no-textconv"]
        if args.get("staged") is True:
            subcommand.append("--staged")
        rel_path = str(args.get("path") or "").strip()
        if rel_path:
            target = _resolve_read_path(rel_path, cwd, allow_escape=bypass, workspace_root=root)
            subcommand.extend(["--", str(target)])
    elif tool == "git_log":
        limit = _parse_bounded_int(
            args.get("limit"),
            default=20,
            minimum=1,
            maximum=100,
            name="limit",
        )
        subcommand = ["log", "--no-ext-diff", "--no-textconv", f"-n{limit}", "--oneline"]
    else:
        subcommand = ["branch", "-a"]
    env = _git_environment(root, bypass=bypass)
    git = _git_executable(root, cwd)
    argv = [*_hardened_git_prefix(git, cwd, env), *subcommand]
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS, env=env)
    if tool == "git_diff" and rc == 0 and not rel_path:
        out = _filter_git_diff_secrets(out, cwd, root)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def execute_client_tool(
    tool: str,
    args: Mapping[str, Any] | None,
    *,
    policy: ClientAgentPolicy,
    approved: bool = False,
) -> ClientToolExecutionResult:
    """Execute one client-delegatable tool locally under ``policy``."""
    clean_tool = str(tool or "").strip().lstrip("/")
    if clean_tool not in CLIENT_DELEGATABLE_TOOLS:
        return ClientToolExecutionResult(
            ok=False,
            error=f"tool '{clean_tool}' is not permitted on the client companion",
            denied=True,
            mode=policy.mode,
            risk="execute",
        )
    risk = tool_risk_of(clean_tool)
    if policy.mode == "plan" and risk != "read_only":
        return ClientToolExecutionResult(
            ok=False,
            error=f"tool '{clean_tool}' ({risk}) is denied in read-only plan mode",
            denied=True,
            mode=policy.mode,
            risk=risk,
        )
    if not approved and not mode_allows_without_prompt(policy.mode, risk):
        return ClientToolExecutionResult(
            ok=False,
            error=f"approval required on client PC for '{clean_tool}' ({risk}) in {policy.mode} mode",
            needs_approval=True,
            mode=policy.mode,
            risk=risk,
        )
    bypass = policy.mode == "bypass"
    safe_args = dict(args or {})
    try:
        if clean_tool == "file_read":
            res = _exec_file_read(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_write":
            res = _exec_file_write(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_edit":
            res = _exec_file_edit(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_delete":
            res = _exec_file_delete(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "directory_create":
            res = _exec_directory_create(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_find":
            res = _exec_file_find(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "directory_tree":
            res = _exec_directory_tree(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "text_search":
            res = _exec_text_search(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "run_code":
            res = _exec_run_code(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "run_program":
            res = _exec_run_program(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "test_run":
            res = _exec_test_run(safe_args, policy.workspace_root, bypass=bypass)
        else:
            res = _exec_git(clean_tool, safe_args, policy.workspace_root, bypass=bypass)
    except ClientPathRefused as exc:
        return ClientToolExecutionResult(
            ok=False,
            error=_sanitize_error_text(str(exc)),
            denied=True,
            mode=policy.mode,
            risk=risk,
        )
    except (PermissionError, OSError, TimeoutError, ValueError, TypeError, OverflowError, UnicodeError) as exc:
        return ClientToolExecutionResult(
            ok=False,
            error=_sanitize_error_text(str(exc)),
            mode=policy.mode,
            risk=risk,
        )
    except Exception:  # noqa: BLE001 - fail closed without leaking internal tracebacks
        return ClientToolExecutionResult(
            ok=False,
            error="internal tool execution error",
            mode=policy.mode,
            risk=risk,
        )
    return ClientToolExecutionResult(
        ok=res.ok,
        output=res.output,
        error=_sanitize_error_text(res.error) if res.error else "",
        denied=res.denied,
        needs_approval=res.needs_approval,
        mode=policy.mode,
        risk=risk,
    )
