"""Pure decisions for keeping Sonder's Ollama cloud models current.

Everything here is a function of its arguments: no I/O, no clock, no process
environment (callers pass an environment mapping and ``now``).  The adapters
(``adapters/cloud_model_refresh.py`` and its two clients) do the reading,
probing and writing; this module decides what those reads mean:

* which names Sonder knows and in which order to probe them;
* what an Ollama ``/api/show`` answer says about a model (the authoritative
  retirement signal is an error of the form ``<name> was retired on <date>.
  Try <suggestion>.``);
* which retired names to add to the overlay, as a union with the shipped
  list, because a list in the overlay replaces the whole shipped key;
* whether a tier whose effective model is retired is rebound, and to which
  operator-approved ladder entry (never a model outside the ladder and never
  an environment binding);
* the bounded refresh state document.

All values are immutable or freshly built; nothing is shared between calls,
so every function is safe to call from any thread.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sonder_runtime.domain.runtime_policy.rules import _MODEL_RE, is_cloud_name

SCHEMA_VERSION = 1
CLOUD_TIERS = ("code", "general")
MAX_LADDER = 16
MAX_PROBES = 64
MAX_RETIRED = 256
MAX_STATE_MODELS = 512
MAX_PULLS = 4
MAX_FAMILIES = 60
MAX_TAGS_PER_FAMILY = 50
MAX_PENDING = len(CLOUD_TIERS)
# Highest priority first: a name's priority is that of its best source.
PRIORITY = ("env", "default", "overlay", "legacy", "ladder", "pulled", "catalog")
SOURCES = frozenset(PRIORITY)
# Names in these sources are re-probed even when already retired, so a binding
# or a ladder entry is always confirmed against the provider.
RECONFIRMED_SOURCES = frozenset({"env", "default", "overlay", "ladder"})
CATALOG_REPROBE_SECONDS = 7 * 24 * 3600
MAX_MESSAGE_CHARS = 512
MAX_TEXT_CHARS = 64
MAX_CAPABILITIES = 16
MAX_HTML_CHARS = 2 * 1024 * 1024

PREFERENCE_KEYS = frozenset(
    {"version", "code", "general", "auto_pull_stubs", "auto_refresh", "catalog"}
)
STATE_KEYS = frozenset(
    {
        "version",
        "last_success_at",
        "last_refresh",
        "models",
        "catalog",
        "pending_choices",
        "overlay_pins",
    }
)
MODEL_STATUSES = frozenset({"live", "retired", "not_found", "error", "unprobed"})
# Ladder entry states reported per tier; ``selected`` is the rebind target and
# ``live`` an eligible entry that was not needed.
LADDER_STATES = frozenset(
    {"retired", "not_live", "not_pulled", "error", "unprobed", "invalid", "live", "selected"}
)
ACTIONS = ("keep", "rebind", "needs_operator_choice")

_NAME_BODY = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}"
_RETIREMENT_RE = re.compile(
    r"(?P<name>" + _NAME_BODY + r") was retired on "
    r"(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\.?"
    r"(?:\s+Try (?P<suggestion>\S+?)\.?)?"
    # The live API appends a request reference, e.g. ``(ref: 59bcf5d7-...)``;
    # accept exactly that bounded token and nothing else after the notice.
    r"(?:\s*\(ref:\s*[A-Za-z0-9._-]{1,64}\))?\s*"
)
_ERROR_PREFIX_RE = re.compile(r"^\s*Error:\s*")
_FAMILY_HREF_RE = re.compile(r'href="/library/([a-z0-9][a-z0-9._-]{0,63})"')
_FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


@dataclass(frozen=True)
class Retirement:
    """Parsed provider retirement notice; owned strings, immutable."""

    provider_name: str
    retired_on: str
    suggestion: str | None

    def evidence(self) -> dict[str, str | None]:
        return {
            "provider_name": self.provider_name,
            "retired_on": self.retired_on,
            "suggestion": self.suggestion,
        }


def valid_model_name(name: object) -> bool:
    return isinstance(name, str) and _MODEL_RE.fullmatch(name) is not None


def is_cloud_stub_name(name: str) -> bool:
    """Strict ``<family>:cloud`` / ``<family>:<tag>-cloud`` grammar for pulls and rebinds."""
    if not valid_model_name(name):
        return False
    family, separator, tag = name.rpartition(":")
    if not separator or not family:
        return False
    tag = tag.lower()
    return tag == "cloud" or (tag.endswith("-cloud") and len(tag) > len("-cloud"))


def parse_retirement(message: str) -> Retirement | None:
    """Parse ``[Error: ]<name> was retired on <Month D, YYYY>.[ Try <name>.][ (ref: <id>)]``."""
    if not isinstance(message, str):
        return None
    text = _ERROR_PREFIX_RE.sub("", message[:MAX_MESSAGE_CHARS]).strip()
    match = _RETIREMENT_RE.fullmatch(text)
    if match is None:
        return None
    suggestion = match.group("suggestion")
    return Retirement(
        provider_name=match.group("name"),
        retired_on=match.group("date"),
        suggestion=suggestion if valid_model_name(suggestion) else None,
    )


class ProbeStatus(str, Enum):
    LIVE = "live"
    RETIRED = "retired"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    """One ``/api/show`` verdict.  ``detail`` is a bounded reason code, never response text."""

    model: str
    status: ProbeStatus
    retirement: Retirement | None = None
    parameters: str | None = None
    context_length: int | None = None
    capabilities: tuple[str, ...] = ()
    family: str | None = None
    detail: str = ""


def _short_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:MAX_TEXT_CHARS] if text else None


def show_metadata(
    payload: Mapping[str, Any],
) -> tuple[str | None, int | None, tuple[str, ...], str | None]:
    """``(parameter_size, context_length, capabilities, family)`` from a show body."""
    details = payload.get("details")
    details = details if isinstance(details, Mapping) else {}
    info = payload.get("model_info")
    context: int | None = None
    if isinstance(info, Mapping):
        for key, value in info.items():
            if (
                isinstance(key, str)
                and key.endswith(".context_length")
                and type(value) is int
                and value > 0
                and (context is None or value > context)
            ):
                context = value
    raw_capabilities = payload.get("capabilities")
    capabilities: list[str] = []
    if isinstance(raw_capabilities, list):
        for item in raw_capabilities:
            text = _short_text(item)
            if text is not None and text not in capabilities:
                capabilities.append(text)
            if len(capabilities) >= MAX_CAPABILITIES:
                break
    return (
        _short_text(details.get("parameter_size")),
        context,
        tuple(capabilities),
        _short_text(details.get("family")),
    )


def classify_show(model: str, http_status: int, payload: object) -> ProbeResult:
    """Classify one ``/api/show`` answer; only a parsed retirement is a conclusion."""
    code = http_status if type(http_status) is int and 100 <= http_status <= 599 else 0
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if code == 200 and isinstance(payload, Mapping) and "error" not in payload:
        parameters, context, capabilities, family = show_metadata(payload)
        return ProbeResult(
            model, ProbeStatus.LIVE, None, parameters, context, capabilities, family, "ok"
        )
    if isinstance(error, str):
        retirement = parse_retirement(error)
        if retirement is not None:
            return ProbeResult(model, ProbeStatus.RETIRED, retirement, detail="retired")
        if code == 404 or "not found" in error[:MAX_MESSAGE_CHARS].lower():
            return ProbeResult(model, ProbeStatus.NOT_FOUND, detail="not_found")
        return ProbeResult(model, ProbeStatus.ERROR, detail="error_%d" % code)
    if code == 404:
        return ProbeResult(model, ProbeStatus.NOT_FOUND, detail="not_found")
    return ProbeResult(
        model, ProbeStatus.ERROR, detail="malformed_body" if code == 200 else "http_%d" % code
    )


def is_pulled_cloud_row(row: Mapping[str, Any]) -> bool:
    """An ``/api/tags`` row is a cloud stub by name or by its ``remote_host``."""
    name = row.get("name") or row.get("model")
    return valid_model_name(name) and (is_cloud_name(str(name)) or bool(row.get("remote_host")))


@dataclass(frozen=True)
class Preferences:
    """Operator-approved ladders and switches; immutable, from cloud_model_preferences.json."""

    code: tuple[str, ...] = ()
    general: tuple[str, ...] = ()
    auto_pull_stubs: bool = False
    auto_refresh: bool = True
    catalog: bool = True


def _ladder_entries(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % key)
    if len(value) > MAX_LADDER:
        raise ValueError("%s holds more than %d entries" % (key, MAX_LADDER))
    seen: set[str] = set()
    entries: list[str] = []
    for item in value:
        if not isinstance(item, str) or not is_cloud_stub_name(item):
            raise ValueError("%s entries must be cloud stub names (<family>:cloud)" % key)
        folded = item.casefold()
        if folded not in seen:
            seen.add(folded)
            entries.append(item)
    return tuple(entries)


def parse_preferences(document: object) -> Preferences:
    """Validate the whole preferences document; any problem raises ValueError."""
    if not isinstance(document, dict):
        raise ValueError("preferences must be a JSON object")
    unknown = set(document) - PREFERENCE_KEYS
    if unknown:
        raise ValueError("unknown preference key(s): " + ", ".join(sorted(map(str, unknown))))
    if "version" in document and (
        type(document["version"]) is not int or document["version"] != SCHEMA_VERSION
    ):
        raise ValueError("preferences version must be %d" % SCHEMA_VERSION)
    flags: dict[str, bool] = {}
    for key, default in (("auto_pull_stubs", False), ("auto_refresh", True), ("catalog", True)):
        value = document.get(key, default)
        if type(value) is not bool:
            raise ValueError("%s must be true or false" % key)
        flags[key] = value
    return Preferences(
        code=_ladder_entries(document.get("code", []), "code"),
        general=_ladder_entries(document.get("general", []), "general"),
        **flags,
    )


def ladder(prefs: Preferences, tier: str) -> tuple[str, ...]:
    if tier == "code":
        return prefs.code
    if tier == "general":
        return prefs.general
    raise ValueError("unknown cloud tier %r" % tier)


def parse_catalog_families(html: str) -> tuple[str, ...]:
    """Family slugs linked as ``href="/library/<name>"``, first-seen order, capped."""
    families: list[str] = []
    for match in _FAMILY_HREF_RE.finditer(html[:MAX_HTML_CHARS]):
        family = match.group(1)
        if family not in families:
            families.append(family)
            if len(families) >= MAX_FAMILIES:
                break
    return tuple(families)


def parse_catalog_tags(family: str, html: str) -> tuple[str, ...]:
    """``<family>:<tag>`` cloud stub names on one family's tags page, capped."""
    if not isinstance(family, str) or _FAMILY_RE.fullmatch(family) is None:
        return ()
    pattern = re.compile(
        r"(?<![A-Za-z0-9._-])" + re.escape(family) + r":([A-Za-z0-9._-]{1,64})"
    )
    tags: list[str] = []
    for match in pattern.finditer(html[:MAX_HTML_CHARS]):
        name = "%s:%s" % (family, match.group(1).rstrip("._-"))
        if is_cloud_stub_name(name) and name not in tags:
            tags.append(name)
            if len(tags) >= MAX_TAGS_PER_FAMILY:
                break
    return tuple(tags)


