"""The narrow authority a ``build_fix`` approval carries into its own loop (F1).

A fix runs unattended in a worker: nobody is there to answer a per-write
prompt, and an unattended "ask" is a refusal. So the one approval of the
``build_fix`` call (bound to its ``plan_digest``) mints a ``BuildFixGrant``.
The grant covers exactly:

* typed ``read_file``, ``text_patch`` and ``write_file`` calls whose paths lie
  inside the plan's edit scope (the project source root, minus build scripts,
  build-time tool sources, generated files, denied names and the build dir),
  within the loop's file and line budgets;
* child builds the fix service itself starts, from the plan's template
  family and on the plan's (build_dir, target, config, platform, world,
  network) tuple.

It never adds roots, never changes the permission mode, never covers
NETWORK unless the fix itself was approved with ``allow_network``, expires
with the job, and is bound to the principal and the fix job id. Anything the
grant does not cover falls through to normal grading, which refuses it when
nobody can be asked.

This module is the policy: the value types, the pure matching functions and
``BuildFixGrantBook``, a small in-process registry the permission evaluator
(lane C) records approvals in and resolves tokens against. It performs no
I/O; the clock is injected.

Transport: a typed gateway request carries the token in ``approval_token``
as ``build_fix_grant:<token>`` (``grant_carrier``). The evaluator reads it
from the whole request; the arguments stay exactly the tool's schema.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

GRANT_TOKEN_PREFIX = "build_fix_grant:"
GRANT_SOURCE_PREFIX = "build_fix_grant:"
FILE_TOOLS = frozenset({"read_file", "text_patch", "write_file"})
WRITE_TOOLS = frozenset({"text_patch", "write_file"})
# Guard knobs that widen what a file tool may touch; a grant never covers them.
_WIDENING_KNOBS = ("bypass", "extra_roots", "developer_authorized")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_GRANT_FILES = 6
MAX_GRANT_CHANGED_LINES = 400
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_OUTSTANDING_APPROVALS = 64
MAX_LIVE_GRANTS = 64
APPROVAL_TTL_SECONDS = 300.0


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def sha256_hex(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# Paths (lexical; the adapter guards still resolve links and roots)


def _is_windows_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("\\\\") or "\\" in text


def relative_in_root(path: object, root: object) -> str | None:
    """``path`` relative to ``root`` ('/'-separated) when it lies inside it, else None.

    Purely lexical: ``..`` segments are resolved before the comparison, and
    Windows paths compare case-insensitively. Link resolution is the file
    guards' job; this only decides whether the grant speaks for the path.
    """
    if not isinstance(path, str) or not isinstance(root, str) or not path or not root:
        return None
    if "\x00" in path or "\x00" in root:
        return None
    windows = _is_windows_path(root) or _is_windows_path(path)
    module = ntpath if windows else posixpath
    if not module.isabs(root):
        return None
    candidate = path if module.isabs(path) else module.join(root, path)
    base = module.normpath(root)
    target = module.normpath(candidate)
    if windows:
        base_cmp, target_cmp = base.lower().replace("/", "\\"), target.lower().replace("/", "\\")
        sep = "\\"
    else:
        base_cmp, target_cmp = base, target
        sep = "/"
    if target_cmp == base_cmp:
        return None
    prefix = base_cmp if base_cmp.endswith(sep) else base_cmp + sep
    if not target_cmp.startswith(prefix):
        return None
    rel = target[len(prefix):] if len(target) >= len(prefix) else ""
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    return rel


def _rel_stays(root: str, rel: str) -> bool:
    """A diff path names a file inside ``root`` and is already normalized."""
    module = ntpath if _is_windows_path(root) else posixpath
    return relative_in_root(module.join(root, rel), root) == rel


def _same_dir(left: str, right: str) -> bool:
    if not left or not right:
        return False
    windows = _is_windows_path(left) or _is_windows_path(right)
    module = ntpath if windows else posixpath
    a, b = module.normpath(left), module.normpath(right)
    return a.lower() == b.lower() if windows else a == b


# ---------------------------------------------------------------------------
# Values


class FileSetScope:
    """An edit scope of exactly these source-relative files (used by restore)."""

    __slots__ = ("_files",)

    def __init__(self, files: tuple[str, ...]) -> None:
        cleaned = tuple(sorted({str(item) for item in files if isinstance(item, str) and item}))
        self._files = frozenset(cleaned)

    def allows(self, rel: str) -> tuple[bool, str]:
        return (True, "") if rel in self._files else (False, "OUT_OF_SCOPE_FILE")

    def digest(self) -> str:
        return sha256_hex({"files": sorted(self._files)})


@dataclass(frozen=True)
class BuildFixGrantSpec:
    """What one approved fix may touch. ``scope`` is the plan's ``EditScope``.

    ``scope`` is carried for matching and excluded from ``digest()``; the
    digest binds ``scope_digest`` instead, so the approval (made over the
    resolved command) and the minted grant agree on the same scope.
    ``expires_at`` is on the injected clock's scale and excluded from the
    digest too: re-planning the same request yields the same digest.
    """

    project_root: str
    build_dir: str
    target: str
    config: str = ""
    platform: str = ""
    template_ids: tuple[str, ...] = ()
    world: str = "host"
    network: str = "advisory_off"
    scope_digest: str = ""
    max_files: int = MAX_GRANT_FILES
    max_changed_lines: int = MAX_GRANT_CHANGED_LINES
    expires_at: float = 0.0
    extra_targets: tuple[str, ...] = ()
    max_writes: int = 64
    allow_network: bool = False
    scope: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("project_root", "build_dir", "target", "config", "platform", "world",
                     "network", "scope_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
                raise ValueError("%s must be bounded text" % name)
        if not self.project_root or (not self.target and self.template_ids):
            raise ValueError("a fix grant needs a project root and a target")
        if self.scope_digest and not _DIGEST_RE.fullmatch(self.scope_digest):
            raise ValueError("scope_digest must be a sha256 hex digest")
        for name in ("template_ids", "extra_targets"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or len(value) > 16 or any(
                    not isinstance(item, str) or not item or len(item) > 128 for item in value):
                raise ValueError("%s must be a bounded tuple of names" % name)
        for name, limit in (("max_files", MAX_GRANT_FILES), ("max_changed_lines", 4000),
                            ("max_writes", 512)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise ValueError("%s is out of bounds" % name)
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)):
            raise ValueError("expires_at must be a number")
        if not isinstance(self.allow_network, bool):
            raise ValueError("allow_network must be boolean")
        if self.scope is not None and not callable(getattr(self.scope, "allows", None)):
            raise ValueError("scope must offer allows(rel)")

    def digest_fields(self) -> dict:
        return {
            "project_root": self.project_root, "build_dir": self.build_dir,
            "target": self.target, "config": self.config, "platform": self.platform,
            "template_ids": sorted(self.template_ids), "world": self.world,
            "network": self.network, "scope_digest": self.scope_digest,
            "max_files": self.max_files, "max_changed_lines": self.max_changed_lines,
            "extra_targets": sorted(self.extra_targets), "max_writes": self.max_writes,
            "allow_network": self.allow_network,
        }

    def digest(self) -> str:
        return sha256_hex(self.digest_fields())


@dataclass(frozen=True)
class BuildFixGrant:
    token: str
    principal_id: str
    job_id: str
    plan_digest: str
    spec: BuildFixGrantSpec
    issued_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not _TOKEN_RE.fullmatch(self.token):
            raise ValueError("grant token has an invalid shape")
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise ValueError("a grant is bound to a principal")
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("a grant is bound to a job")
        if not isinstance(self.plan_digest, str) or not _DIGEST_RE.fullmatch(self.plan_digest):
            raise ValueError("a grant is bound to a plan digest")
        if not isinstance(self.spec, BuildFixGrantSpec):
            raise ValueError("a grant carries a BuildFixGrantSpec")

    @property
    def receipt_source(self) -> str:
        """What the evaluator records as the receipt's policy match."""
        return GRANT_SOURCE_PREFIX + self.plan_digest

    def expired(self, now: float) -> bool:
        return float(now) >= float(self.spec.expires_at)


