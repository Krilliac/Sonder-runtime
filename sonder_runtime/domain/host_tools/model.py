"""Pure host tool inventory model.

The inventory records which developer tools exist on this host, where they
were discovered, and what fixed version probe (if any) reported.  Everything
here is pure: no filesystem, environment, subprocess, or clock access.  The
adapters own discovery; the application service owns caching; this module
owns the wire format, its validation, path redaction, and the compact model
context summary.

Security note: a snapshot is persisted in the state home, which a model may
be able to write.  ``snapshot_from_wire`` therefore validates every bound and
enum, but a valid snapshot is still never trusted for execution -- callers
re-validate a path with the adapter guard before launching it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Callable, Iterable, Mapping

from ..common.errors import InvalidInput


SNAPSHOT_SCHEMA = "sonder.host-tools/v1"
MAX_TOOLS = 512
MAX_ALTERNATIVES = 4
MAX_DETAILS = 8
MAX_DETAIL_KEY_CHARS = 64
MAX_DETAIL_VALUE_CHARS = 200
MAX_NOTES = 16
MAX_NOTE_CHARS = 200
MAX_VERSION_CHARS = 64
MAX_PATH_CHARS = 4096
MAX_IDENTITY_CHARS = 64
MAX_SUMMARY_CHARS = 480
DEFAULT_VERSION_PATTERN = r"(\d+(?:\.\d+){1,3})"
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9+._-]{1,64}$")
_VERSION_SCAN_CHARS = 2_000

# Tools that are inventoried but deliberately left out of the one-line model
# context summary (specialist or rarely useful tools that would crowd out the
# common ones).  The registry derives ``ToolSpec.in_brief`` from this single
# set so the two can never drift.
BRIEF_EXCLUDED_NAMES = frozenset({
    "ccache", "sccache", "xperf", "wpaexporter", "doxygen", "nssm", "clcache",
})


class ToolCategory(str, Enum):
    COMPILER = "compiler"
    BUILD_SYSTEM = "build_system"
    TEST_RUNNER = "test_runner"
    LINTER_FORMATTER = "linter_formatter"
    DEBUGGER_PROFILER = "debugger_profiler"
    PACKAGE_MANAGER = "package_manager"
    RUNTIME = "runtime"
    CONTAINER_VM = "container_vm"
    VCS = "vcs"
    DB_CLIENT = "db_client"
    MEDIA_DOC = "media_doc"
    CLOUD_CLI = "cloud_cli"
    EDITOR_IDE = "editor_ide"
    SHELL = "shell"


CATEGORY_ORDER: tuple[ToolCategory, ...] = tuple(ToolCategory)
_CATEGORY_RANK = {category: index for index, category in enumerate(CATEGORY_ORDER)}

# Short labels for the one-line capability summary.
CATEGORY_BRIEF_LABELS: Mapping[ToolCategory, str] = {
    ToolCategory.COMPILER: "compilers",
    ToolCategory.BUILD_SYSTEM: "build",
    ToolCategory.TEST_RUNNER: "tests",
    ToolCategory.LINTER_FORMATTER: "lint",
    ToolCategory.DEBUGGER_PROFILER: "debug",
    ToolCategory.PACKAGE_MANAGER: "packages",
    ToolCategory.RUNTIME: "runtimes",
    ToolCategory.CONTAINER_VM: "containers",
    ToolCategory.VCS: "vcs",
    ToolCategory.DB_CLIENT: "db",
    ToolCategory.MEDIA_DOC: "media",
    ToolCategory.CLOUD_CLI: "cloud",
    ToolCategory.EDITOR_IDE: "editors",
    ToolCategory.SHELL: "shells",
}


class DiscoverySource(str, Enum):
    PATH = "path"
    KNOWN_PREFIX = "known_prefix"
    VSWHERE = "vswhere"
    WINDOWS_SDK = "windows_sdk"
    APP_PATHS = "app_paths"
    PY_LAUNCHER = "py_launcher"
    SCOOP = "scoop"
    CHOCO = "choco"
    WINGET = "winget"
    BREW = "brew"
    XCODE = "xcode"
    APP_BUNDLE = "app_bundle"


class VersionStatus(str, Enum):
    OK = "ok"
    NOT_PROBED = "not_probed"
    DEFERRED = "deferred"
    TIMEOUT = "timeout"
    FAILED = "failed"
    OUTPUT_LIMIT = "output_limit"
    PROJECT_LOCAL = "project_local_not_probed"
    ALIAS = "alias_not_probed"
    FROM_METADATA = "from_metadata"


# Statuses whose version text is meaningful evidence (still not attestation).
VERSION_BEARING_STATUSES = frozenset({
    VersionStatus.OK, VersionStatus.OUTPUT_LIMIT, VersionStatus.FROM_METADATA,
})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Host-owned description of one tool and its only permitted probe."""

    name: str
    category: ToolCategory
    executables: tuple[str, ...]
    version_args: tuple[str, ...] | None
    version_pattern: str = DEFAULT_VERSION_PATTERN
    platforms: frozenset[str] = frozenset({"any"})
    in_brief: bool = True
    probe_env: tuple[tuple[str, str], ...] = ()

    def supports(self, system: str) -> bool:
        return "any" in self.platforms or system in self.platforms


