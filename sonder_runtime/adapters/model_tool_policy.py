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
overlay that exists but is unreadable or invalid (or cannot be inspected, or
whose location cannot be worked out) refuses every model in every tool-using
lane the same way (``ModelToolConfig.overlay_problem``): it may be the only
thing that restricted a model, and an unusable file cannot say which.  The
error is reported and the file is never rewritten.  A missing overlay is
normal.

The shipped file is also pinned.  The first valid read of a path records the
sha256 of its bytes (the module pins ``SHIPPED_PATH`` when it is imported); a
later read that finds other bytes makes the floor unavailable, which refuses
every tool-using lane exactly like a missing file, until the process restarts
or the console operator runs ``/runtime tools reload`` (:func:`repin_shipped`),
which pins what is on disk and reports both hashes.  This holds the floor
against a write that reaches the file by a route nobody guarded.  Putting the
pinned bytes back makes the floor available again without a reload; a rewrite
with identical bytes is not a change.  ``runtime_source_update`` and a
reviewed self-modification deploy change the shipped file legitimately, so
after either one the floor stays unavailable until a restart or a reload.

The pure rule is ``sonder_runtime.domain.tool_lane_models``; this module only
supplies its inputs.  Results are cached against each file's identity (path,
modification time, size, inode); a failed read is reused for the same
identity for at most ``FAILED_READ_TTL_SECONDS`` and refuses while it is.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
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
# How long a failed read is reused for the same file identity.  Every model
# call asks, so a bad file would otherwise be parsed again on each one; the
# bound keeps a transient read failure (a file held open by another program)
# from outliving the moment.
FAILED_READ_TTL_SECONDS = 1.0

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


class ShippedPin(str, Enum):
    """How the shipped file compares with the hash pinned for its path."""

    PINNED = "pinned"  # read, valid, and the bytes are the pinned ones
    CHANGED = "changed"  # the bytes differ from the pinned ones: the floor is unavailable
    UNREAD = "unread"  # missing, unreadable or invalid: nothing was pinned from this read


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
    shipped_pin: ShippedPin = ShippedPin.UNREAD
    shipped_sha256: str = ""

    def profile(self, profile_id: str) -> MergedProfile | None:
        return next((item for item in self.profiles if item.id == profile_id), None)

    @property
    def secret_globs(self) -> tuple[str, ...]:
        return tuple(entry.value for entry in self.secret_path_globs)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(issue.text() for issue in self.issues)

    @property
    def overlay_problem(self) -> str:
        """Why the overlay cannot be used, naming it; ``""`` when it is usable or absent.

        Non-empty refuses every model in every tool-using lane
        (``ToolLaneDenyList.overlay_problem``).
        """
        if self.readable_overlay:
            return ""
        reasons = [issue.message for issue in self.issues if issue.source is ConfigSource.OVERLAY]
        return "%s: %s" % (
            self.overlay_path or "(unknown)", "; ".join(reasons) or "the file cannot be used",
        )

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