@dataclass(frozen=True)
class GrantDecision:
    """Whether a grant speaks for one call; ``reason`` names why not."""

    allowed: bool
    reason: str = ""
    files: tuple[str, ...] = ()
    changed_lines: int = 0
    source: str = ""


def _deny(reason: str) -> GrantDecision:
    return GrantDecision(False, reason)


# ---------------------------------------------------------------------------
# Token transport


def grant_carrier(token: str) -> str:
    """The ``approval_token`` value that carries a grant token on a gateway request."""
    return GRANT_TOKEN_PREFIX + str(token or "")


def grant_token_from(approval_token: object) -> str:
    """The grant token inside an ``approval_token`` value ('' when there is none)."""
    if not isinstance(approval_token, str) or not approval_token.startswith(GRANT_TOKEN_PREFIX):
        return ""
    token = approval_token[len(GRANT_TOKEN_PREFIX):]
    return token if _TOKEN_RE.fullmatch(token) else ""


def restore_plan_digest(job_id: str, files: tuple[str, ...] | list[str]) -> str:
    """The digest a ``build_fix_restore`` approval is bound to."""
    return sha256_hex({"restore": str(job_id), "files": sorted(str(item) for item in files)})


# ---------------------------------------------------------------------------
# Pure matching


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_files_and_lines(patch: object) -> tuple[tuple[str, ...], int] | None:
    """(target paths, changed lines) of a strict unified diff, or None if malformed.

    Hunk bodies are consumed by their header counts, so a removed line that
    itself starts with ``--`` is never mistaken for a new file header.
    """
    if not isinstance(patch, str) or not patch or "\x00" in patch or len(patch) > 4_000_000:
        return None
    lines = patch.splitlines()
    files: list[str] = []
    changed = 0
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- ") or i + 1 >= len(lines) \
                or not lines[i + 1].startswith("+++ "):
            return None
        path = lines[i + 1][4:].strip()
        if not path or path == "/dev/null":
            return None  # deletions are never part of a fix
        files.append(path[2:] if path.startswith("b/") else path)
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith("@@ "):
            match = _HUNK_RE.match(lines[i])
            if not match:
                return None
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
            i += 1
            hunks += 1
            while old_left > 0 or new_left > 0:
                if i >= len(lines):
                    return None
                line = lines[i]
                tag = line[:1]
                if tag == "\\":
                    i += 1
                    continue
                if tag == " ":
                    old_left -= 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                    changed += 1
                elif tag == "+":
                    new_left -= 1
                    changed += 1
                else:
                    return None
                if old_left < 0 or new_left < 0:
                    return None
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
        if not hunks:
            return None
    if not files:
        return None
    return tuple(files), changed


