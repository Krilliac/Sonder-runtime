"""Pure parsing of Ollama's tag catalog into canonical model records.

The catalog payload is the only cheap metadata available for every model.
These helpers deduplicate names case-insensitively, keep unknown records
eligible, read a tag's immutable digest and resolve an exact selector; the
HTTP fetch itself stays with the caller. Moved from ``server.py`` in the WP1
Three-Hundred-Twenty-Ninth Slice with its behaviour byte-for-byte intact.
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


def resolve_record(selector, records):
    """Resolve an exact catalog record case-insensitively, or return None."""
    wanted = str(selector or "").strip().casefold()
    if not wanted:
        return None
    for name, record in records:
        if name.casefold() == wanted:
            return name, record
    return None