def _read_json_hashed(path: Path) -> tuple[Any, str, str]:
    """``(decoded document, "", sha256 of the bytes read)`` or ``(None, reason, "")``; never raises."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, "the file is larger than %d bytes" % MAX_FILE_BYTES, ""
        data = path.read_bytes()
        return json.loads(data.decode("utf-8")), "", hashlib.sha256(data).hexdigest()
    except FileNotFoundError:
        return None, "the file does not exist", ""
    except (OSError, ValueError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None, "%s: %s" % (type(exc).__name__, exc), ""


def _read_json(path: Path) -> tuple[Any, str]:
    """``(decoded document, "")`` or ``(None, reason)``; never raises."""
    raw, reason, _digest = _read_json_hashed(path)
    return raw, reason


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


# ---- the startup pin ---------------------------------------------------------

@dataclass(frozen=True)
class _Pin:
    """What was pinned for one path: the sha256 and what the file held (so a reload can show what changed)."""

    sha256: str
    names: frozenset[str]
    prefixes: tuple[str, ...]
    ceiling: tuple[str, ...] = ()
    globs: tuple[str, ...] = ()
    undetermined: frozenset[str] = frozenset()
    profile_tools: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _pin_of(digest: str, document: _Document) -> _Pin:
    names, prefixes = _floor_of(document)
    return _Pin(
        digest, names, prefixes, ceiling=document.ceiling, globs=document.globs,
        undetermined=document.undetermined,
        profile_tools=tuple((profile.id, profile.tools) for profile in document.profiles),
    )


def _pin_changes(old: _Pin | None, new: _Pin) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(label, values)`` for every floor name, prefix, ceiling tool, secret glob, undetermined name and
    profile tool that differs between ``old`` (nothing, when there was no pin) and ``new``."""
    before = old if old is not None else _Pin("", frozenset(), ())
    pairs: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
        ("floor names", tuple(sorted(before.names)), tuple(sorted(new.names))),
        ("floor prefixes", before.prefixes, new.prefixes),
        ("restricted tool ceiling", before.ceiling, new.ceiling),
        ("restricted secret path globs", before.globs, new.globs),
        ("undetermined served models", tuple(sorted(before.undetermined)), tuple(sorted(new.undetermined))),
    ]
    old_tools, new_tools = dict(before.profile_tools), dict(new.profile_tools)
    for profile_id in dict.fromkeys((*new_tools, *old_tools)):
        pairs.append(("profile %s tools" % profile_id, old_tools.get(profile_id, ()), new_tools.get(profile_id, ())))
    changes: list[tuple[str, tuple[str, ...]]] = []
    for label, was, now in pairs:
        changes.append(("%s added" % label, tuple(item for item in now if item not in was)))
        changes.append(("%s removed" % label, tuple(item for item in was if item not in now)))
    return tuple((label, values) for label, values in changes if values)


# normcased path -> the pin made at its first valid read.  Guarded by
# ``_PIN_LOCK``; an entry is only ever added, except by ``repin_shipped``.
_PINNED: dict[str, _Pin] = {}
_PIN_LOCK = threading.Lock()


def _pin_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _pin_state(path: Path, made: _Pin) -> tuple[ShippedPin, _Pin]:
    """Pin ``made`` for ``path`` when it has no pin yet; ``(state, the pin in force)``."""
    with _PIN_LOCK:
        pinned = _PINNED.setdefault(_pin_key(path), made)
    return (ShippedPin.PINNED if pinned.sha256 == made.sha256 else ShippedPin.CHANGED), pinned


def _floor_of(document: _Document) -> tuple[frozenset[str], tuple[str, ...]]:
    names = frozenset(name for profile in document.profiles for name in profile.names)
    prefixes = tuple(dict.fromkeys(item for profile in document.profiles for item in profile.prefixes))
    return names, prefixes


@dataclass(frozen=True)
class _Shipped:
    """One read of the shipped file."""

    floor: ToolLaneFloor
    document: _Document | None
    issues: tuple[ConfigIssue, ...]
    pin: ShippedPin
    sha256: str


def _validated_document(raw: Any, reason: str) -> tuple[_Document | None, list[str]]:
    """The parsed shipped document (None when it could not be read) and every problem with it."""
    if reason:
        return None, [reason]
    document = _parse_document(raw)
    problems = list(document.problems)
    if not any(profile.floor for profile in document.profiles):
        problems.append("no profile is marked \"floor\": true")
    return document, problems


def _load_shipped(path: Path) -> _Shipped:
    raw, reason, digest = _read_json_hashed(path)
    document, problems = _validated_document(raw, reason)
    pin = ShippedPin.UNREAD
    if document is not None and not problems:
        pin, pinned = _pin_state(path, _pin_of(digest, document))
        if pin is ShippedPin.CHANGED:
            problems.append(
                "the file changed since it was pinned at startup (sha256 %s, now %s); restart Sonder "
                "or run /runtime tools reload at the console" % (pinned.sha256, digest)
            )
    if problems or document is None:
        issues = tuple(ConfigIssue(ConfigSource.SHIPPED, str(path), problem) for problem in problems)
        floor = ToolLaneFloor.unavailable("%s: %s" % (path, "; ".join(problems)))
        return _Shipped(floor, None, issues, pin, digest)
    names, prefixes = _floor_of(document)
    floor = ToolLaneFloor(names=names, prefixes=prefixes, undetermined_served=document.undetermined)
    return _Shipped(floor, document, (), pin, digest)