def _scope_allows(spec: BuildFixGrantSpec, rel: str) -> tuple[bool, str]:
    if spec.scope is None:
        return False, "the grant has no edit scope"
    try:
        allowed, why = spec.scope.allows(rel)
    except Exception:  # noqa: BLE001 - a scope that cannot decide refuses
        return False, "the edit scope could not decide"
    return bool(allowed), str(why or "")


def match_file_call(grant: BuildFixGrant, *, principal_id: str, tool_name: str,
                    arguments: Mapping[str, Any], now: float) -> GrantDecision:
    """Does ``grant`` cover this typed file-tool call? (Budgets are the book's.)"""
    if not isinstance(grant, BuildFixGrant):
        return _deny("no grant")
    if grant.expired(now):
        return _deny("the fix grant expired")
    if principal_id != grant.principal_id:
        return _deny("the fix grant belongs to another principal")
    if tool_name not in FILE_TOOLS:
        return _deny("the fix grant does not cover %s" % tool_name)
    if not isinstance(arguments, Mapping):
        return _deny("arguments are not a mapping")
    for knob in _WIDENING_KNOBS:
        if arguments.get(knob):
            return _deny("the fix grant never covers %s" % knob)
    spec = grant.spec
    if tool_name == "text_patch":
        if not _same_dir(str(arguments.get("root") or ""), spec.project_root):
            return _deny("text_patch root is not the fix's project root")
        parsed = diff_files_and_lines(arguments.get("patch"))
        if parsed is None:
            return _deny("text_patch carries no well-formed diff")
        files, changed = parsed
        for rel in files:
            if not _rel_stays(spec.project_root, rel):
                return _deny("patch path leaves the project root")
            allowed, why = _scope_allows(spec, rel)
            if not allowed:
                return _deny("%s: %s" % (why or "OUT_OF_SCOPE_FILE", rel))
        if changed > spec.max_changed_lines * 2:
            return _deny("the patch changes more lines than the fix may")
        return GrantDecision(True, files=tuple(files), changed_lines=changed,
                             source=grant.receipt_source)
    rel = relative_in_root(str(arguments.get("path") or ""), spec.project_root)
    if rel is None:
        return _deny("path is outside the fix's project root")
    allowed, why = _scope_allows(spec, rel)
    if not allowed:
        return _deny("%s: %s" % (why or "OUT_OF_SCOPE_FILE", rel))
    if tool_name == "write_file":
        if arguments.get("mode") != "overwrite":
            return _deny("the fix grant only covers overwriting an existing source")
        content = arguments.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8", "surrogatepass")) > MAX_WRITE_BYTES:
            return _deny("write content is not bounded text")
    return GrantDecision(True, files=(rel,), source=grant.receipt_source)


