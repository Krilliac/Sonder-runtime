"""Load, validate and merge the model tool policy files.

Two JSON files describe which models may drive tool-using lanes:

* the shipped defaults, ``adapters/data/model_tool_policy.json``, versioned
  with the code (the same pattern as ``semantic_tier_head.json``); and
* an optional operator overlay, ``<state home>/model_tool_policy.json``
  (``SONDER_MODEL_TOOL_POLICY`` names another path).  It is a different file
  from ``runtime_policy.json`` so that ``runtime_policy_update`` can never
  touch it.  This module only reads it; the one writer is the console
  editor (``adapters.model_tool_policy_editor``, operator-attended only), and
  nothing rewrites a bad overlay: it is left exactly as the operator wrote it.

Merge rules.  A shipped profile marked ``"floor": true`` can neither be
removed nor narrowed: its names and prefixes are always in force, whatever
the overlay says.  The overlay may add names and prefixes (to a new profile or
to a floor profile), may add ``tools`` and ``lanes`` to any profile (a floor
included; an overlay profile whose ``id`` names a shipped profile extends it
and may leave ``match`` empty), may add secret path globs and may add
served-model ids that name no model.  It may not set ``floor`` and may not set
``restricted_tool_ceiling`` (only the shipped file does).  Merging only ever
adds: an overlay cannot remove a shipped entry of any kind.

Every model a profile matches stays on the tool-lane deny list.  A profile's
``tools`` are a grant only in the lanes it lists, and only for tools that are
in the shipped ceiling and pass the safety predicate; that filtering and the
grant itself are ``adapters.model_tool_grants`` and
``domain.model_tool_grants``.  This module records where every entry came from
(:class:`Entry`) so the status can show it.

Failure rules (fail closed).  A shipped file that is missing, unreadable or
invalid yields an unavailable floor (``ToolLaneFloor.available`` False): every
tool-using lane refuses every model, and the status text names the file.  An
overlay that is unreadable or invalid is ignored as a whole: the floor still
applies, caller-named (free-form) models fail closed through the deny list's
``readable`` flag, and the error is reported.  A missing overlay is normal.

The pure rule is ``sonder_runtime.domain.tool_lane_models``; this module only
supplies its inputs.  Results are cached against each file's identity (path,
modification time, size, inode); a failed read is never cached.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from sonder_runtime.domain.runtime_policy.rules import _MODEL_RE
from sonder_runtime.domain.tool_lane_models import ToolLaneFloor, normalize_name
from sonder_runtime.platform import paths as runtime_paths

SCHEMA_VERSION = 1
OVERLAY_ENV = "SONDER_MODEL_TOOL_POLICY"
OVERLAY_FILE_NAME = "model_tool_policy.json"
SHIPPED_PATH = Path(__file__).resolve().parent / "data" / "model_tool_policy.json"
# A policy file is a short list of names; anything larger is not one.
MAX_FILE_BYTES = 1024 * 1024

CEILING_KEY = "restricted_tool_ceiling"
SECRET_GLOBS_KEY = "restricted_secret_path_globs"
_DOCUMENT_KEYS = frozenset({
    "version", "model_profiles", "undetermined_served_models", CEILING_KEY, SECRET_GLOBS_KEY,
})
_PROFILE_KEYS = frozenset({"id", "floor", "match", "tools", "lanes"})
_MATCH_KEYS = frozenset({"names", "prefixes"})
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A secret path glob: a short relative pattern, no drive, no parent step.
_GLOB_RE = re.compile(r"^[A-Za-z0-9._*?/\[\]!-]{1,128}$")


class ConfigSource(str, Enum):
    SHIPPED = "shipped"
    OVERLAY = "overlay"
    # The deprecated ``tool_lane_denied_models`` section of runtime_policy.json.
    LEGACY = "legacy"


@dataclass(frozen=True)
class ConfigIssue:
    """One reason a file could not be used (or one entry of it was refused)."""

    source: ConfigSource
    path: str
    message: str

    def text(self) -> str:
        return "%s config %s: %s" % (self.source.value, self.path, self.message)


@dataclass(frozen=True)
class Profile:
    """One validated model profile (names and prefixes already normalised)."""

    id: str
    floor: bool
    names: tuple[str, ...]
    prefixes: tuple[str, ...]
    tools: tuple[str, ...]
    lanes: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    """One merged value and the file it came from."""

    value: str
    source: ConfigSource


@dataclass(frozen=True)
class MergedProfile:
    """One profile after the merge: shipped entries first, overlay additions after."""

    id: str
    floor: bool
    names: tuple[Entry, ...]
    prefixes: tuple[Entry, ...]
    tools: tuple[Entry, ...]
    lanes: tuple[Entry, ...]

    def values(self, field: str) -> tuple[str, ...]:
        return tuple(entry.value for entry in getattr(self, field))

    def source_of(self, field: str, value: str) -> ConfigSource | None:
        return next((entry.source for entry in getattr(self, field) if entry.value == value), None)


@dataclass(frozen=True)
class ModelToolConfig:
    """The merged result of the shipped file and the overlay."""

    floor: ToolLaneFloor
    overlay_names: tuple[str, ...]
    overlay_prefixes: tuple[str, ...]
    readable_shipped: bool
    readable_overlay: bool
    overlay_present: bool
    shipped_path: str
    overlay_path: str
    issues: tuple[ConfigIssue, ...] = ()
    profiles: tuple[MergedProfile, ...] = ()
    tool_ceiling: tuple[str, ...] = ()
    secret_path_globs: tuple[Entry, ...] = ()

    def profile(self, profile_id: str) -> MergedProfile | None:
        return next((item for item in self.profiles if item.id == profile_id), None)

    @property
    def secret_globs(self) -> tuple[str, ...]:
        return tuple(entry.value for entry in self.secret_path_globs)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(issue.text() for issue in self.issues)

    def overlay_section(self) -> dict[str, list[str]]:
        """The overlay's additions in ``tool_lane_denied_models`` section shape."""
        return {"names": list(self.overlay_names), "prefixes": list(self.overlay_prefixes)}