@dataclass(frozen=True)
class PinReport:
    """What :func:`repin_shipped` did: the hash pinned before and now, and whether the file is usable."""

    path: str
    old_sha256: str
    new_sha256: str
    available: bool
    problems: tuple[str, ...]
    # ``(label, values)``, e.g. ``("restricted tool ceiling removed", ("context_pack",))``: every
    # floor name, prefix, ceiling tool, secret glob, undetermined name and profile tool that differs.
    changes: tuple[tuple[str, tuple[str, ...]], ...] = ()


def repin_shipped() -> PinReport:
    """Pin whatever valid shipped file is on disk now (the console operator's ``reload``).

    An unreadable or invalid file is not pinned: the old pin stays and the
    report says why.  The cached config is dropped either way.
    """
    path = SHIPPED_PATH
    raw, reason, digest = _read_json_hashed(path)
    document, problems = _validated_document(raw, reason)
    if document is None or problems:
        return PinReport(str(path), _pinned_sha256(path), "", False, tuple(problems))
    new = _pin_of(digest, document)
    with _PIN_LOCK:
        old = _PINNED.get(_pin_key(path))
        _PINNED[_pin_key(path)] = new
    clear_cache()
    return PinReport(
        str(path), old.sha256 if old is not None else "", digest, True, (), changes=_pin_changes(old, new),
    )


def _pinned_sha256(path: Path) -> str:
    with _PIN_LOCK:
        pinned = _PINNED.get(_pin_key(path))
    return pinned.sha256 if pinned is not None else ""


def _pin_at_import() -> None:
    """Pin the shipped file as the process starts; a file that cannot be read pins at its first valid read."""
    try:
        _load_shipped(SHIPPED_PATH)
    except Exception:  # noqa: BLE001 - importing this module must never fail
        pass


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

# ``(key, config, expiry)``: expiry is None for a clean read, else the
# ``time.monotonic()`` after which a failed read is read again.
_CACHE: tuple[tuple, ModelToolConfig, float | None] | None = None


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
    shipped = _load_shipped(SHIPPED_PATH)
    return shipped.document, list(shipped.issues)


def _build() -> ModelToolConfig:
    loaded = _load_shipped(SHIPPED_PATH)
    floor, shipped, issues = loaded.floor, loaded.document, list(loaded.issues)
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
        shipped_pin=loaded.pin,
        shipped_sha256=loaded.sha256,
    )


def load() -> ModelToolConfig:
    """The merged config, read again only when either file changed.

    Never raises.  The key is taken before the read and checked after it, so a
    write racing the read is never cached under a newer key.  A result with
    any issue is reused for the same key for at most
    ``FAILED_READ_TTL_SECONDS``.  Thread-safe: the cache is one tuple replaced
    atomically, and a racing reader at worst reads the files itself.
    """
    global _CACHE
    key = identity_key()
    cached = _CACHE
    if key is not None and cached is not None and cached[0] == key and (
        cached[2] is None or time.monotonic() < cached[2]
    ):
        return cached[1]
    config = _build()
    if key is not None and identity_key() == key:
        expiry = None if not config.issues else time.monotonic() + FAILED_READ_TTL_SECONDS
        _CACHE = (key, config, expiry)
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
    if config.shipped_sha256:
        lines.append("  shipped sha256 %s (%s)" % (config.shipped_sha256, config.shipped_pin.value))
    if not config.overlay_present and config.readable_overlay:
        overlay_state = "(absent: optional operator overlay)"
    elif config.readable_overlay:
        overlay_state = "names=%s prefixes=%s" % (
            ", ".join(config.overlay_names) or "-", ", ".join(config.overlay_prefixes) or "-",
        )
    else:
        overlay_state = "ERROR: every tool-using lane refuses every model until it is fixed or removed"
    lines.append("model tool policy (overlay): %s %s" % (config.overlay_path or "(unknown)", overlay_state))
    lines.extend("  ERROR: %s" % error for error in config.errors)
    return lines


_pin_at_import()