def match_child_build(grant: BuildFixGrant, *, principal_id: str, template_id: str,
                      build_dir: str, target: str, config: str, platform: str, world: str,
                      network: str, now: float, compile_one: bool = False,
                      file_rel: str = "") -> GrantDecision:
    """Does ``grant`` cover a child build the fix service is about to start?"""
    if not isinstance(grant, BuildFixGrant):
        return _deny("no grant")
    spec = grant.spec
    if grant.expired(now):
        return _deny("the fix grant expired")
    if principal_id != grant.principal_id:
        return _deny("the fix grant belongs to another principal")
    if template_id not in spec.template_ids:
        return _deny("template %s is outside the fix's template family" % template_id)
    if not _same_dir(build_dir, spec.build_dir):
        return _deny("the child build uses another build directory")
    if (world, network) != (spec.world, spec.network):
        return _deny("the child build changes the world or network")
    if network == "allowed" and not spec.allow_network:
        return _deny("the fix was not approved with network access")
    if config != spec.config or platform != spec.platform:
        if not (compile_one and not spec.config and not spec.platform):
            return _deny("the child build changes config or platform")
    if compile_one:
        allowed, why = _scope_allows(spec, file_rel) if file_rel else (False, "no file")
        if not allowed:
            return _deny("compile_one file is outside the edit scope (%s)" % why)
    elif target not in (spec.target,) + tuple(spec.extra_targets):
        return _deny("the child build names another target")
    return GrantDecision(True, source=grant.receipt_source)


# ---------------------------------------------------------------------------
# The in-process book


@dataclass
class _Usage:
    files: set[str] = field(default_factory=set)
    writes: int = 0
    changed_lines: int = 0