@dataclass(frozen=True, slots=True)
class ToolRecord:
    name: str
    category: ToolCategory
    path: str
    source: DiscoverySource
    on_path: bool
    version: str
    version_status: VersionStatus
    identity: str
    alternatives: tuple[str, ...] = ()
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    os: str
    os_release: str
    machine: str
    created_at: float
    duration_ms: int
    tools: tuple[ToolRecord, ...]
    truncated: bool = False
    notes: tuple[str, ...] = ()
    digest: str = ""
    schema: str = SNAPSHOT_SCHEMA


@dataclass(frozen=True, slots=True)
class ToolView:
    name: str
    category: str
    version: str
    version_status: str
    source: str
    on_path: bool
    path_display: str
    alternatives_display: tuple[str, ...]
    details: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class InventoryView:
    snapshot_digest: str
    created_at: float
    age_seconds: int
    stale: bool
    os: str
    machine: str
    counts: tuple[tuple[str, int], ...]
    tools: tuple[ToolView, ...]
    filtered_by: str
    notes: tuple[str, ...] = field(default=())
    truncated: bool = False


# ---------------------------------------------------------------------------
# Helpers


def _clip(text: str, limit: int) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return text[:limit]


def is_absolute_host_path(path: str) -> bool:
    """True for a POSIX, drive-letter, or UNC absolute path (OS independent)."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    if path.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", path):
        return True
    return path.startswith("\\\\")


def sort_key(record: ToolRecord) -> tuple[int, str]:
    return (_CATEGORY_RANK[record.category], record.name.lower())


def _record_to_wire(record: ToolRecord) -> dict:
    return {
        "name": record.name,
        "category": record.category.value,
        "path": record.path,
        "source": record.source.value,
        "on_path": bool(record.on_path),
        "version": record.version,
        "version_status": record.version_status.value,
        "identity": record.identity,
        "alternatives": list(record.alternatives),
        "details": [[key, value] for key, value in record.details],
    }


def snapshot_digest(tools: Iterable[ToolRecord]) -> str:
    """sha256 over the canonical JSON of the tool records."""
    canonical = json.dumps(
        [_record_to_wire(record) for record in tools],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def snapshot_to_wire(snapshot: InventorySnapshot) -> dict:
    return {
        "schema": snapshot.schema,
        "os": snapshot.os,
        "os_release": snapshot.os_release,
        "machine": snapshot.machine,
        "created_at": float(snapshot.created_at),
        "duration_ms": int(snapshot.duration_ms),
        "tools": [_record_to_wire(record) for record in snapshot.tools],
        "truncated": bool(snapshot.truncated),
        "notes": list(snapshot.notes),
        "digest": snapshot.digest,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInput(message)


def _wire_str(value: object, limit: int, what: str, *, allow_empty: bool = True) -> str:
    _require(isinstance(value, str), f"{what} must be a string")
    assert isinstance(value, str)
    _require(len(value) <= limit, f"{what} exceeds {limit} characters")
    _require("\x00" not in value, f"{what} contains NUL")
    _require(allow_empty or bool(value), f"{what} must not be empty")
    return value


def _wire_enum(enum_type, value: object, what: str):
    _require(isinstance(value, str), f"{what} must be a string")
    try:
        return enum_type(value)
    except ValueError:
        raise InvalidInput(f"unknown {what}") from None


def _record_from_wire(data: object) -> ToolRecord:
    _require(isinstance(data, Mapping), "tool record must be an object")
    assert isinstance(data, Mapping)
    name = _wire_str(data.get("name"), 64, "tool name", allow_empty=False)
    _require(bool(TOOL_NAME_PATTERN.match(name)), "tool name is invalid")
    path = _wire_str(data.get("path"), MAX_PATH_CHARS, "tool path", allow_empty=False)
    _require(is_absolute_host_path(path), "tool path must be absolute")
    on_path = data.get("on_path")
    _require(isinstance(on_path, bool), "on_path must be a boolean")
    alternatives = data.get("alternatives", [])
    _require(isinstance(alternatives, list), "alternatives must be a list")
    _require(len(alternatives) <= MAX_ALTERNATIVES, "too many alternatives")
    alt_paths = []
    for item in alternatives:
        alt = _wire_str(item, MAX_PATH_CHARS, "alternative path", allow_empty=False)
        _require(is_absolute_host_path(alt), "alternative path must be absolute")
        alt_paths.append(alt)
    details = data.get("details", [])
    _require(isinstance(details, list), "details must be a list")
    _require(len(details) <= MAX_DETAILS, "too many details")
    pairs = []
    for item in details:
        _require(isinstance(item, list) and len(item) == 2, "detail must be a pair")
        key = _wire_str(item[0], MAX_DETAIL_KEY_CHARS, "detail key", allow_empty=False)
        value = _wire_str(item[1], MAX_DETAIL_VALUE_CHARS, "detail value")
        pairs.append((key, value))
    return ToolRecord(
        name=name,
        category=_wire_enum(ToolCategory, data.get("category"), "category"),
        path=path,
        source=_wire_enum(DiscoverySource, data.get("source"), "discovery source"),
        on_path=on_path,
        version=_wire_str(data.get("version"), MAX_VERSION_CHARS, "version"),
        version_status=_wire_enum(VersionStatus, data.get("version_status"), "version status"),
        identity=_wire_str(data.get("identity"), MAX_IDENTITY_CHARS, "identity"),
        alternatives=tuple(alt_paths),
        details=tuple(pairs),
    )


def snapshot_from_wire(data: Mapping) -> InventorySnapshot:
    """Validate and rebuild a snapshot; raises ``InvalidInput`` on any defect."""
    _require(isinstance(data, Mapping), "snapshot must be an object")
    _require(data.get("schema") == SNAPSHOT_SCHEMA, "snapshot schema mismatch")
    tools = data.get("tools")
    _require(isinstance(tools, list), "tools must be a list")
    assert isinstance(tools, list)
    _require(len(tools) <= MAX_TOOLS, "too many tools")
    records = tuple(_record_from_wire(item) for item in tools)
    notes = data.get("notes", [])
    _require(isinstance(notes, list) and len(notes) <= MAX_NOTES, "notes are invalid")
    note_values = tuple(_wire_str(note, MAX_NOTE_CHARS, "note") for note in notes)
    created_at = data.get("created_at")
    _require(type(created_at) in (int, float) and created_at >= 0, "created_at is invalid")
    duration = data.get("duration_ms")
    _require(type(duration) is int and duration >= 0, "duration_ms is invalid")
    truncated = data.get("truncated")
    _require(isinstance(truncated, bool), "truncated must be a boolean")
    digest = _wire_str(data.get("digest"), 64, "digest")
    _require(digest == snapshot_digest(records), "snapshot digest mismatch")
    return InventorySnapshot(
        os=_wire_str(data.get("os"), 64, "os"),
        os_release=_wire_str(data.get("os_release"), 128, "os_release"),
        machine=_wire_str(data.get("machine"), 64, "machine"),
        created_at=float(created_at),
        duration_ms=duration,
        tools=records,
        truncated=truncated,
        notes=note_values,
        digest=digest,
    )


def build_snapshot(
    *,
    os: str,
    os_release: str,
    machine: str,
    created_at: float,
    duration_ms: int,
    tools: Iterable[ToolRecord],
    notes: Iterable[str] = (),
    truncated: bool = False,
) -> InventorySnapshot:
    """Build a bounded, sorted, digested snapshot from discovered records."""
    ordered = sorted(tools, key=sort_key)
    if len(ordered) > MAX_TOOLS:
        ordered = ordered[:MAX_TOOLS]
        truncated = True
    note_list = [_clip(note, MAX_NOTE_CHARS) for note in notes]
    if len(note_list) > MAX_NOTES:
        note_list = note_list[:MAX_NOTES]
    records = tuple(ordered)
    return InventorySnapshot(
        os=_clip(os, 64),
        os_release=_clip(os_release, 128),
        machine=_clip(machine, 64),
        created_at=float(created_at),
        duration_ms=max(0, int(duration_ms)),
        tools=records,
        truncated=bool(truncated),
        notes=tuple(note_list),
        digest=snapshot_digest(records),
    )


def is_stale(snapshot: InventorySnapshot | None, *, now: float, ttl_seconds: int) -> bool:
    """A snapshot is stale at ``ttl_seconds`` of age, or when dated in the future."""
    if snapshot is None:
        return True
    age = now - snapshot.created_at
    if age < -300:
        return True
    return age >= ttl_seconds


def parse_version(text: str, pattern: str = DEFAULT_VERSION_PATTERN) -> str:
    """Extract a version from bounded probe output ("" when absent)."""
    if not text:
        return ""
    match = re.search(pattern, str(text)[:_VERSION_SCAN_CHARS])
    if match is None:
        return ""
    value = match.group(1) if match.groups() else match.group(0)
    return _clip(value or "", MAX_VERSION_CHARS).strip()


# ---------------------------------------------------------------------------
# Redaction

_WINDOWS_USERS = re.compile(r"^([A-Za-z]:[\\/])Users[\\/]([^\\/]+)(?=$|[\\/])", re.IGNORECASE)


def _looks_windows(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:", path)) or "\\" in path


def _strip_sep(path: str) -> str:
    if len(path) > 1 and path[-1] in "/\\" and not re.match(r"^[A-Za-z]:[\\/]$", path):
        return path[:-1]
    return path


def _prefix_replace(path: str, prefix: str, replacement: str, *, fold: bool) -> str | None:
    prefix = _strip_sep(prefix)
    if not prefix or prefix in ("/", "\\"):
        return None
    # Compare an equal-length slice: casefold() can change a string's length
    # (``"ß"`` -> ``"ss"``), so folding the whole path and then indexing by
    # the prefix length would mis-slice or raise.
    head = path[:len(prefix)]
    same = head.casefold() == prefix.casefold() if fold else head == prefix
    if not same:
        return None
    if len(path) == len(prefix):
        return replacement
    if path[len(prefix)] in "/\\":
        return replacement + path[len(prefix):]
    return None


def redact_path(path: str, *, home: str, user: str, workspace_roots: tuple[str, ...]) -> str:
    """Redact user-identifying parts of a host path for model-visible text.

    Workspace roots become ``[WORKSPACE]``, the home directory ``~`` (or
    ``%USERPROFILE%`` for a Windows ``C:\\Users\\<name>`` profile), and any
    remaining path segment equal to the user name ``<user>``.  Unrelated paths
    are returned unchanged.
    """
    if not isinstance(path, str) or not path:
        return "" if not isinstance(path, str) else path
    fold = _looks_windows(path)
    for root in sorted((r for r in workspace_roots if r), key=len, reverse=True):
        replaced = _prefix_replace(path, root, "[WORKSPACE]", fold=fold)
        if replaced is not None:
            path = replaced
            break
    else:
        profile = _WINDOWS_USERS.match(path)
        if profile is not None:
            path = "%USERPROFILE%" + path[profile.end():]
        elif home:
            home_fold = fold or _looks_windows(home)
            replacement = "%USERPROFILE%" if _looks_windows(home) else "~"
            replaced = _prefix_replace(path, home, replacement, fold=home_fold)
            if replaced is not None:
                path = replaced
    if user and len(user) >= 2:
        parts = re.split(r"([\\/])", path)
        target = user.casefold() if fold else user
        path = "".join(
            "<user>" if (part.casefold() if fold else part) == target else part
            for part in parts
        )
    return path


# ---------------------------------------------------------------------------
# Views


def find_tool(snapshot: InventorySnapshot | None, name: str) -> ToolRecord | None:
    """Case-insensitive lookup by record name or discovered executable name."""
    if snapshot is None or not isinstance(name, str) or not name:
        return None
    wanted = name.strip().casefold()
    for record in snapshot.tools:
        if record.name.casefold() == wanted:
            return record
    for record in snapshot.tools:
        base = re.split(r"[\\/]", record.path)[-1].casefold()
        stem = re.sub(r"\.(exe|cmd|bat|com|ps1)$", "", base)
        if wanted in (base, stem):
            return record
    return None


def _validate_filters(category: str | None, name: str | None) -> tuple[ToolCategory | None, str | None]:
    parsed = None
    if category is not None and category != "":
        try:
            parsed = ToolCategory(category)
        except ValueError:
            raise InvalidInput("unknown tool category") from None
    wanted = None
    if name is not None and name != "":
        if not isinstance(name, str) or not TOOL_NAME_PATTERN.match(name):
            raise InvalidInput("invalid tool name")
        wanted = name
    return parsed, wanted


def build_view(
    snapshot: InventorySnapshot,
    *,
    now: float,
    ttl_seconds: int,
    category: str | None = None,
    name: str | None = None,
    redact: Callable[[str], str],
) -> InventoryView:
    """Filtered, redacted projection of *snapshot* for model or HTTP callers.

    Every free-text field (version, paths, details, notes) is redacted and
    then clipped to printable characters: the snapshot file lives in a state
    home a model may write, so its text must not carry line breaks or control
    characters into model-visible output.
    """
    parsed_category, wanted = _validate_filters(category, name)
    tools: Iterable[ToolRecord] = snapshot.tools
    filtered = []
    if parsed_category is not None:
        tools = [record for record in tools if record.category is parsed_category]
        filtered.append("category=" + parsed_category.value)
    if wanted is not None:
        match = find_tool(
            InventorySnapshot(os="", os_release="", machine="", created_at=0.0,
                              duration_ms=0, tools=tuple(tools)),
            wanted,
        )
        tools = [match] if match is not None else []
        filtered.append("name=" + wanted)
    counts: dict[str, int] = {}
    for record in snapshot.tools:
        counts[record.category.value] = counts.get(record.category.value, 0) + 1
    views = tuple(
        ToolView(
            name=record.name,
            category=record.category.value,
            version=_clip(redact(record.version), MAX_VERSION_CHARS) if record.version else "",
            version_status=record.version_status.value,
            source=record.source.value,
            on_path=record.on_path,
            path_display=_clip(redact(record.path), MAX_PATH_CHARS),
            alternatives_display=tuple(
                _clip(redact(item), MAX_PATH_CHARS) for item in record.alternatives
            ),
            details=tuple(
                (_clip(key, MAX_DETAIL_KEY_CHARS), _clip(redact(value), MAX_DETAIL_VALUE_CHARS))
                for key, value in record.details
            ),
        )
        for record in tools
    )
    return InventoryView(
        snapshot_digest=snapshot.digest,
        created_at=snapshot.created_at,
        age_seconds=max(0, int(now - snapshot.created_at)),
        stale=is_stale(snapshot, now=now, ttl_seconds=ttl_seconds),
        os=snapshot.os,
        machine=snapshot.machine,
        counts=tuple((c.value, counts[c.value]) for c in CATEGORY_ORDER if c.value in counts),
        tools=views,
        filtered_by=",".join(filtered),
        notes=tuple(_clip(redact(note), MAX_NOTE_CHARS) for note in snapshot.notes),
        truncated=snapshot.truncated,
    )


def view_to_wire(view: InventoryView) -> dict:
    return {
        "object": "tool_inventory",
        "snapshot_digest": view.snapshot_digest,
        "created_at": view.created_at,
        "age_seconds": view.age_seconds,
        "stale": view.stale,
        "os": view.os,
        "machine": view.machine,
        "counts": {key: value for key, value in view.counts},
        "tools": [
            {
                "name": tool.name,
                "category": tool.category,
                "version": tool.version,
                "version_status": tool.version_status,
                "source": tool.source,
                "on_path": tool.on_path,
                "path": tool.path_display,
                "alternatives": list(tool.alternatives_display),
                "details": {key: value for key, value in tool.details},
            }
            for tool in view.tools
        ],
        "filtered_by": view.filtered_by,
        "notes": list(view.notes),
        "truncated": view.truncated,
    }


def _short_version(version: str) -> str:
    match = re.match(r"^(\d+(?:\.\d+)?)", version or "")
    return match.group(1) if match else ""


def capability_summary(snapshot: InventorySnapshot | None, *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """One path-free line naming discovered tools by category.

    Example: ``compilers: gcc 13.2, clang 18.1; build: cmake 3.28``.  Tools in
    ``BRIEF_EXCLUDED_NAMES`` are omitted; when the line would exceed
    ``max_chars`` whole entries are dropped and ``+N more`` is appended.
    """
    if snapshot is None or not snapshot.tools:
        return ""
    limit = max(0, min(int(max_chars), MAX_SUMMARY_CHARS))
    groups: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for category in CATEGORY_ORDER:
        entries = []
        for record in sorted(
            (r for r in snapshot.tools if r.category is category), key=lambda r: r.name.lower()
        ):
            key = record.name.casefold()
            if key in seen or key in BRIEF_EXCLUDED_NAMES:
                continue
            if not re.fullmatch(r"[A-Za-z0-9+._-]{1,64}", record.name):
                continue
            seen.add(key)
            version = (
                _short_version(record.version)
                if record.version_status in VERSION_BEARING_STATUSES else ""
            )
            entries.append(f"{record.name} {version}".strip())
        if entries:
            groups.append((CATEGORY_BRIEF_LABELS[category], entries))
    if not groups:
        return ""
    flat = [(label, entry) for label, entries in groups for entry in entries]
    total = len(flat)

    def render(count: int) -> str:
        parts: list[str] = []
        current_label = None
        current: list[str] = []
        for label, entry in flat[:count]:
            if label != current_label:
                if current_label is not None:
                    parts.append(f"{current_label}: {', '.join(current)}")
                current_label, current = label, []
            current.append(entry)
        if current_label is not None:
            parts.append(f"{current_label}: {', '.join(current)}")
        text = "; ".join(parts)
        if count < total:
            text = (text + "; " if text else "") + f"+{total - count} more"
        return text

    count = total
    text = render(count)
    while len(text) > limit and count > 0:
        count -= 1
        text = render(count)
    return text if len(text) <= limit else ""


__all__ = [
    "BRIEF_EXCLUDED_NAMES",
    "CATEGORY_BRIEF_LABELS",
    "CATEGORY_ORDER",
    "DEFAULT_VERSION_PATTERN",
    "DiscoverySource",
    "InventorySnapshot",
    "InventoryView",
    "MAX_ALTERNATIVES",
    "MAX_DETAILS",
    "MAX_NOTES",
    "MAX_SUMMARY_CHARS",
    "MAX_TOOLS",
    "SNAPSHOT_SCHEMA",
    "TOOL_NAME_PATTERN",
    "ToolCategory",
    "ToolRecord",
    "ToolSpec",
    "ToolView",
    "VERSION_BEARING_STATUSES",
    "VersionStatus",
    "build_snapshot",
    "build_view",
    "capability_summary",
    "find_tool",
    "is_absolute_host_path",
    "is_stale",
    "parse_version",
    "redact_path",
    "snapshot_digest",
    "snapshot_from_wire",
    "snapshot_to_wire",
    "sort_key",
    "view_to_wire",
]