@dataclass(frozen=True)
class KnownModel:
    """A cloud model name Sonder knows, the sources naming it, and whether it fits the probe budget."""

    name: str
    sources: frozenset[str]
    probe: bool = True

    def priority(self) -> int:
        return min(PRIORITY.index(source) for source in self.sources)


def parse_timestamp(value: object) -> float | None:
    """Seconds since the epoch for a ``YYYY-MM-DDTHH:MM:SSZ`` string, else None."""
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def iso_timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _catalog_due(name: str, state_models: Mapping[str, Any], now: float) -> bool:
    entry = state_models.get(name)
    if not isinstance(entry, Mapping):
        return True
    probed = parse_timestamp(entry.get("probed_at"))
    return probed is None or now - probed >= CATALOG_REPROBE_SECONDS


def collect_known_models(
    *,
    shipped: Mapping[str, Any],
    effective: Mapping[str, Any],
    env: Mapping[str, str],
    pulled: Sequence[Mapping[str, Any]],
    prefs: Preferences | None,
    catalog_tags: Iterable[str],
    state_models: Mapping[str, Any],
    now: float,
    max_probes: int = MAX_PROBES,
) -> tuple[KnownModel, ...]:
    """Every cloud name Sonder knows, best priority first; entries past ``max_probes`` have ``probe=False``."""
    candidates: list[tuple[str, str]] = []
    for variable in ("SONDER_CLOUD_CODE", "SONDER_CLOUD_GENERAL"):
        candidates.append((str(env.get(variable, "") or "").strip(), "env"))
    local_code = str(env.get("SONDER_CODE", "") or "").strip()
    if is_cloud_name(local_code):
        candidates.append((local_code, "env"))
    shipped_defaults = shipped.get("default_cloud_models") or {}
    effective_defaults = effective.get("default_cloud_models") or {}
    for tier in CLOUD_TIERS:
        candidates.append((str(shipped_defaults.get(tier, "")), "default"))
        value = str(effective_defaults.get(tier, ""))
        candidates.append((value, "default"))
        if value != shipped_defaults.get(tier):
            candidates.append((value, "overlay"))
    candidates.append((str(effective.get("legacy_cloud_general_model", "")), "legacy"))
    if prefs is not None:
        candidates.extend((name, "ladder") for tier in CLOUD_TIERS for name in ladder(prefs, tier))
    pulled_names: list[str] = []
    for row in pulled:
        if isinstance(row, Mapping) and is_pulled_cloud_row(row):
            name = str(row.get("name") or row.get("model"))
            pulled_names.append(name)
            candidates.append((name, "pulled"))
    for name in catalog_tags:
        if isinstance(name, str) and _catalog_due(name, state_models, now):
            candidates.append((name, "catalog"))
    pulled_cf = {name.casefold() for name in pulled_names}
    retired_cf = {str(name).casefold() for name in effective.get("retired_cloud_models") or ()}
    order: list[str] = []
    spelling: dict[str, str] = {}
    sources: dict[str, set[str]] = {}
    for name, source in candidates:
        if not valid_model_name(name):
            continue
        folded = name.casefold()
        if not is_cloud_name(name) and folded not in pulled_cf:
            continue
        if folded not in sources:
            order.append(folded)
            spelling[folded] = name
            sources[folded] = set()
        sources[folded].add(source)
    kept = [
        KnownModel(spelling[folded], frozenset(sources[folded]))
        for folded in order
        if folded not in retired_cf or sources[folded] & RECONFIRMED_SOURCES
    ]
    ranked = sorted(kept, key=KnownModel.priority)
    budget = max(0, max_probes)
    return tuple(
        model if index < budget else KnownModel(model.name, model.sources, probe=False)
        for index, model in enumerate(ranked)
    )