class BuildFixGrantBook:
    """Approvals recorded by the evaluator, grants minted for approved plans.

    * ``approve(plan_digest, principal_id)``: the evaluator allowed a
      ``build_fix`` (or ``build_fix_restore``) call bound to this digest.
    * ``issue(spec, principal_id=, job_id=, plan_digest=)``: the service mints
      the grant for its job; it consumes a matching unexpired approval, so a
      grant exists only behind an approval.
    * ``authorize(token, principal_id=, tool_name=, arguments=)``: the
      evaluator asks whether a grant-carrying call is covered; the book
      enforces the loop budgets (distinct files, writes, lines).
    * ``revoke(job_id)``: the job ended; the grant is gone.
    """

    def __init__(self, *, clock: Callable[[], float],
                 approval_ttl_seconds: float = APPROVAL_TTL_SECONDS,
                 token_factory: Callable[[], str] | None = None) -> None:
        self._clock = clock
        self._ttl = float(approval_ttl_seconds)
        self._token = token_factory or (lambda: secrets.token_urlsafe(32))
        self._approvals: dict[tuple[str, str], float] = {}
        self._grants: dict[str, BuildFixGrant] = {}
        self._by_job: dict[str, str] = {}
        self._usage: dict[str, _Usage] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for key in [key for key, until in self._approvals.items() if until <= now]:
            self._approvals.pop(key, None)
        for token in [token for token, grant in self._grants.items() if grant.expired(now)]:
            self._drop(token)

    def _drop(self, token: str) -> None:
        grant = self._grants.pop(token, None)
        self._usage.pop(token, None)
        if grant is not None and self._by_job.get(grant.job_id) == token:
            self._by_job.pop(grant.job_id, None)

    def approve(self, plan_digest: str, principal_id: str) -> None:
        if not isinstance(plan_digest, str) or not _DIGEST_RE.fullmatch(plan_digest):
            raise ValueError("plan_digest must be a sha256 hex digest")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("an approval is bound to a principal")
        now = self._clock()
        with self._lock:
            self._expire(now)
            if len(self._approvals) >= MAX_OUTSTANDING_APPROVALS:
                oldest = min(self._approvals, key=self._approvals.get)
                self._approvals.pop(oldest, None)
            self._approvals[(principal_id, plan_digest)] = now + self._ttl

    def approved(self, plan_digest: str, principal_id: str) -> bool:
        now = self._clock()
        with self._lock:
            self._expire(now)
            return (principal_id, plan_digest) in self._approvals

    def issue(self, spec: BuildFixGrantSpec, *, principal_id: str, job_id: str,
              plan_digest: str) -> BuildFixGrant | None:
        """Mint the grant for an approved plan; None when there is no approval."""
        if not isinstance(spec, BuildFixGrantSpec):
            raise ValueError("spec must be a BuildFixGrantSpec")
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._approvals.pop((principal_id, plan_digest), None) is None:
                return None
            if spec.expires_at <= now:
                return None
            if len(self._grants) >= MAX_LIVE_GRANTS:
                return None
            previous = self._by_job.get(job_id)
            if previous is not None:
                self._drop(previous)
            grant = BuildFixGrant(token=self._token(), principal_id=principal_id, job_id=job_id,
                                  plan_digest=plan_digest, spec=spec, issued_at=now)
            self._grants[grant.token] = grant
            self._by_job[job_id] = grant.token
            self._usage[grant.token] = _Usage()
            return grant

    def lookup(self, token: str) -> BuildFixGrant | None:
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._grants.get(token) if isinstance(token, str) else None

    def revoke(self, grant_or_job: BuildFixGrant | str | None) -> None:
        if grant_or_job is None:
            return
        with self._lock:
            if isinstance(grant_or_job, BuildFixGrant):
                self._drop(grant_or_job.token)
                return
            token = self._by_job.get(str(grant_or_job))
            if token is not None:
                self._drop(token)

    def authorize(self, token: str, *, principal_id: str, tool_name: str,
                  arguments: Mapping[str, Any]) -> GrantDecision:
        """Match one grant-carrying file call and charge it to the loop budget."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            grant = self._grants.get(token) if isinstance(token, str) else None
            if grant is None:
                return _deny("unknown or expired fix grant")
            decision = match_file_call(grant, principal_id=principal_id, tool_name=tool_name,
                                       arguments=arguments, now=now)
            if not decision.allowed or tool_name not in WRITE_TOOLS:
                return decision
            usage = self._usage.setdefault(token, _Usage())
            spec = grant.spec
            files = usage.files | set(decision.files)
            if len(files) > spec.max_files:
                return _deny("the fix may touch at most %d files" % spec.max_files)
            if usage.writes + 1 > spec.max_writes:
                return _deny("the fix used up its write budget")
            if decision.changed_lines > spec.max_changed_lines * 2:
                return _deny("the write changes more lines than the fix may")
            usage.files = files
            usage.writes += 1
            usage.changed_lines += decision.changed_lines
            return decision

    def authorize_build(self, token: str, **kwargs: Any) -> GrantDecision:
        now = self._clock()
        with self._lock:
            self._expire(now)
            grant = self._grants.get(token) if isinstance(token, str) else None
        if grant is None:
            return _deny("unknown or expired fix grant")
        return match_child_build(grant, now=now, **kwargs)

    def live(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._grants)


__all__ = [
    "BuildFixGrant", "BuildFixGrantBook", "BuildFixGrantSpec", "FILE_TOOLS", "FileSetScope",
    "GRANT_SOURCE_PREFIX", "GRANT_TOKEN_PREFIX", "GrantDecision", "WRITE_TOOLS",
    "diff_files_and_lines", "grant_carrier", "grant_token_from", "match_child_build",
    "match_file_call", "relative_in_root", "restore_plan_digest", "sha256_hex",
]