# ---- parsing -----------------------------------------------------------------

def _entries(value: Any, where: str, *, pattern: re.Pattern[str] | None, problems: list[str]) -> list[str]:
    """A list of strings, each matching ``pattern`` (when given)."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        problems.append("%s must be a list" % where)
        return []
    result: list[str] = []
    for entry in value:
        text = entry.strip() if isinstance(entry, str) else ""
        if not isinstance(entry, str) or (pattern is not None and not pattern.fullmatch(text)):
            problems.append("invalid entry %r in %s" % (entry, where))
            continue
        result.append(text)
    return result


def _parse_profile(
    raw: Any, index: int, problems: list[str], extends: frozenset[str] = frozenset(),
) -> Profile | None:
    where = "model_profiles[%d]" % index
    if not isinstance(raw, dict):
        problems.append("%s must be an object" % where)
        return None
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        problems.append("unknown key(s) in %s: %s" % (where, ", ".join(sorted(map(str, unknown)))))
    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        problems.append("%s.id must be a lowercase identifier" % where)
        return None
    where = "profile %r" % profile_id
    floor = raw.get("floor", False)
    if not isinstance(floor, bool):
        problems.append("%s.floor must be true or false" % where)
        floor = False
    match = raw.get("match", {})
    if not isinstance(match, dict):
        problems.append("%s.match must be an object" % where)
        match = {}
    unknown = set(match) - _MATCH_KEYS
    if unknown:
        problems.append("unknown key(s) in %s.match: %s" % (where, ", ".join(sorted(map(str, unknown)))))
    names = _entries(match.get("names"), where + ".match.names", pattern=_MODEL_RE, problems=problems)
    prefixes = _entries(match.get("prefixes"), where + ".match.prefixes", pattern=_MODEL_RE, problems=problems)
    tools = _entries(raw.get("tools"), where + ".tools", pattern=_TOKEN_RE, problems=problems)
    lanes = _entries(raw.get("lanes"), where + ".lanes", pattern=_TOKEN_RE, problems=problems)
    if not names and not prefixes and profile_id not in extends:
        problems.append("%s matches no model" % where)
    return Profile(
        id=profile_id,
        floor=floor,
        names=tuple(dict.fromkeys(filter(None, (normalize_name(name) for name in names)))),
        prefixes=tuple(dict.fromkeys(filter(None, (normalize_name(item) for item in prefixes)))),
        tools=tuple(dict.fromkeys(tools)),
        lanes=tuple(dict.fromkeys(lanes)),
    )


@dataclass(frozen=True)
class _Document:
    profiles: tuple[Profile, ...]
    undetermined: frozenset[str]
    ceiling: tuple[str, ...]
    ceiling_present: bool
    globs: tuple[str, ...]
    problems: tuple[str, ...]


def _parse_document(raw: Any, *, extends: frozenset[str] = frozenset()) -> _Document:
    """One decoded document, validated; ``extends`` are ids whose profile may leave ``match`` empty."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return _Document((), frozenset(), (), False, (), ("the file must hold one JSON object",))
    unknown = set(raw) - _DOCUMENT_KEYS
    if unknown:
        problems.append("unknown key(s): %s" % ", ".join(sorted(map(str, unknown))))
    version = raw.get("version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        problems.append("version must be %d" % SCHEMA_VERSION)
    raw_profiles = raw.get("model_profiles", [])
    profiles: list[Profile] = []
    if not isinstance(raw_profiles, list):
        problems.append("model_profiles must be a list")
        raw_profiles = []
    for index, entry in enumerate(raw_profiles):
        profile = _parse_profile(entry, index, problems, extends)
        if profile is None:
            continue
        if any(existing.id == profile.id for existing in profiles):
            problems.append("duplicate profile id %r" % profile.id)
            continue
        profiles.append(profile)
    served = raw.get("undetermined_served_models", [])
    undetermined: set[str] = set()
    if not isinstance(served, list) or any(not isinstance(item, str) for item in served):
        problems.append("undetermined_served_models must be a list of strings")
    else:
        undetermined = {normalize_name(item) for item in served}
    ceiling = _entries(raw.get(CEILING_KEY), CEILING_KEY, pattern=_TOKEN_RE, problems=problems)
    globs = _entries(raw.get(SECRET_GLOBS_KEY), SECRET_GLOBS_KEY, pattern=_GLOB_RE, problems=problems)
    for glob in globs:
        if glob.startswith("/") or ".." in glob.split("/"):
            problems.append("invalid entry %r in %s: must be relative, without '..'" % (glob, SECRET_GLOBS_KEY))
    return _Document(
        profiles=tuple(profiles), undetermined=frozenset(undetermined),
        ceiling=tuple(dict.fromkeys(ceiling)), ceiling_present=CEILING_KEY in raw,
        globs=tuple(dict.fromkeys(globs)), problems=tuple(problems),
    )


# ---- file access -------------------------------------------------------------

def _identity(path: Path) -> tuple | None:
    """Identity of one file (path, mtime, size, inode); ``(path, None)`` when absent; None when it cannot be stat'ed."""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return (str(path), None)
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _read_json(path: Path) -> tuple[Any, str]:
    """``(decoded document, "")`` or ``(None, reason)``; never raises."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, "the file is larger than %d bytes" % MAX_FILE_BYTES
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "the file does not exist"
    except (OSError, ValueError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None, "%s: %s" % (type(exc).__name__, exc)


def read_document(path: Path) -> tuple[Any, str]:
    """``(decoded document, "")`` or ``(None, reason)`` for one policy file; never raises."""
    return _read_json(Path(path))


def overlay_path() -> Path:
    """The operator overlay's location (``SONDER_MODEL_TOOL_POLICY`` wins over the state home)."""
    override = os.environ.get(OVERLAY_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(runtime_paths.state_path(OVERLAY_FILE_NAME))


def _safe_overlay_path() -> tuple[Path | None, str]:
    try:
        return overlay_path(), ""
    except (OSError, ValueError, TypeError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def _load_shipped(path: Path) -> tuple[ToolLaneFloor, _Document | None, list[ConfigIssue]]:
    raw, reason = _read_json(path)
    problems: list[str] = [reason] if reason else []
    document: _Document | None = None
    if not reason:
        document = _parse_document(raw)
        problems = list(document.problems)
        if not any(profile.floor for profile in document.profiles):
            problems.append("no profile is marked \"floor\": true")
    if problems or document is None:
        issues = [ConfigIssue(ConfigSource.SHIPPED, str(path), problem) for problem in problems]
        return ToolLaneFloor.unavailable("%s: %s" % (path, "; ".join(problems))), None, issues
    names = frozenset(name for profile in document.profiles for name in profile.names)
    prefixes = tuple(dict.fromkeys(item for profile in document.profiles for item in profile.prefixes))
    floor = ToolLaneFloor(names=names, prefixes=prefixes, undetermined_served=document.undetermined)
    return floor, document, []


def overlay_problems(raw: Any, shipped: _Document | None) -> tuple[_Document, list[str]]:
    """Validate one decoded overlay document against the shipped one.

    Shared by the loader and the console editor, so a document the editor
    writes is exactly one the loader accepts.
    """
    extends = frozenset(profile.id for profile in shipped.profiles) if shipped is not None else frozenset()
    document = _parse_document(raw, extends=extends)
    problems = list(document.problems)
    for profile in document.profiles:
        if profile.floor:
            problems.append("profile %r: only the shipped file may mark a profile as a floor" % profile.id)
    if document.ceiling_present:
        problems.append("%s: only the shipped file sets the restricted tool ceiling" % CEILING_KEY)
    return document, problems


@dataclass(frozen=True)
class _Overlay:
    document: _Document | None
    readable: bool
    present: bool
    issues: tuple[ConfigIssue, ...]


def _load_overlay(path: Path | None, reason: str, shipped: _Document | None) -> _Overlay:
    if path is None:
        return _Overlay(None, False, False, (ConfigIssue(ConfigSource.OVERLAY, "(unknown)", reason),))
    identity = _identity(path)
    if identity is None:
        return _Overlay(None, False, True, (
            ConfigIssue(ConfigSource.OVERLAY, str(path), "the file cannot be inspected"),
        ))
    if identity[1:] == (None,):
        return _Overlay(None, True, False, ())
    raw, reason = _read_json(path)
    problems: list[str] = [reason] if reason else []
    document: _Document | None = None
    if not reason:
        document, problems = overlay_problems(raw, shipped)
    if problems:
        return _Overlay(None, False, True, tuple(
            ConfigIssue(ConfigSource.OVERLAY, str(path), problem) for problem in problems
        ))
    return _Overlay(document, True, True, ())


def _sourced(values: tuple[str, ...], source: ConfigSource) -> tuple[Entry, ...]:
    return tuple(Entry(value, source) for value in values)


def _add(existing: tuple[Entry, ...], values: tuple[str, ...], source: ConfigSource) -> tuple[Entry, ...]:
    seen = {entry.value for entry in existing}
    return existing + tuple(Entry(value, source) for value in values if value not in seen)


def _merge_profiles(shipped: _Document | None, overlay: _Document | None) -> tuple[MergedProfile, ...]:
    """Shipped profiles first, then the overlay's additions; nothing is ever removed."""
    merged: dict[str, MergedProfile] = {}
    for profile in shipped.profiles if shipped is not None else ():
        merged[profile.id] = MergedProfile(
            id=profile.id, floor=profile.floor,
            names=_sourced(profile.names, ConfigSource.SHIPPED),
            prefixes=_sourced(profile.prefixes, ConfigSource.SHIPPED),
            tools=_sourced(profile.tools, ConfigSource.SHIPPED),
            lanes=_sourced(profile.lanes, ConfigSource.SHIPPED),
        )
    for profile in overlay.profiles if overlay is not None else ():
        base = merged.get(profile.id) or MergedProfile(profile.id, False, (), (), (), ())
        merged[profile.id] = MergedProfile(
            id=base.id, floor=base.floor,
            names=_add(base.names, profile.names, ConfigSource.OVERLAY),
            prefixes=_add(base.prefixes, profile.prefixes, ConfigSource.OVERLAY),
            tools=_add(base.tools, profile.tools, ConfigSource.OVERLAY),
            lanes=_add(base.lanes, profile.lanes, ConfigSource.OVERLAY),
        )
    return tuple(merged.values())


# ---- merge and cache ---------------------------------------------------------

_CACHE: tuple[tuple, ModelToolConfig] | None = None


def identity_key() -> tuple | None:
    """Identity of both files, for callers that cache what they derived from them."""
    overlay, _reason = _safe_overlay_path()
    overlay_id = _identity(overlay) if overlay is not None else None
    shipped_id = _identity(SHIPPED_PATH)
    if overlay_id is None or shipped_id is None:
        return None
    return (shipped_id, overlay_id)


def load_shipped_document() -> tuple[_Document | None, list[ConfigIssue]]:
    """The validated shipped document (None when unusable), for the console editor."""
    _floor, document, issues = _load_shipped(SHIPPED_PATH)
    return document, issues


def _build() -> ModelToolConfig:
    floor, shipped, issues = _load_shipped(SHIPPED_PATH)
    path, reason = _safe_overlay_path()
    overlay = _load_overlay(path, reason, shipped)
    document = overlay.document
    names: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    if document is not None:
        names = tuple(dict.fromkeys(name for profile in document.profiles for name in profile.names))
        prefixes = tuple(dict.fromkeys(item for profile in document.profiles for item in profile.prefixes))
        if document.undetermined and floor.available:
            floor = ToolLaneFloor(
                names=floor.names, prefixes=floor.prefixes,
                undetermined_served=floor.undetermined_served | document.undetermined,
            )
    globs: tuple[Entry, ...] = ()
    if shipped is not None:
        globs = _sourced(shipped.globs, ConfigSource.SHIPPED)
        if document is not None:
            globs = _add(globs, document.globs, ConfigSource.OVERLAY)
    return ModelToolConfig(
        floor=floor,
        overlay_names=names,
        overlay_prefixes=prefixes,
        readable_shipped=floor.available,
        readable_overlay=overlay.readable,
        overlay_present=overlay.present,
        shipped_path=str(SHIPPED_PATH),
        overlay_path=str(path) if path is not None else "",
        issues=tuple(issues) + overlay.issues,
        profiles=_merge_profiles(shipped, document) if shipped is not None else (),
        tool_ceiling=shipped.ceiling if shipped is not None else (),
        secret_path_globs=globs,
    )


def load() -> ModelToolConfig:
    """The merged config, read again only when either file changed.

    Never raises.  The key is taken before the read and checked after it, so a
    write racing the read is never cached under a newer key; a result with any
    issue is not cached.  Thread-safe: the cache is one tuple replaced
    atomically, and a racing reader at worst reads the files itself.
    """
    global _CACHE
    key = identity_key()
    cached = _CACHE
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    config = _build()
    if key is not None and not config.issues and identity_key() == key:
        _CACHE = (key, config)
    return config


def clear_cache() -> None:
    """Forget the cached config (the next ``load`` reads both files)."""
    global _CACHE
    _CACHE = None


def shipped_floor() -> ToolLaneFloor:
    """The shipped floor alone (what callers that ignore the overlay pass in)."""
    return load().floor


def describe(config: ModelToolConfig | None = None) -> list[str]:
    """Status lines for the two files, errors included."""
    config = load() if config is None else config
    lines = ["model tool policy (shipped): %s%s" % (
        config.shipped_path,
        "" if config.readable_shipped else " ERROR: every tool-using lane is refused until it is restored",
    )]
    if not config.overlay_present and config.readable_overlay:
        overlay_state = "(absent: optional operator overlay)"
    elif config.readable_overlay:
        overlay_state = "names=%s prefixes=%s" % (
            ", ".join(config.overlay_names) or "-", ", ".join(config.overlay_prefixes) or "-",
        )
    else:
        overlay_state = "ERROR: ignored; caller-named models are refused in tool lanes until it is fixed"
    lines.append("model tool policy (overlay): %s %s" % (config.overlay_path or "(unknown)", overlay_state))
    lines.extend("  ERROR: %s" % error for error in config.errors)
    return lines

