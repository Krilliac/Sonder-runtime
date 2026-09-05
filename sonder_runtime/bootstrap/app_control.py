"""Private host project policy snapshots; no request data or credential authority."""

from dataclasses import dataclass, field
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from ..application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ..platform.app_control_config import app_control_errors

MAX_CATALOG_BYTES = 262144


@dataclass(frozen=True)
class ProjectGrant:
    grant_id: str
    revision: int
    project: str
    accounts: tuple[str, ...] = field(repr=False)
    role: str
    roots: tuple[str, ...] = field(repr=False)
    tools: tuple[str, ...]
    allow_cloud: bool
    allow_remote: bool
    expires_at: float
    digest: str
    catalog_digest: str
    file_identity: tuple[int, int, int, int]
    runtime_id: str


@dataclass(frozen=True)
class ProjectGrantSnapshot:
    grants: tuple[ProjectGrant, ...] = field(repr=False)
    catalog_digest: str
    file_identity: tuple[int, int, int, int]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate catalog key")
        result[key] = value
    return result


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid policy identifier")
    return value


def _root(value):
    if type(value) is not str or len(value) > 4096:
        raise ValueError("invalid workspace root")
    path = Path(value)
    if (
        not path.is_absolute()
        or not path.is_dir()
        or str(path.absolute()) != str(path.resolve())
    ):
        raise ValueError("workspace root must be canonical and existing")
    return path.resolve()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class AppProjectGrantCatalog:
    """Trusted composition supplies live full workspace/inventory providers.

    No fallback, directory creation, environment policy, or catalog writes.
    A caller must still authenticate account/session and enforce actual effects.
    """

    def __init__(
        self, *, config_provider, workspace_roots, private_inventory, clock=time.time
    ):
        self._config = config_provider
        self._roots = workspace_roots
        self._inventory = private_inventory
        self._clock = clock

    def _boundary(self, config, path):
        roots = tuple(islice(self._roots(), 257))
        if not roots or len(roots) > 256:
            raise PermissionError("app policy unavailable")
        canonical = tuple(_root(str(value)) for value in roots)
        for root in canonical:
            if (
                root == path.parent
                or root in path.parents
                or path.parent in root.parents
            ):
                raise PermissionError("app policy overlaps model workspace")
        # Loaded TOML/secrets/binding provenance is part of the private closure,
        # even if a caller's supplemental inventory accidentally omits it.
        sources = config.private_source_paths
        if type(sources) is not tuple or len(sources) > 256:
            raise PermissionError("private source closure exceeds bound")
        for source in sources:
            private = Path(source)
            if not private.is_absolute():
                raise PermissionError("private source must be absolute")
            private = private.resolve()
            if any(
                root == private.parent
                or root in private.parents
                or private.parent in root.parents
                for root in canonical
            ):
                raise PermissionError("private source overlaps model workspace")
        self._inventory().require_disjoint(canonical)
        return canonical

    def snapshot(self):
        config = self._config()
        if not config.app_control.enabled or app_control_errors(config):
            raise PermissionError("app policy unavailable")
        path = Path(config.app_control.catalog_file)
        try:
            roots = self._boundary(config, path)
            with PrivateDirectoryAnchor(path.parent) as anchor:
                with anchor.open_read(path.name) as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise ValueError("catalog must be a single-link regular file")
                    if os.name != "nt" and (
                        before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) & 0o077
                    ):
                        raise ValueError(
                            "catalog ownership or permissions are not private"
                        )
                    raw = stream.read(MAX_CATALOG_BYTES + 1)
                    if len(raw) > MAX_CATALOG_BYTES or _identity(before) != _identity(
                        os.fstat(stream.fileno())
                    ):
                        raise ValueError("catalog changed or exceeds bound")
                    anchor.validate()
                    if _identity(before) != _identity(path.stat(follow_symlinks=False)):
                        raise ValueError("catalog replaced")
                    data = json.loads(raw, object_pairs_hook=_pairs)
                    if (
                        type(data) is not dict
                        or set(data) != {"version", "grants"}
                        or type(data["version"]) is not int
                        or data["version"] != 1
                    ):
                        raise ValueError("invalid catalog envelope")
                    entries = data["grants"]
                    if type(entries) is not list or not 1 <= len(entries) <= 64:
                        raise ValueError("catalog count bound")
                    digest = hashlib.sha256(raw).hexdigest()
                    identity = _identity(before)
                    grants = tuple(
                        self._grant(item, roots, config, digest, identity)
                        for item in entries
                    )
                    if len({g.grant_id for g in grants}) != len(grants) or len(
                        {g.project for g in grants}
                    ) != len(grants):
                        raise ValueError("duplicate grant identity or project")
                    if config != self._config() or roots != self._boundary(
                        config, path
                    ):
                        raise ValueError("live app policy changed")
                    if any(g.expires_at <= self._clock() for g in grants):
                        raise ValueError("app policy expired")
                    if identity != _identity(path.stat(follow_symlinks=False)):
                        raise ValueError("catalog replaced")
                    return ProjectGrantSnapshot(grants, digest, identity)
        except Exception:
            raise PermissionError("app policy unavailable") from None

    def _grant(self, item, roots, config, catalog_digest, identity):
        keys = {
            "grant_id",
            "revision",
            "project",
            "accounts",
            "role",
            "roots",
            "tools",
            "allow_cloud",
            "allow_remote",
            "expires_at",
        }
        if type(item) is not dict or set(item) != keys:
            raise ValueError("invalid grant fields")
        gid, project = _identifier(item["grant_id"]), _identifier(item["project"])
        if (
            type(item["revision"]) is not int
            or not 1 <= item["revision"] <= 2**63 - 1
            or item["role"] != "admin"
        ):
            raise ValueError("invalid grant revision or role")
        accounts = item["accounts"]
        if (
            type(accounts) is not list
            or not 1 <= len(accounts) <= 64
            or any(
                type(a) is not str
                or not 3 <= len(a) <= 128
                or a != a.strip().lower()
                or any(ord(c) < 32 or ord(c) == 127 for c in a)
                for a in accounts
            )
            or len(set(accounts)) != len(accounts)
        ):
            raise ValueError("invalid normalized accounts")
        values = item["roots"]
        if type(values) is not list or not 1 <= len(values) <= 16:
            raise ValueError("invalid roots")
        selected = tuple(_root(value) for value in values)
        if len(set(selected)) != len(selected) or any(
            not any(root == allowed or allowed in root.parents for allowed in roots)
            for root in selected
        ):
            raise ValueError("grant root exceeds current workspace")
        tools = item["tools"]
        if (
            type(tools) is not list
            or not 1 <= len(tools) <= 64
            or len(set(tools)) != len(tools)
        ):
            raise ValueError("invalid tools")
        for tool in tools:
            _identifier(tool)
        for flag in ("allow_cloud", "allow_remote"):
            if type(item[flag]) is not bool:
                raise ValueError("invalid authority flag")
        if (
            item["allow_cloud"]
            and not config.features.cloud
            or item["allow_remote"]
            and not config.compute.allow_remote
        ):
            raise ValueError("grant exceeds current transport ceilings")
        expires = item["expires_at"]
        if (
            type(expires) not in (float, int)
            or not math.isfinite(expires)
            or not self._clock() < expires <= 253402300799
        ):
            raise ValueError("invalid finite expiry")
        canonical = json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return ProjectGrant(
            gid,
            item["revision"],
            project,
            tuple(accounts),
            "admin",
            tuple(str(p) for p in selected),
            tuple(tools),
            item["allow_cloud"],
            item["allow_remote"],
            float(expires),
            hashlib.sha256(canonical).hexdigest(),
            catalog_digest,
            identity,
            config.app_control.runtime_id,
        )

    def resolve(self, project, normalized_account, role):
        """Inputs come only from root's live authenticated account adapter."""
        if role != "admin":
            raise PermissionError("app policy unavailable")
        for grant in self.snapshot().grants:
            if grant.project == project and normalized_account in grant.accounts:
                return grant
        raise PermissionError("app policy unavailable")

    def require_current(self, grant):
        if type(grant) is not ProjectGrant:
            raise PermissionError("app policy unavailable")
        if grant not in self.snapshot().grants:
            raise PermissionError("app policy changed")
