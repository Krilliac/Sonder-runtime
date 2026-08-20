"""Consolidated, read-only health report for the Sonder runtime ("sonder doctor").

This module does not perform any health work of its own. It aggregates signals
that already exist elsewhere in the runtime -- config/preflight, self-heal,
memory quality, runtime policy, and Ollama reachability -- into one structured
report plus a plain-text renderer.

Design constraints that this module deliberately honours:

* **Read-only.** ``run_doctor`` never mutates state, applies repairs, writes
  files, or opens sockets on its own. It only calls the check callables it is
  handed and normalizes what they return. The bundled default checks are chosen
  to be read-only too (they inspect, they do not repair), and each guards its
  own imports/probes so a missing collaborator degrades to ``skipped`` rather
  than crashing.
* **Injectable collaborators.** Every check is a callable supplied through the
  ``checks`` registry. Tests inject fakes for full offline determinism; the
  real wiring is layered on later by whichever CLI/server surface calls
  ``run_doctor``. When ``checks`` is ``None`` we fall back to
  ``default_checks()``.
* **Import-safe.** Importing this module opens no DB, starts no thread, reads no
  mutable env, and needs no live model or network. All heavy imports and probes
  live inside the default-check bodies, which only run when ``run_doctor`` is
  actually invoked.

A single check contributes one entry to the report::

    {"name": "ollama", "status": "ok", "detail": "127.0.0.1: 3 models"}

and the report rolls those up::

    {"overall": "ok" | "warn" | "fail", "checks": [ ...entries... ]}
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from sonder_runtime.adapters.config_validation import (
    validated_config_check as _validated_config_check,
)
from sonder_runtime.domain.doctor_result import normalize_result as _normalize_result
from sonder_runtime.domain.doctor_result import skipped as _skipped_result
from sonder_runtime.domain.doctor_status import coerce_status as _coerce_status

# Status vocabulary. ``skipped`` is intentionally neutral: a collaborator that
# is simply absent should not make an otherwise-healthy runtime look sick.
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

_VALID_STATUSES = frozenset(
    {STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIPPED}
)

# Rollup precedence: any fail dominates, then any warn, otherwise ok. Skipped
# never counts toward warn/fail, so an all-skipped report is still ``ok``.
_SEVERITY = {STATUS_OK: 0, STATUS_SKIPPED: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
_SEVERITY_TO_STATUS = {0: STATUS_OK, 1: STATUS_WARN, 2: STATUS_FAIL}

# A check spec is anything ``_iter_specs`` can turn into a ``(name, callable)``
# pair. A check callable takes no required arguments and returns one of:
#   * a status string ("ok" / "warn" / "fail" / "skipped")
#   * a (status, detail) tuple
#   * a mapping with at least "status" and optionally "name"/"detail"
# or it may raise -- a raised exception is captured as a ``fail`` entry.
CheckCallable = Callable[[], Any]


def _iter_specs(
    checks: Mapping[str, CheckCallable] | Iterable[Any],
) -> list[tuple[str, CheckCallable]]:
    """Normalize a heterogeneous check registry into ordered (name, fn) pairs.

    Accepts, in rough order of convenience:

    * a mapping ``{name: callable}`` (insertion order preserved),
    * an iterable of ``(name, callable)`` pairs,
    * an iterable of bare callables (name taken from ``.name`` or ``__name__``).
    """
    specs: list[tuple[str, CheckCallable]] = []
    if isinstance(checks, Mapping):
        for name, fn in checks.items():
            specs.append((str(name), fn))
        return specs
    for index, item in enumerate(checks):
        if isinstance(item, tuple) and len(item) == 2:
            name, fn = item
            specs.append((str(name), fn))
        elif callable(item):
            name = getattr(item, "name", None) or getattr(
                item, "__name__", None
            )
            specs.append((str(name or "check_%d" % index), item))
        else:
            raise TypeError(
                "check spec must be a (name, callable) pair or a callable, "
                "got %r" % (item,)
            )
    return specs


def run_doctor(
    checks: Mapping[str, CheckCallable] | Iterable[Any] | None = None,
) -> dict:
    """Run every check and return one structured, read-only health report.

    Parameters
    ----------
    checks:
        The check registry (see :data:`CheckCallable`). ``None`` uses
        :func:`default_checks`. Order is preserved: the ``checks`` list in the
        report follows the order of the registry.

    Returns
    -------
    dict
        ``{"overall": "ok"|"warn"|"fail", "checks": [{name, status, detail}]}``.

    Each check is executed inside its own ``try``/``except``. A check that
    raises becomes a captured ``fail`` entry whose ``detail`` names the
    exception -- one broken collaborator never aborts the whole report.
    """
    registry = default_checks() if checks is None else checks
    specs = _iter_specs(registry)

    entries: list[dict] = []
    worst = 0
    for name, fn in specs:
        try:
            raw = fn()
            entry = _normalize_result(name, raw)
        except Exception as exc:  # noqa: BLE001 - one failure must not abort
            entry = {
                "name": str(name),
                "status": STATUS_FAIL,
                "detail": "%s: %s" % (exc.__class__.__name__, exc),
            }
        entries.append(entry)
        worst = max(worst, _SEVERITY[entry["status"]])

    return {"overall": _SEVERITY_TO_STATUS[worst], "checks": entries}


def render_report(report: Mapping[str, Any]) -> str:
    """Render :func:`run_doctor`'s report as a clean terminal summary.

    The layout is a single header line stating the overall verdict followed by
    one aligned line per check. It is intentionally plain text (no colour codes)
    so it renders identically in logs, pipes, and terminals.
    """
    overall = str(report.get("overall", "unknown")).lower()
    checks = list(report.get("checks") or [])

    lines = ["sonder doctor: %s" % overall.upper()]
    if not checks:
        lines.append("  (no checks run)")
        return "\n".join(lines)

    # Pad the status column so details line up regardless of status length.
    marks = {
        STATUS_OK: "ok",
        STATUS_WARN: "warn",
        STATUS_FAIL: "FAIL",
        STATUS_SKIPPED: "skip",
    }
    name_width = max(len(str(c.get("name", ""))) for c in checks)
    for check in checks:
        name = str(check.get("name", ""))
        status = str(check.get("status", "")).lower()
        mark = marks.get(status, status or "?")
        detail = str(check.get("detail", "") or "")
        line = "  [%-4s] %-*s" % (mark, name_width, name)
        if detail:
            line = "%s  %s" % (line, detail)
        lines.append(line.rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default (production) checks.
#
# These are the read-only checks wired in when no registry is injected. Each is
# fully self-contained: it imports its collaborator lazily, guards every probe,
# and returns a ``skipped`` verdict (never raises) when a collaborator or its
# inputs are unavailable. Tests do not exercise these -- they inject fakes -- so
# these bodies favour defensiveness over precision.
# ---------------------------------------------------------------------------


def _skip(reason: str) -> dict:
    return _skipped_result(reason)


def _load_config_or_none():
    """Best-effort read-only config load; ``None`` if unavailable."""
    try:
        from sonder_runtime.platform import config as sonder_config
    except Exception:
        return None
    try:
        return sonder_config.load_config()
    except Exception:
        return None


def _check_config() -> dict:
    """Report whether configuration loads and validates (read-only)."""
    try:
        from sonder_runtime.platform import config as sonder_config
    except Exception as exc:
        return _skip("sonder_config unavailable (%s)" % exc)
    try:
        config = sonder_config.load_config()
    except sonder_config.ConfigError as exc:
        return {"status": STATUS_FAIL, "detail": "config invalid: %s" % exc}
    except Exception as exc:
        return _skip("config load failed (%s)" % exc)
    return validated_config_check(config)()


def validated_config_check(config):
    """Compatibility alias for the packaged config-check adapter."""
    return _validated_config_check(config)


def _check_self_heal() -> dict:
    """Summarize self-heal findings without applying any repair (read-only)."""
    try:
        import self_heal
    except Exception as exc:
        return _skip("self_heal unavailable (%s)" % exc)
    import os

    db_path = os.environ.get("SONDER_DB")
    if not db_path:
        return _skip("SONDER_DB not set")
    try:
        issues = self_heal.check(db_path)
    except Exception as exc:
        return _skip("self-heal check failed (%s)" % exc)
    if not issues:
        return {"status": STATUS_OK, "detail": "no issues"}
    repairable = sum(1 for i in issues if getattr(i, "repairable", False))
    status = STATUS_WARN if repairable == len(issues) else STATUS_FAIL
    return {
        "status": status,
        "detail": "%d issue(s), %d repairable" % (len(issues), repairable),
    }


def _check_memory_quality() -> dict:
    """Summarize memory-quality audit counters (read-only)."""
    import os

    db_path = os.environ.get("SONDER_DB")
    if not db_path:
        return _skip("SONDER_DB not set")
    try:
        import memory_quality
        import sonder_runtime.adapters.memory_store as memory_store
    except Exception as exc:
        return _skip("memory quality surfaces unavailable (%s)" % exc)
    try:
        conn = memory_store.connect(db_path)
    except Exception as exc:
        return _skip("cannot open memory db (%s)" % exc)
    try:
        audit = memory_quality.audit(conn)
    except Exception as exc:
        return _skip("audit failed (%s)" % exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    severe = int(audit.get("path_or_secret_like", 0)) + int(
        audit.get("missing_fts", 0)
    ) + int(audit.get("orphan_fts", 0))
    hygiene = int(audit.get("exact_duplicate_prunable", 0)) + int(
        audit.get("no_embedding", 0)
    ) + int(audit.get("vague_without_anchor", 0))
    total = int(audit.get("total_lessons", 0))
    if severe:
        return {
            "status": STATUS_FAIL,
            "detail": "%d lessons, %d severe issue(s)" % (total, severe),
        }
    if hygiene:
        return {
            "status": STATUS_WARN,
            "detail": "%d lessons, %d hygiene issue(s)" % (total, hygiene),
        }
    return {"status": STATUS_OK, "detail": "%d lessons clean" % total}


def _check_runtime_policy() -> dict:
    """Report runtime-policy load status without creating the file."""
    try:
        import sonder_runtime.adapters.runtime_policy as runtime_policy
    except Exception as exc:
        return _skip("runtime_policy unavailable (%s)" % exc)
    try:
        policy = runtime_policy.load(create=False)
    except Exception as exc:
        return _skip("policy load failed (%s)" % exc)
    if policy.get("error"):
        return {
            "status": STATUS_WARN,
            "detail": "policy error, safe defaults active: %s"
            % policy["error"],
        }
    return {
        "status": STATUS_OK,
        "detail": "revision=%s source=%s"
        % (policy.get("revision", "?"), policy.get("source", "")),
    }


def schema_check(config=None):
    """Bind a non-mutating, non-disclosing migration-health check."""
    def check():
        try:
            import sonder_runtime.adapters.persistence.migrations as sonder_migrations

            cfg = config if config is not None else _load_config_or_none()
            if cfg is None:
                return _skip("config unavailable for schema inspection")
            statuses = sonder_migrations.status_all_read_only(cfg.state.home)
        except Exception as exc:
            return {
                "status": STATUS_FAIL,
                "detail": (
                    "schema inspection failed (%s)" % exc.__class__.__name__
                ),
            }

        records = tuple(statuses.values())
        modified = sum(len(record.checksum_mismatches) for record in records)
        future = sum(len(record.unknown) for record in records)
        pending = sum(len(record.pending) for record in records)
        applied = sum(len(record.applied) for record in records)
        if modified or future:
            return {
                "status": STATUS_FAIL,
                "detail": (
                    "%d store(s); unhealthy history: modified=%d future=%d; "
                    "pending=%d"
                ) % (len(records), modified, future, pending),
            }
        if pending:
            return {
                "status": STATUS_WARN,
                "detail": "%d store(s); pending migrations=%d" % (
                    len(records), pending
                ),
            }
        return {
            "status": STATUS_OK,
            "detail": "%d store(s) current; applied migrations=%d" % (
                len(records), applied
            ),
        }

    return check


def _check_ollama(*, timeout: float = 5.0) -> dict:
    """Probe Ollama reachability read-only via GET /api/tags."""
    config = _load_config_or_none()
    if config is None:
        return _skip("config unavailable for Ollama endpoint")
    url = getattr(getattr(config, "ollama", None), "url", None)
    if not url:
        return _skip("no Ollama url configured")
    try:
        import json
        import urllib.error
        import urllib.request
        from urllib.parse import urlsplit
    except Exception as exc:  # pragma: no cover - stdlib import guard
        return _skip("urllib unavailable (%s)" % exc)
    host = urlsplit(url).hostname or ""
    tags_url = url.rstrip("/") + "/api/tags"
    try:
        request = urllib.request.Request(tags_url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return {
                    "status": STATUS_FAIL,
                    "detail": "%s: HTTP %s" % (host, response.status),
                }
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
            models = len(payload.get("models") or [])
            if not models:
                # Reachable is not ready: with an empty catalog there is no
                # model to serve, so the next chat turn fails at the provider.
                # Report that rather than a bare "ok" the operator would read
                # as "models are fine".  Warn, not fail — the runtime does
                # start, and a warn keeps `sonder doctor` at exit code 0.
                return {
                    "status": STATUS_WARN,
                    "detail": (
                        "%s: reachable, no models installed "
                        "(run setup_alias.py)" % host
                    ),
                }
            return {
                "status": STATUS_OK,
                "detail": "%s: %d models" % (host, models),
            }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": STATUS_FAIL, "detail": "%s: %s" % (host, exc)}


def storage_checks(config=None, *, throughput: bool = False):
    """Build storage checks for a validated config without running them yet."""
    def loaded_config():
        if config is not None:
            return config
        loaded = _load_config_or_none()
        if loaded is None:
            raise RuntimeError("configuration unavailable")
        return loaded

    def state_check():
        from sonder_runtime.adapters import storage as sonder_storage

        cfg = loaded_config()
        record = sonder_storage.inspect_root(
            cfg.state.home,
            minimum_free_bytes=cfg.state.minimum_free_disk_bytes,
            role="state",
        )
        status = STATUS_WARN if record["warnings"] else STATUS_OK
        detail = sonder_storage.summarize(record)
        if throughput:
            probe = sonder_storage.throughput_probe(record["path"])
            detail += "; explicit probe write=%.1f MiB/s read=%.1f MiB/s" % (
                probe["write_mib_s"], probe["read_mib_s"]
            )
        return {"status": status, "detail": detail}

    def models_check():
        from sonder_runtime.adapters import storage as sonder_storage

        cfg = loaded_config()
        records = [
            sonder_storage.inspect_root(
                root,
                minimum_free_bytes=cfg.state.minimum_free_disk_bytes,
                role="models",
            )
            for root in sonder_storage.model_roots()
        ]
        status = STATUS_WARN if any(r["warnings"] for r in records) else STATUS_OK
        return {
            "status": status,
            "detail": " | ".join(sonder_storage.summarize(r) for r in records),
        }

    return [("storage_state", state_check), ("storage_models", models_check)]


def default_checks() -> list[tuple[str, CheckCallable]]:
    """The ordered, read-only checks used when no registry is injected.

    Returned as ``(name, callable)`` pairs so ordering in the report is stable
    and callers can inspect or subset the registry before handing it back to
    :func:`run_doctor`.
    """
    return [
        ("config", _check_config),
        *storage_checks(),
        ("schemas", schema_check()),
        ("self_heal", _check_self_heal),
        ("memory_quality", _check_memory_quality),
        ("runtime_policy", _check_runtime_policy),
        ("ollama", _check_ollama),
    ]


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import sys

    _report = run_doctor()
    print(render_report(_report))
    # Exit non-zero when the consolidated health is failing so scripts and
    # CI can gate on it; a warn state is informational and still exits 0.
    sys.exit(1 if _report.get("overall") == STATUS_FAIL else 0)
