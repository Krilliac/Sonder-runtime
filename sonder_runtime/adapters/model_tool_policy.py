"""Load, validate and merge the model tool policy files.

Two JSON files describe which models may drive tool-using lanes:

* the shipped defaults, ``adapters/data/model_tool_policy.json``, versioned
  with the code (the same pattern as ``semantic_tier_head.json``); and
* an optional operator overlay, ``<state home>/model_tool_policy.json``
  (``SONDER_MODEL_TOOL_POLICY`` names another path).  It is a different file
  from ``runtime_policy.json`` so that ``runtime_policy_update`` can never
  touch it.  This module only reads it: nothing here writes or rewrites
  either file, so a bad overlay is left exactly as the operator wrote it.

Merge rules.  A shipped profile marked ``"floor": true`` can neither be
removed nor narrowed: its names and prefixes are always in force, whatever
the overlay says.  The overlay may add names and prefixes (to a new profile or
to a floor profile) and may add served-model ids that name no model; it may
not set ``floor`` and may not give a floor profile tools.  In phase 1a a
profile's ``tools`` are validated but not yet enforced: every model the merged
profiles match stays chat-by-name only, which is the safe reading.

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

_DOCUMENT_KEYS = frozenset({"version", "model_profiles", "undetermined_served_models"})
_PROFILE_KEYS = frozenset({"id", "floor", "match", "tools", "lanes"})
_MATCH_KEYS = frozenset({"names", "prefixes"})
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigSource(str, Enum):
    SHIPPED = "shipped"
    OVERLAY = "overlay"


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


def _parse_profile(raw: Any, index: int, problems: list[str]) -> Profile | None:
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
    if not names and not prefixes:
        problems.append("%s matches no model" % where)
    return Profile(
        id=profile_id,
        floor=floor,
        names=tuple(dict.fromkeys(filter(None, (normalize_name(name) for name in names)))),
        prefixes=tuple(dict.fromkeys(filter(None, (normalize_name(item) for item in prefixes)))),
        tools=tuple(dict.fromkeys(tools)),
        lanes=tuple(dict.fromkeys(lanes)),
    )


def _parse_document(raw: Any) -> tuple[list[Profile], frozenset[str], list[str]]:
    """``(profiles, undetermined served ids, problems)`` of one decoded document."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return [], frozenset(), ["the file must hold one JSON object"]
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
        profile = _parse_profile(entry, index, problems)
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
    return profiles, frozenset(undetermined), problems


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


def _load_shipped(path: Path) -> tuple[ToolLaneFloor, list[Profile], list[ConfigIssue]]:
    raw, reason = _read_json(path)
    problems: list[str] = [reason] if reason else []
    profiles: list[Profile] = []
    undetermined: frozenset[str] = frozenset()
    if not reason:
        profiles, undetermined, problems = _parse_document(raw)
        if not any(profile.floor for profile in profiles):
            problems.append("no profile is marked \"floor\": true")
    if problems:
        issues = [ConfigIssue(ConfigSource.SHIPPED, str(path), problem) for problem in problems]
        return ToolLaneFloor.unavailable("%s: %s" % (path, "; ".join(problems))), [], issues
    names = frozenset(name for profile in profiles for name in profile.names)
    prefixes = tuple(dict.fromkeys(item for profile in profiles for item in profile.prefixes))
    return ToolLaneFloor(names=names, prefixes=prefixes, undetermined_served=undetermined), profiles, []


def _load_overlay(
    path: Path | None, reason: str, shipped: list[Profile],
) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str], bool, bool, list[ConfigIssue]]:
    """``(names, prefixes, extra undetermined ids, readable, present, issues)`` of the overlay."""
    if path is None:
        return (), (), frozenset(), False, False, [ConfigIssue(ConfigSource.OVERLAY, "(unknown)", reason)]
    identity = _identity(path)
    if identity is None:
        return (), (), frozenset(), False, True, [
            ConfigIssue(ConfigSource.OVERLAY, str(path), "the file cannot be inspected"),
        ]
    if identity[1:] == (None,):
        return (), (), frozenset(), True, False, []
    raw, reason = _read_json(path)
    problems: list[str] = [reason] if reason else []
    profiles: list[Profile] = []
    undetermined: frozenset[str] = frozenset()
    if not reason:
        profiles, undetermined, problems = _parse_document(raw)
        floors = {profile.id for profile in shipped if profile.floor}
        for profile in profiles:
            if profile.floor:
                problems.append("profile %r: only the shipped file may mark a profile as a floor" % profile.id)
            if profile.id in floors and profile.tools:
                problems.append("profile %r: a floor profile cannot be given tools" % profile.id)
    if problems:
        return (), (), frozenset(), False, True, [
            ConfigIssue(ConfigSource.OVERLAY, str(path), problem) for problem in problems
        ]
    names = tuple(dict.fromkeys(name for profile in profiles for name in profile.names))
    prefixes = tuple(dict.fromkeys(item for profile in profiles for item in profile.prefixes))
    return names, prefixes, undetermined, True, True, []


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


def _build() -> ModelToolConfig:
    floor, shipped_profiles, issues = _load_shipped(SHIPPED_PATH)
    overlay, reason = _safe_overlay_path()
    names, prefixes, undetermined, readable, present, overlay_issues = _load_overlay(
        overlay, reason, shipped_profiles,
    )
    if undetermined and floor.available:
        floor = ToolLaneFloor(
            names=floor.names, prefixes=floor.prefixes,
            undetermined_served=floor.undetermined_served | undetermined,
        )
    return ModelToolConfig(
        floor=floor,
        overlay_names=names,
        overlay_prefixes=prefixes,
        readable_shipped=floor.available,
        readable_overlay=readable,
        overlay_present=present,
        shipped_path=str(SHIPPED_PATH),
        overlay_path=str(overlay) if overlay is not None else "",
        issues=tuple(issues) + tuple(overlay_issues),
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

