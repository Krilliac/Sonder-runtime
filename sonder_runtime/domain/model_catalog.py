"""Pure parsing of Ollama's tag catalog into canonical model records.

The catalog payload is the only cheap metadata available for every model.
These helpers deduplicate names case-insensitively, keep unknown records
eligible, read a tag's immutable digest and resolve a selector (exactly, or
an untagged name to its ``:latest`` record); the HTTP fetch itself stays with
the caller. Moved from ``server.py`` in the WP1 Three-Hundred-Twenty-Ninth
Slice with its behaviour intact; the ``:latest`` reading of an untagged
selector was added later.
"""
from __future__ import annotations


def catalog_names(payload):
    """Return the catalog as canonical, deduplicated model names."""
    raw = payload.get("models", []) if isinstance(payload, dict) else []
    names, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        name = str(item.get("name") or item.get("model") or "").strip() if isinstance(item, dict) else ""
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    return sorted(names, key=str.casefold)


def catalog_records(payload):
    """Return canonical catalog records, sorted, without probing or selecting."""
    raw = payload.get("models", []) if isinstance(payload, dict) else []
    records, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        records.append((name, item))
    return sorted(records, key=lambda row: row[0].casefold())


def installed_records(payload) -> tuple[tuple[str, dict], ...]:
    """Return one coherent catalog snapshot, in catalog order, for policy validation."""
    rows = payload.get("models", []) if isinstance(payload, dict) else []
    records, seen = [], set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            records.append((name, item))
    return tuple(records)


def catalog_revision(model, records):
    """Return the immutable catalog digest for one local model tag, or ``""``.

    Ollama tags are mutable, so the deterministic request cache treats an
    absent digest as an admission failure; an untagged model also matches its
    ``:latest`` record.
    """
    requested = str(model or "").strip().casefold()
    if not requested:
        return ""
    candidates = {requested}
    if ":" not in requested:
        candidates.add(requested + ":latest")
    for name, record in records:
        advertised = str(name or "").strip().casefold()
        if advertised not in candidates:
            continue
        record = record if isinstance(record, dict) else {}
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        digest = str(record.get("digest") or details.get("digest") or "").strip()
        if digest:
            return digest
    return ""


def _untagged(name):
    """True when the model part of ``name`` (after the last ``/``) carries no tag."""
    return ":" not in name.rsplit("/", 1)[-1]


def resolve_record(selector, records):
    """Resolve a catalog record case-insensitively, or return None.

    An exact name always wins. A selector without a tag also matches its
    ``:latest`` record, which is how Ollama itself reads an untagged name (and
    what ``catalog_revision`` already does). No other tag is ever inferred, a
    tagged selector never matches an untagged record, and the name returned is
    always the catalog's own, so callers keep judging the model that answers.
    """
    wanted = str(selector or "").strip().casefold()
    if not wanted:
        return None
    latest = wanted + ":latest" if _untagged(wanted) else None
    fallback = None
    for name, record in records:
        advertised = name.casefold()
        if advertised == wanted:
            return name, record
        if fallback is None and advertised == latest:
            fallback = (name, record)
    return fallback