def retired_union(
    shipped: Sequence[str], overlay: Sequence[str] | None, new: Sequence[str]
) -> list[str]:
    """Shipped, then overlay, then new names; order-stable, case-insensitively unique."""
    seen: set[str] = set()
    union: list[str] = []
    for group in (shipped, overlay or (), new):
        for name in group:
            folded = str(name).casefold()
            if folded not in seen:
                seen.add(folded)
                union.append(str(name))
    return union


def retired_heal_needed(shipped: Sequence[str], overlay: Sequence[str] | None) -> bool:
    """True when an overlay retired list hides shipped entries (lists replace whole keys)."""
    if overlay is None:
        return False
    present = {str(name).casefold() for name in overlay}
    return any(str(name).casefold() not in present for name in shipped)


@dataclass(frozen=True)
class LadderEntry:
    model: str
    state: str


@dataclass(frozen=True)
class TierDecision:
    """What the refresh does for one cloud tier; ``new_model`` is set only for ``rebind``."""

    tier: str
    effective_model: str
    effective_source: str
    action: str
    new_model: str | None
    ladder: tuple[LadderEntry, ...]
    suggestion: str | None
    note: str


def _probe_lookup(probes: Mapping[str, ProbeResult]) -> dict[str, ProbeResult]:
    return {name.casefold(): result for name, result in probes.items()}


