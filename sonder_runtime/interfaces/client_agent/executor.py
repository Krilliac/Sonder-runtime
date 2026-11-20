"""Workspace-contained client tool executor with client-local PermissionMode."""
from __future__ import annotations

import asyncio
import fnmatch
import itertools
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 65_536
MAX_ERROR_CHARS = 4_096
_MAX_FILE_READ_CHARS = 65_536
_MAX_SEARCH_MATCHES = 100
_MAX_FIND_MATCHES = 200
_MAX_TREE_ENTRIES = 200

CLIENT_AGENT_MODES: tuple[str, ...] = (
    "plan",
    "manual",
    "acceptEdits",
    "auto",
    "bypass",
)

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


class ClientAgentPolicy:
    """Mutable client-local mode & workspace policy owned by the local PC user."""

    def __init__(
        self,
        *,
        workspace_root: Path | str | None = None,
        mode: str = "manual",
    ) -> None:
        resolved_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        self.workspace_root: Path = resolved_root.resolve()
        if mode not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown client permission mode: {mode!r}")
        self.mode: str = mode

    def set_mode(self, mode: str, *, confirm_bypass: bool = False) -> str:
        clean = mode.strip()
        if clean not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown mode '{clean}'; valid modes: {', '.join(CLIENT_AGENT_MODES)}")
        if clean == "bypass" and not confirm_bypass:
            raise PermissionError("bypass mode requires explicit confirmation (confirm_bypass: true)")
        self.mode = clean
        return self.mode


def _resolve_path(raw: object, root: Path, *, allow_escape: bool) -> Path:
    text = str(raw or "").strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not allow_escape:
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"path '{text}' escapes workspace root '{root}'") from exc
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
_GIT_CONTROL_FILES: frozenset[str] = frozenset({".gitattributes", ".gitmodules"})


class WritePathRefused(PermissionError):
    """A write-class tool targeted a path no client mode may change."""


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
        if _GIT_METADATA_DIRECTORY in names:
            return GitControlPathRefused(
                f"refusing to modify git repository metadata '{text}': hooks and config there "
                "name programs git runs; use run_program (execute approval) if this is intended"
            )
        if names and names[-1] in _GIT_CONTROL_FILES:
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


def _resolve_write_path(raw: object, root: Path, *, allow_escape: bool) -> Path:
    """``_resolve_path`` for write-class tools, refusing git-executable config and credential stores."""
    text = str(raw or "").strip() or "."
    resolved = _resolve_path(text, root, allow_escape=allow_escape)
    refusal = _write_refusal(text, resolved, root)
    if refusal is not None:
        raise refusal
    return resolved


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str = "",
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin_text else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text else None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=float(timeout))
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise TimeoutError(f"command timed out after {timeout}s") from exc
    stdout_s = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr_s = (stderr_b or b"").decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout_s, stderr_s


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
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    if not path.is_file():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, min(500, int(args.get("limit") or 200)))
    # Stream to the requested window instead of decoding the whole file.
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        slice_lines = [
            line.removesuffix("\n")
            for line in itertools.islice(fh, offset - 1, offset - 1 + limit)
        ]
    rendered = "\n".join(f"{offset + idx}: {line}" for idx, line in enumerate(slice_lines))
    return ClientToolExecutionResult(ok=True, output=_bound_text(rendered, _MAX_FILE_READ_CHARS))