def _entry_state(
    name: str, retired_cf: frozenset[str], probes: Mapping[str, ProbeResult], pulled_cf: frozenset[str]
) -> str:
    if not isinstance(name, str) or not is_cloud_stub_name(name):
        return "invalid"
    folded = name.casefold()
    if folded in retired_cf:
        return "retired"
    probe = probes.get(folded)
    if probe is None:
        return "unprobed"
    if probe.status is ProbeStatus.RETIRED:
        return "retired"
    if probe.status is ProbeStatus.ERROR:
        return "error"
    if probe.status is ProbeStatus.NOT_FOUND:
        return "not_live"
    return "live" if folded in pulled_cf else "not_pulled"


def decide_tier(
    tier: str,
    *,
    env_value: str,
    default_model: str,
    retired_cf: frozenset[str],
    probes: Mapping[str, ProbeResult],
    pulled_cf: frozenset[str],
    ladder: Sequence[str],
    suggestion: str | None,
) -> TierDecision:
    """Keep a live binding; rebind a retired default to the first eligible ladder entry.

    Eligible means: a cloud stub name, not retired, LIVE in this run's probes,
    and pulled (or pulled during this run).  The provider's suggestion is only
    reported; it is chosen only when it is itself the first eligible entry.
    """
    lookup = _probe_lookup(probes)
    notes: list[str] = []
    env_text = str(env_value or "").strip()
    if env_text and env_text.casefold() not in retired_cf:
        effective, source = env_text, "env"
    else:
        if env_text:
            notes.append(
                "env SONDER_CLOUD_%s names retired %s; not changed" % (tier.upper(), env_text)
            )
        effective, source = default_model, "default"
    retired = effective.casefold() in retired_cf
    states = [_entry_state(name, retired_cf, lookup, pulled_cf) for name in ladder]
    selected: str | None = None
    if retired:
        for index, state in enumerate(states):
            if state == "live":
                states[index] = "selected"
                selected = ladder[index]
                break
    entries = tuple(LadderEntry(str(name), state) for name, state in zip(ladder, states, strict=True))
    probe = lookup.get(effective.casefold())
    if suggestion is None and probe is not None and probe.retirement is not None:
        suggestion = probe.retirement.suggestion
    if not retired:
        action = "keep"
    elif selected is not None:
        action = "rebind"
        notes.append("default_cloud_models.%s: %s -> %s (ladder #%d)" % (
            tier, effective, selected, ladder.index(selected) + 1))
    else:
        action = "needs_operator_choice"
        notes.append(
            "needs operator choice: tier %s: %s retired%s; ladder: %s" % (
                tier,
                effective,
                "; provider suggests %s" % suggestion if suggestion else "",
                ", ".join("%s (%s)" % (e.model, e.state) for e in entries) or "none",
            )
        )
    return TierDecision(
        tier, effective, source, action, selected, entries, suggestion, "; ".join(notes)
    )