def _exec_file_write(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    content = str(args.get("content") or "")
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"create", "overwrite", "append"}:
        return ClientToolExecutionResult(ok=False, error=f"unsupported file_write mode: {mode}")
    if mode == "create" and path.exists():
        return ClientToolExecutionResult(ok=False, error=f"file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and path.exists():
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return ClientToolExecutionResult(ok=True, output=f"wrote {len(content)} chars to {path}")


def _exec_file_edit(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    if not path.is_file():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    if not old:
        return ClientToolExecutionResult(ok=False, error="'old' text must be non-empty")
    text = path.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return ClientToolExecutionResult(ok=False, error="target string not found in file")
    if count > 1:
        return ClientToolExecutionResult(ok=False, error=f"target string matched {count} times; must be unique")
    updated = text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    return ClientToolExecutionResult(ok=True, output=f"edited {path}")


def _exec_file_delete(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    if not path.exists():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if path.is_dir():
        return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
    path.unlink()
    return ClientToolExecutionResult(ok=True, output=f"deleted {path}")


def _exec_directory_create(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_write_path(args.get("path"), root, allow_escape=bypass)
    path.mkdir(parents=True, exist_ok=True)
    return ClientToolExecutionResult(ok=True, output=f"created directory {path}")


def _exec_file_find(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_path(args.get("root", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    pattern = str(args.get("pattern") or "*").strip() or "*"
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".dart_tool", "node_modules"})
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(base).as_posix()
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
                if len(matches) >= _MAX_FIND_MATCHES:
                    break
        if len(matches) >= _MAX_FIND_MATCHES:
            break
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_directory_tree(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_path(args.get("path", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    depth = max(1, min(5, int(args.get("depth") or 2)))
    lines: list[str] = []

    def walk(cur: Path, level: int) -> None:
        if level > depth or len(lines) >= _MAX_TREE_ENTRIES:
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name in {".git", "__pycache__"}:
                continue
            indent = "  " * (level - 1)
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{indent}{entry.name}{suffix}")
            if len(lines) >= _MAX_TREE_ENTRIES:
                return
            if entry.is_dir() and level < depth:
                walk(entry, level + 1)

    walk(base, 1)
    return ClientToolExecutionResult(ok=True, output="\n".join(lines) or "(empty)")


def _exec_text_search(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    query = str(args.get("query") or "")
    if not query:
        return ClientToolExecutionResult(ok=False, error="query is required")
    base = _resolve_path(args.get("root", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    glob_pat = str(args.get("glob") or "").strip()
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".dart_tool", "node_modules"})
        for name in sorted(filenames):
            if glob_pat and not fnmatch.fnmatch(name, glob_pat):
                continue
            fpath = Path(dirpath) / name
            try:
                if fpath.stat().st_size > 512 * 1024:
                    continue
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = fpath.relative_to(base).as_posix()
            for lineno, line in enumerate(content.splitlines(), 1):
                if query in line:
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return ClientToolExecutionResult(ok=True, output="\n".join(matches))
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_run_code(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    code = str(args.get("code") or "")
    if not code.strip():
        return ClientToolExecutionResult(ok=False, error="code is required")
    language = str(args.get("language") or "python").strip().lower()
    stdin_text = str(args.get("stdin") or "")
    timeout = max(1, min(60, int(args.get("timeout") or 15)))
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
            ["bash", "-lc", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    return ClientToolExecutionResult(ok=False, error=f"unsupported language on client: {language}")


def _exec_run_program(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ClientToolExecutionResult(ok=False, error="command is required")
    cwd = _resolve_path(args.get("cwd") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"working directory not found: {cwd}")
    timeout = max(1, min(120, int(args.get("timeout") or 30)))
    try:
        raw_tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return ClientToolExecutionResult(ok=False, error=f"invalid command syntax: {exc}")
    argv = [
        tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
        for tok in raw_tokens
    ]
    if not argv:
        return ClientToolExecutionResult(ok=False, error="command is required")
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=timeout)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def _exec_test_run(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_path(args.get("root") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    rel_path = str(args.get("path") or "").strip()
    if rel_path:
        target = _resolve_path(rel_path, cwd, allow_escape=bypass)
        argv.append(str(target))
    pattern = str(args.get("pattern") or "").strip()
    if pattern:
        argv.extend(["-k", pattern])
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=120)
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


def _git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() not in _GIT_PROGRAM_ENV_VARS}
    env.update({
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
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


def _exec_git(tool: str, args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_path(args.get("cwd") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    subcommand: list[str]
    if tool == "git_status":
        subcommand = ["status", "--short"]
    elif tool == "git_diff":
        subcommand = ["diff", "--no-ext-diff", "--no-textconv"]
        if args.get("staged") is True:
            subcommand.append("--staged")
        rel_path = str(args.get("path") or "").strip()
        if rel_path:
            target = _resolve_path(rel_path, cwd, allow_escape=bypass)
            subcommand.extend(["--", str(target)])
    elif tool == "git_log":
        limit = max(1, min(100, int(args.get("limit") or 20)))
        subcommand = ["log", "--no-ext-diff", "--no-textconv", f"-n{limit}", "--oneline"]
    else:
        subcommand = ["branch", "-a"]
    env = _git_environment()
    git = _git_executable(root, cwd)
    argv = [*_hardened_git_prefix(git, cwd, env), *subcommand]
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS, env=env)
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
    except WritePathRefused as exc:
        return ClientToolExecutionResult(
            ok=False,
            error=str(exc),
            denied=True,
            mode=policy.mode,
            risk=risk,
        )
    except (PermissionError, OSError, TimeoutError, ValueError, TypeError) as exc:
        return ClientToolExecutionResult(
            ok=False,
            error=str(exc),
            mode=policy.mode,
            risk=risk,
        )
    return ClientToolExecutionResult(
        ok=res.ok,
        output=res.output,
        error=res.error,
        denied=res.denied,
        needs_approval=res.needs_approval,
        mode=policy.mode,
        risk=risk,
    )