def plan_pulls(
    prefs: Preferences | None,
    probes: Mapping[str, ProbeResult],
    pulled_cf: frozenset[str],
    retired_cf: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(pulls, refused)``: live ladder stubs that are not pulled, only when auto-pull is on."""
    if prefs is None or not prefs.auto_pull_stubs:
        return (), ()
    lookup = _probe_lookup(probes)
    pulls: list[str] = []
    refused: list[str] = []
    seen: set[str] = set()
    for tier in CLOUD_TIERS:
        for name in ladder(prefs, tier):
            folded = name.casefold()
            probe = lookup.get(folded)
            if folded in seen or folded in pulled_cf or folded in retired_cf:
                continue
            if probe is None or probe.status is not ProbeStatus.LIVE:
                continue
            seen.add(folded)
            if not is_cloud_stub_name(name):
                refused.append(name)
            elif len(pulls) < MAX_PULLS:
                pulls.append(name)
    return tuple(pulls), tuple(refused)


@dataclass(frozen=True)
class RefreshInputs:
    """Everything one plan depends on; built by the orchestrator, immutable."""

    shipped: Mapping[str, Any]
    overlay: Mapping[str, Any] | None
    effective: Mapping[str, Any]
    env: Mapping[str, str]
    prefs: Preferences | None
    known: tuple[KnownModel, ...]
    probes: Mapping[str, ProbeResult]
    pulled_before: frozenset[str]
    pulled_now: frozenset[str] = frozenset()
    catalog_families: Mapping[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class RefreshPlan:
    retire_add: tuple[tuple[str, Retirement], ...]
    retired_after: tuple[str, ...]
    write_retired: bool
    decisions: tuple[TierDecision, ...]
    pulls: tuple[str, ...]
    refused_pulls: tuple[str, ...]
    notes: tuple[str, ...]


def plan_refresh(inputs: RefreshInputs) -> RefreshPlan:
    shipped_retired = [str(name) for name in inputs.shipped["retired_cloud_models"]]
    overlay_retired_raw = (inputs.overlay or {}).get("retired_cloud_models")
    overlay_retired = (
        [str(name) for name in overlay_retired_raw]
        if isinstance(overlay_retired_raw, list)
        else None
    )
    effective_cf = {str(name).casefold() for name in inputs.effective["retired_cloud_models"]}
    sources = {model.name.casefold(): model.sources for model in inputs.known}
    notes: list[str] = []
    retire_add: list[tuple[str, Retirement]] = []
    base = retired_union(shipped_retired, overlay_retired, ())
    for name, probe in inputs.probes.items():
        folded = name.casefold()
        if probe.status is not ProbeStatus.RETIRED or probe.retirement is None:
            continue
        if folded in effective_cf or any(n.casefold() == folded for n, _ in retire_add):
            continue
        catalog_only = sources.get(folded, frozenset()) <= {"catalog"}
        if catalog_only and len(base) + len(retire_add) >= MAX_RETIRED:
            notes.append("retired list full: catalog-only %s reported, not written" % name)
            continue
        retire_add.append((name, probe.retirement))
    retired_after = retired_union(shipped_retired, overlay_retired, [n for n, _ in retire_add])
    # Heal (restore hidden shipped entries) only alongside a retirement this run
    # writes anyway: an overlay list that omits shipped entries may be a
    # deliberate operator un-retire, and the daily run must not undo it
    # without provider evidence.
    write_retired = bool(retire_add)
    retired_cf = frozenset(name.casefold() for name in retired_after)
    pulls, refused = plan_pulls(inputs.prefs, inputs.probes, inputs.pulled_before, retired_cf)
    pulled_cf = inputs.pulled_before | inputs.pulled_now
    decisions = tuple(
        decide_tier(
            tier,
            env_value=str(inputs.env.get("SONDER_CLOUD_" + tier.upper(), "") or ""),
            default_model=str(inputs.effective["default_cloud_models"][tier]),
            retired_cf=retired_cf,
            probes=inputs.probes,
            pulled_cf=pulled_cf,
            ladder=ladder(inputs.prefs, tier) if inputs.prefs is not None else (),
            suggestion=None,
        )
        for tier in CLOUD_TIERS
    )
    notes.extend(decision.note for decision in decisions if decision.note)
    return RefreshPlan(
        tuple(retire_add), tuple(retired_after), write_retired, decisions, pulls, refused,
        tuple(notes),
    )


@dataclass(frozen=True)
class Change:
    """One overlay edit (or a skipped one: action ``changed_concurrently``)."""

    action: str
    key: str
    before: Any
    after: Any
    model: str | None
    evidence: Mapping[str, Any] | None


def apply_to_overlay(
    document: Mapping[str, Any],
    plan: RefreshPlan,
    *,
    shipped: Mapping[str, Any],
    observed_defaults: Mapping[str, str],
) -> tuple[dict[str, Any], tuple[Change, ...]]:
    """Apply ``plan`` to a freshly read overlay document; pure, returns a new document.

    Only ``retired_cloud_models``, ``default_cloud_models.<tier>`` and ``version``
    are written.  The retired list is re-unioned against ``document``; a rebind
    is a compare-and-swap: it happens only if the tier's merged default still
    equals ``observed_defaults[tier]``, otherwise a ``changed_concurrently``
    change (not an edit) is returned for the report.
    """
    result: dict[str, Any] = copy.deepcopy(dict(document))
    result["version"] = SCHEMA_VERSION
    changes: list[Change] = []
    shipped_retired = [str(name) for name in shipped["retired_cloud_models"]]
    before_raw = document.get("retired_cloud_models")
    before = [str(name) for name in before_raw] if isinstance(before_raw, list) else None
    present_cf = {name.casefold() for name in retired_union(shipped_retired, before, ())}
    additions = [(name, ev) for name, ev in plan.retire_add if name.casefold() not in present_cf]
    if additions or (plan.write_retired and retired_heal_needed(shipped_retired, before)):
        after = retired_union(shipped_retired, before, [name for name, _ in additions])
        if after != before:
            result["retired_cloud_models"] = after
            if retired_heal_needed(shipped_retired, before):
                changes.append(Change("heal_retired_union", "retired_cloud_models",
                                      before, after, None, None))
            for name, retirement in additions:
                changes.append(Change("retire", "retired_cloud_models", before, after,
                                      name, retirement.evidence()))
    overlay_defaults = document.get("default_cloud_models")
    overlay_defaults = dict(overlay_defaults) if isinstance(overlay_defaults, Mapping) else {}
    for decision in plan.decisions:
        if decision.action != "rebind" or decision.new_model is None:
            continue
        key = "default_cloud_models." + decision.tier
        current = str(overlay_defaults.get(decision.tier, shipped["default_cloud_models"][decision.tier]))
        observed = observed_defaults.get(decision.tier)
        if observed is None or current != observed:
            changes.append(Change("changed_concurrently", key, observed, current,
                                  decision.new_model, None))
            continue
        if current == decision.new_model:
            continue
        overlay_defaults[decision.tier] = decision.new_model
        result["default_cloud_models"] = dict(overlay_defaults)
        changes.append(Change("rebind", key, current, decision.new_model, decision.new_model,
                              {"ladder_index": _ladder_index(decision) + 1}))
    return result, tuple(changes)


def _ladder_index(decision: TierDecision) -> int:
    for index, entry in enumerate(decision.ladder):
        if entry.state == "selected":
            return index
    return -1


# --- refresh state -----------------------------------------------------------


def _probe_fields(probe: ProbeResult, now_iso: str, previous: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {"status": probe.status.value, "probed_at": now_iso}
    if probe.status is ProbeStatus.LIVE:
        fields.update(
            parameters=probe.parameters,
            context_length=probe.context_length,
            capabilities=list(probe.capabilities),
            family=probe.family,
        )
    if probe.status is ProbeStatus.RETIRED and probe.retirement is not None:
        old = previous.get("retired")
        detected = (
            old.get("detected_at")
            if isinstance(old, Mapping) and old.get("retired_on") == probe.retirement.retired_on
            else None
        )
        fields["retired"] = {**probe.retirement.evidence(), "detected_at": detected or now_iso}
    return fields


def _merge_models(
    previous: Mapping[str, Any],
    known: Sequence[KnownModel],
    probes: Mapping[str, ProbeResult],
    now_iso: str,
) -> dict[str, Any]:
    models: dict[str, Any] = {
        name: copy.deepcopy(dict(entry))
        for name, entry in previous.items()
        if valid_model_name(name) and isinstance(entry, Mapping)
    }
    lookup = _probe_lookup(probes)
    current: set[str] = set()
    for model in known:
        current.add(model.name)
        entry = models.get(model.name) or {"first_seen": now_iso}
        entry.setdefault("first_seen", now_iso)
        entry["last_seen"] = now_iso
        entry["sources"] = sorted(model.sources, key=PRIORITY.index)
        probe = lookup.get(model.name.casefold())
        if probe is not None:
            fields = _probe_fields(probe, now_iso, entry)
            for stale in ("retired", "parameters", "context_length", "capabilities", "family"):
                entry.pop(stale, None)
            entry.update(fields)
        elif entry.get("status") not in MODEL_STATUSES:
            entry["status"] = "unprobed"
        models[model.name] = entry
    if len(models) > MAX_STATE_MODELS:
        stale_names = sorted(
            (name for name in models if name not in current),
            key=lambda name: str(models[name].get("last_seen", "")),
        )
        for name in stale_names[: len(models) - MAX_STATE_MODELS]:
            del models[name]
    if len(models) > MAX_STATE_MODELS:
        for name in sorted(models)[MAX_STATE_MODELS:]:
            del models[name]
    return models


def _merge_catalog(
    previous: Mapping[str, Any] | None,
    families: Mapping[str, tuple[str, ...]] | None,
    refreshed: Iterable[str],
    *,
    now_iso: str,
    source: str,
) -> dict[str, Any] | None:
    old = copy.deepcopy(dict(previous)) if isinstance(previous, Mapping) else None
    if families is None:
        return old
    old_families = old.get("families") if old else None
    merged: dict[str, Any] = {}
    refreshed_set = set(refreshed)
    for family in list(families)[:MAX_FAMILIES]:
        prior = old_families.get(family) if isinstance(old_families, Mapping) else None
        entry = dict(prior) if isinstance(prior, Mapping) else {"first_seen": now_iso, "tags": []}
        entry["last_seen"] = now_iso
        if family in refreshed_set:
            entry["tags"] = list(families[family])[:MAX_TAGS_PER_FAMILY]
            entry["tags_fetched_at"] = now_iso
        merged[family] = entry
    return {"fetched_at": now_iso, "source": source, "families": merged}


def _pending_choices(
    previous: object, decisions: Sequence[TierDecision], now_iso: str
) -> list[dict[str, Any]]:
    prior = {
        (item.get("tier"), item.get("model")): item.get("since")
        for item in previous
        if isinstance(item, Mapping)
    } if isinstance(previous, list) else {}
    return [
        {
            "tier": decision.tier,
            "model": decision.effective_model,
            "since": prior.get((decision.tier, decision.effective_model)) or now_iso,
            "suggestion": decision.suggestion,
            "ladder": [{"model": e.model, "state": e.state} for e in decision.ladder],
        }
        for decision in decisions
        if decision.action == "needs_operator_choice"
    ]


def merge_state(
    previous: Mapping[str, Any] | None,
    *,
    now_iso: str,
    run_id: str,
    known: Sequence[KnownModel],
    probes: Mapping[str, ProbeResult],
    plan: RefreshPlan,
    applied_rebinds: Sequence[Change],
    catalog: Mapping[str, tuple[str, ...]] | None,
    catalog_refreshed: Iterable[str],
    catalog_source: str,
    last_refresh: Mapping[str, Any],
    succeeded: bool,
) -> dict[str, Any]:
    """The next state document; the caller validates it with :func:`validate_state`."""
    old = previous if isinstance(previous, Mapping) else {}
    models = old.get("models")
    state: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "models": _merge_models(models if isinstance(models, Mapping) else {}, known, probes, now_iso),
        "pending_choices": _pending_choices(old.get("pending_choices"), plan.decisions, now_iso),
        "last_refresh": copy.deepcopy(dict(last_refresh)),
    }
    last_success = now_iso if succeeded else old.get("last_success_at")
    if isinstance(last_success, str):
        state["last_success_at"] = last_success
    merged_catalog = _merge_catalog(
        old.get("catalog") if isinstance(old.get("catalog"), Mapping) else None,
        catalog, catalog_refreshed, now_iso=now_iso, source=catalog_source,
    )
    if merged_catalog is not None:
        state["catalog"] = merged_catalog
    pins_raw = old.get("overlay_pins")
    pins = {
        tier: copy.deepcopy(dict(pin))
        for tier, pin in (pins_raw.items() if isinstance(pins_raw, Mapping) else ())
        if tier in CLOUD_TIERS and isinstance(pin, Mapping)
    }
    for change in applied_rebinds:
        if change.action == "rebind":
            tier = change.key.rpartition(".")[2]
            pins[tier] = {"model": change.after, "replaced": change.before,
                          "written_at": now_iso, "run_id": run_id}
    if pins:
        state["overlay_pins"] = pins
    return state


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_models(models: object) -> None:
    _require(isinstance(models, dict), "models must be an object")
    assert isinstance(models, dict)
    _require(len(models) <= MAX_STATE_MODELS, "models exceeds %d entries" % MAX_STATE_MODELS)
    for name, entry in models.items():
        _require(valid_model_name(name), "invalid model name in state")
        _require(isinstance(entry, dict), "model entry must be an object")
        _require(entry.get("status") in MODEL_STATUSES, "invalid model status for %s" % name)
        sources = entry.get("sources", [])
        _require(isinstance(sources, list) and set(sources) <= SOURCES, "invalid sources for %s" % name)
        for key in ("first_seen", "last_seen", "probed_at"):
            if key in entry:
                _require(parse_timestamp(entry[key]) is not None, "invalid %s for %s" % (key, name))
        if "retired" in entry:
            _require(entry["status"] == "retired" and isinstance(entry["retired"], dict),
                     "retired evidence only on retired models")


def _validate_catalog(catalog: object) -> None:
    _require(isinstance(catalog, dict), "catalog must be an object")
    assert isinstance(catalog, dict)
    families = catalog.get("families", {})
    _require(isinstance(families, dict) and len(families) <= MAX_FAMILIES,
             "catalog families must be a bounded object")
    for family, entry in families.items():
        _require(isinstance(family, str) and _FAMILY_RE.fullmatch(family) is not None,
                 "invalid catalog family")
        _require(isinstance(entry, dict), "catalog family entry must be an object")
        tags = entry.get("tags", [])
        _require(
            isinstance(tags, list) and len(tags) <= MAX_TAGS_PER_FAMILY
            and all(isinstance(tag, str) and is_cloud_stub_name(tag) for tag in tags),
            "invalid catalog tags for %s" % family,
        )


def validate_state(document: object) -> None:
    """Raise ValueError unless ``document`` is a well-formed, bounded refresh state."""
    _require(isinstance(document, dict), "state must be a JSON object")
    assert isinstance(document, dict)
    _require(set(document) <= STATE_KEYS, "unknown state key(s)")
    _require(type(document.get("version")) is int and document["version"] == SCHEMA_VERSION,
             "state version must be %d" % SCHEMA_VERSION)
    if "last_success_at" in document:
        _require(parse_timestamp(document["last_success_at"]) is not None,
                 "invalid last_success_at")
    _validate_models(document.get("models", {}))
    if "catalog" in document:
        _validate_catalog(document["catalog"])
    pending = document.get("pending_choices", [])
    _require(isinstance(pending, list) and len(pending) <= MAX_PENDING
             and all(isinstance(item, dict) and item.get("tier") in CLOUD_TIERS for item in pending),
             "invalid pending_choices")
    pins = document.get("overlay_pins", {})
    _require(isinstance(pins, dict) and set(pins) <= set(CLOUD_TIERS)
             and all(isinstance(pin, dict) and valid_model_name(pin.get("model")) for pin in pins.values()),
             "invalid overlay_pins")
    _require(isinstance(document.get("last_refresh", {}), dict), "invalid last_refresh")


__all__ = [
    "ACTIONS",
    "CLOUD_TIERS",
    "Change",
    "KnownModel",
    "LadderEntry",
    "MAX_FAMILIES",
    "MAX_LADDER",
    "MAX_PROBES",
    "MAX_PULLS",
    "MAX_RETIRED",
    "MAX_STATE_MODELS",
    "MAX_TAGS_PER_FAMILY",
    "PRIORITY",
    "Preferences",
    "ProbeResult",
    "ProbeStatus",
    "RefreshInputs",
    "RefreshPlan",
    "Retirement",
    "SCHEMA_VERSION",
    "TierDecision",
    "apply_to_overlay",
    "classify_show",
    "collect_known_models",
    "decide_tier",
    "is_cloud_stub_name",
    "is_pulled_cloud_row",
    "iso_timestamp",
    "ladder",
    "merge_state",
    "parse_catalog_families",
    "parse_catalog_tags",
    "parse_preferences",
    "parse_retirement",
    "parse_timestamp",
    "plan_pulls",
    "plan_refresh",
    "retired_heal_needed",
    "retired_union",
    "show_metadata",
    "valid_model_name",
]
