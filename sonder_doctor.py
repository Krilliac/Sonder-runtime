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

from typing import Any, Iterable, Mapping, Sequence

from sonder_runtime.adapters.config_validation import (
    validated_config_check as _validated_config_check,
)
from sonder_runtime.domain.doctor_result import normalize_result as _normalize_result
from sonder_runtime.domain.doctor_result import skipped as _skipped_result
from sonder_runtime.domain.doctor_status import coerce_status as _coerce_status
from sonder_runtime.bootstrap.doctor_formatting import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WARN,
    _SEVERITY,
    _SEVERITY_TO_STATUS,
    render_report,
    rollup_status,
)
from sonder_runtime.domain.doctor_specs import (
    CheckCallable,
    iter_specs as _iter_specs,
)
from sonder_runtime.bootstrap.config_loading import (
    check_config as _check_config_impl,
    load_config_or_none as _load_config_or_none_impl,
)
from sonder_runtime.bootstrap.doctor_checks import (
    bounded_join as _bounded_join,
    summarize_memory_quality as _summarize_memory_quality,
    summarize_self_heal as _summarize_self_heal,
    summarize_worker_probe as _summarize_worker_probe,
)

# A check spec is anything ``_iter_specs`` can turn into a ``(name, callable)``
# pair. A check callable takes no required arguments and returns one of:
#   * a status string ("ok" / "warn" / "fail" / "skipped")
#   * a (status, detail) tuple
#   * a mapping with at least "status" and optionally "name"/"detail"
# or it may raise -- a raised exception is captured as a ``fail`` entry.
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

    return {"overall": rollup_status(entries), "checks": entries}


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
    """Compatibility delegate for the packaged bootstrap config boundary."""
    return _load_config_or_none_impl()


def _check_config() -> dict:
    """Compatibility wrapper for the packaged read-only config check."""
    return _check_config_impl()()


def validated_config_check(config):
    """Compatibility alias for the packaged config-check adapter."""
    return _validated_config_check(config)


def _memory_db_target(config=None) -> tuple[str | None, str]:
    """Resolve the memory database doctor inspects, without touching it.

    ``SONDER_DB`` is the explicit override every runtime surface honours. When
    it is unset the database is ``<state home>/memory.db`` -- the same file the
    REPL and server use -- taken from the operator-selected configuration so
    ``--config``/``--set`` select the same home the other checks inspect.
    Unlike ``paths.memory_db_path`` this never creates the home or performs the
    one-time legacy-database migration: doctor is read-only.
    """
    import os
    from pathlib import Path

    override = os.environ.get("SONDER_DB", "").strip()
    if override:
        return str(Path(override).expanduser()), "SONDER_DB"
    cfg = config if config is not None else _load_config_or_none()
    home = getattr(getattr(cfg, "state", None), "home", None) if cfg else None
    if not home:
        return None, "config unavailable to locate the state home"
    return str(Path(home).expanduser() / "memory.db"), "state home"


def _existing_memory_db(config=None) -> tuple[str | None, dict | None]:
    """Return ``(path, None)`` for an existing DB, else ``(None, skip)``."""
    import os

    db_path, source = _memory_db_target(config)
    if db_path is None:
        return None, _skip(source)
    if not os.path.isfile(db_path):
        # Opening it would create an empty store; report instead of writing.
        return None, _skip("no memory database yet (%s)" % source)
    try:
        import sonder_runtime.adapters.memory_store as memory_store

        conn = memory_store.connect_read_only(db_path)
        try:
            initialized = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lessons'"
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:
        return None, _skip(
            "memory database unreadable (%s)" % exc.__class__.__name__
        )
    if not initialized:
        # A store the runtime has not initialized yet has nothing to audit;
        # initializing it here would break doctor's read-only contract.
        return None, _skip("memory database not initialized yet (%s)" % source)
    return db_path, None


def _check_self_heal(config=None) -> dict:
    """Summarize self-heal findings without applying any repair (read-only)."""
    try:
        import self_heal
        import sonder_runtime.adapters.memory_store as memory_store
    except Exception as exc:
        return _skip("self_heal unavailable (%s)" % exc)

    db_path, skipped = _existing_memory_db(config)
    if skipped is not None:
        return skipped

    def inspect(path):
        return self_heal.check(path, connect=memory_store.connect_read_only)

    return _summarize_self_heal(inspect, db_path)


def _check_memory_quality(config=None) -> dict:
    """Compatibility delegate for the packaged memory-quality policy."""
    db_path, skipped = _existing_memory_db(config)
    if skipped is not None:
        return skipped
    try:
        import memory_quality
        import sonder_runtime.adapters.memory_store as memory_store
    except Exception as exc:
        return _skip("memory quality surfaces unavailable (%s)" % exc)
    return _summarize_memory_quality(
        memory_store.connect_read_only, memory_quality.audit, db_path
    )


def memory_checks(config) -> list[tuple[str, CheckCallable]]:
    """Bind the memory-store checks to one already-validated configuration."""
    return [
        ("self_heal", lambda: _check_self_heal(config)),
        ("memory_quality", lambda: _check_memory_quality(config)),
    ]


def schema_epoch_check(config=None):
    """Bind a read-only check that ``serve`` would pass its epoch-2 gate.

    ``serve`` refuses to start until ``migrate --adopt-epoch2`` has stamped
    every SPEC-5 domain database at schema epoch 2, so an un-adopted home is a
    FAIL here: a doctor that says WARN/rc 0 while serve refuses is lying about
    whether the runtime can start. A home with no databases at all is also
    un-adopted -- serve's own startup creates ``memory.db`` before the gate
    and then refuses -- so it is reported the same way.
    """
    def check():
        try:
            from pathlib import Path

            from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
                EPOCH,
                EPOCH2_DATABASES,
                check_epoch,
            )

            cfg = config if config is not None else _load_config_or_none()
            if cfg is None:
                return _skip("config unavailable for schema-epoch inspection")
            home = Path(cfg.state.home).expanduser()
            # check_epoch returns None for a missing file without creating it.
            epochs = {name: check_epoch(home / name) for name in EPOCH2_DATABASES}
        except Exception as exc:
            return {
                "status": STATUS_FAIL,
                "detail": "schema epoch inspection failed (%s)"
                % exc.__class__.__name__,
            }
        future = sorted(
            name for name, epoch in epochs.items()
            if epoch is not None and epoch > EPOCH
        )
        if future:
            return {
                "status": STATUS_FAIL,
                "detail": "future schema epoch in %s; this build cannot run it"
                % ", ".join(future),
            }
        missing = sorted(name for name, epoch in epochs.items() if epoch != EPOCH)
        if missing:
            return {
                "status": STATUS_FAIL,
                "detail": (
                    "schema epoch %d not adopted (%s); serve will refuse to "
                    "start -- run `python -m sonder_runtime migrate "
                    "--adopt-epoch2`" % (EPOCH, ", ".join(missing))
                ),
            }
        return {
            "status": STATUS_OK,
            "detail": "schema epoch %d adopted (%d databases)"
            % (EPOCH, len(epochs)),
        }

    return check


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


def backup_check(config=None, *, max_age_hours: float = 48.0):
    """Bind a non-mutating check reporting the most recent backup outcome.

    Reads ``operations.db`` directly in ``mode=ro`` rather than opening an
    :class:`OperationsStore`, whose constructor auto-migrates the database on
    open -- a write side effect this read-only surface must not have.
    """
    def check():
        cfg = config if config is not None else _load_config_or_none()
        if cfg is None:
            return _skip("config unavailable for backup inspection")
        if not cfg.backup.enabled:
            return {"status": STATUS_OK, "detail": "backups disabled by configuration"}
        try:
            import sqlite3
            from pathlib import Path

            db_path = Path(cfg.state.home).expanduser() / "operations.db"
            if not db_path.is_file():
                row = None
            else:
                uri = db_path.resolve(strict=True).as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                try:
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='backup_run'"
                    ).fetchone()
                    row = (
                        conn.execute(
                            "SELECT backup_id, completed_at_utc, status, error_code"
                            " FROM backup_run ORDER BY started_at_utc DESC LIMIT 1"
                        ).fetchone()
                        if has_table
                        else None
                    )
                finally:
                    conn.close()
        except Exception as exc:
            return _skip("backup history unavailable (%s)" % exc.__class__.__name__)

        if row is None:
            return {"status": STATUS_WARN, "detail": "no backups have been created yet"}
        backup_id, completed_at_utc, run_status, error_code = row
        if run_status == "running":
            return {
                "status": STATUS_WARN,
                "detail": "backup %s has not finished (status=running)" % backup_id,
            }
        if run_status != "verified":
            detail = "latest backup %s: %s" % (backup_id, run_status)
            if error_code:
                detail += " (%s)" % error_code
            return {"status": STATUS_FAIL, "detail": detail}
        try:
            import calendar
            import time

            completed_epoch = calendar.timegm(
                time.strptime(completed_at_utc, "%Y-%m-%dT%H:%M:%SZ")
            )
            age_hours = (time.time() - completed_epoch) / 3600.0
        except (TypeError, ValueError):
            return {
                "status": STATUS_OK,
                "detail": "latest backup %s verified" % backup_id,
            }
        if age_hours > max_age_hours:
            return {
                "status": STATUS_WARN,
                "detail": "latest verified backup is %.1fh old (limit %.0fh)"
                % (age_hours, max_age_hours),
            }
        return {
            "status": STATUS_OK,
            "detail": "latest backup verified %.1fh ago" % age_hours,
        }

    return check


def _check_ollama(*, timeout: float = 5.0, config=None) -> dict:
    """Probe Ollama reachability read-only via GET /api/tags."""
    config = config if config is not None else _load_config_or_none()
    if config is None:
        return _skip("config unavailable for Ollama endpoint")
    if getattr(getattr(config, "membership", None), "mode", "static") == "external":
        return _skip("deferred: external membership requires explicit typed pool refresh")
    url = getattr(getattr(config, "ollama", None), "url", None)
    if not url:
        return _skip("no Ollama url configured")
    try:
        import json
        import urllib.error
        import urllib.request
        from urllib.parse import urlsplit
        from sonder_runtime.adapters.inference import ollama_endpoint
    except Exception as exc:  # pragma: no cover - import guard
        return _skip("Ollama transport unavailable (%s)" % exc)
    host = urlsplit(url).hostname or ""
    tags_url = url.rstrip("/") + "/api/tags"
    try:
        request = urllib.request.Request(tags_url, method="GET")
        with ollama_endpoint.open_url(
            request, timeout=timeout,
            allow_remote=getattr(config.ollama, "allow_remote", False) is True,
        ) as response:
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


def _check_ollama_workers(*, timeout: float = 5.0, config=None) -> dict:
    """Probe every configured multi-PC Ollama worker independently.

    ``_check_ollama`` only verifies the primary endpoint. A remote worker
    (``[ollama].workers`` / ``SONDER_OLLAMA_WORKERS``, see
    ``docs/runbooks/multi-pc-ollama.md``) is otherwise invisible in ``sonder
    doctor`` until a live request happens to fail over onto it -- an operator
    would not learn PC 2 or PC 3 is down until traffic actually needed it.
    """
    config = config if config is not None else _load_config_or_none()
    if config is None:
        return _skip("config unavailable for Ollama worker endpoints")
    if getattr(getattr(config, "membership", None), "mode", "static") == "external":
        return _skip("deferred: external membership requires explicit typed pool refresh")
    workers = tuple(getattr(getattr(config, "ollama", None), "workers", ()) or ())
    if not workers:
        return _skip("no worker endpoints configured (single-endpoint deployment)")
    try:
        import json
        import urllib.error
        import urllib.request
        from urllib.parse import urlsplit
        from sonder_runtime.adapters.inference import ollama_endpoint
    except Exception as exc:  # pragma: no cover - import guard
        return _skip("Ollama transport unavailable (%s)" % exc)

    up: list[str] = []
    down: list[str] = []
    for origin in workers:
        host = urlsplit(origin).hostname or origin
        tags_url = origin.rstrip("/") + "/api/tags"
        try:
            request = urllib.request.Request(tags_url, method="GET")
            with ollama_endpoint.open_url(
                request, timeout=timeout,
                allow_remote=getattr(config.ollama, "allow_remote", False) is True,
            ) as response:
                if response.status != 200:
                    down.append("%s (HTTP %s)" % (host, response.status))
                    continue
                payload = json.loads(response.read(1_048_576).decode("utf-8"))
                models = len(payload.get("models") or [])
                up.append("%s: %d models" % (host, models))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            down.append("%s (%s)" % (host, exc))

    return _summarize_worker_probe(up, down, len(workers))


def _check_ollama_residency(*, timeout: float = 5.0, config=None) -> dict:
    """Detect Ollama models that outlived their ``keep_alive`` expiry.

    ``/api/ps`` reports each resident model's ``expires_at``. Ollama is
    supposed to unload a model once that deadline passes; one still listed
    well after expiry usually means the eviction stalled -- a model wedged in
    VRAM (often from a killed/hung generation) rather than one legitimately
    still in use. This is a read-only observation, not a repair: it never
    unloads anything itself.
    """
    config = config if config is not None else _load_config_or_none()
    if config is None:
        return _skip("config unavailable for Ollama residency check")
    if getattr(getattr(config, "membership", None), "mode", "static") == "external":
        return _skip("deferred: external membership requires explicit typed pool refresh")
    url = getattr(getattr(config, "ollama", None), "url", None)
    if not url:
        return _skip("no Ollama url configured")
    try:
        import json
        import urllib.error
        import urllib.request
        from datetime import datetime, timezone
        from urllib.parse import urlsplit
        from sonder_runtime.adapters.inference import ollama_endpoint
    except Exception as exc:  # pragma: no cover - import guard
        return _skip("Ollama transport unavailable (%s)" % exc)

    host = urlsplit(url).hostname or ""
    ps_url = url.rstrip("/") + "/api/ps"
    try:
        request = urllib.request.Request(ps_url, method="GET")
        with ollama_endpoint.open_url(
            request, timeout=timeout,
            allow_remote=getattr(config.ollama, "allow_remote", False) is True,
        ) as response:
            if response.status != 200:
                return _skip("%s: /api/ps returned HTTP %s" % (host, response.status))
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _skip("%s: /api/ps unreachable (%s)" % (host, exc))

    models = payload.get("models") or []
    if not models:
        return {"status": STATUS_OK, "detail": "%s: no models resident" % host}

    now = datetime.now(timezone.utc)
    stale: list[str] = []
    for model in models:
        name = model.get("name") or model.get("model") or "?"
        expires_at = model.get("expires_at")
        if not expires_at:
            continue
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            stale.append(str(name))

    if stale:
        return {
            "status": STATUS_WARN,
            "detail": (
                "%s: %d/%d resident model(s) past keep_alive expiry "
                "(stuck in VRAM?): %s"
            ) % (host, len(stale), len(models), _bounded_join(stale)),
        }
    return {
        "status": STATUS_OK,
        "detail": "%s: %d model(s) resident, all within keep_alive" % (
            host, len(models)
        ),
    }


def _inference_binding(env=None):
    """Return ``(bindings, None)`` or ``(None, fail entry)``; never raises."""
    from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

    try:
        return provider_bindings_from_env(env), None
    except ValueError as exc:
        return None, {
            "status": STATUS_FAIL,
            "detail": "invalid provider bindings: %s" % exc,
        }


def _check_sonder_inference(*, env=None, gateway=None) -> dict:
    """Probe the Sonder Inference provider when any binding uses it.

    Read-only: one cached, 2-second-bounded GET of ``/v1/sonder/health`` (and
    ``/v1/sonder/identity`` when ready).  It never generates.

    * skipped -- no tier, default or embedding binding names sonder_inference;
    * ok      -- the server is ready and speaks API version 1;
    * warn    -- the mock backend is served (synthetic output), or the server
                 is unreachable but ``SONDER_INFERENCE_FALLBACK=ollama`` will
                 carry requests it never received;
    * fail    -- bound and unreachable without a fallback, an API version
                 mismatch, or refused credentials.
    """
    bindings, failure = _inference_binding(env)
    if failure is not None:
        return failure
    if "sonder_inference" not in bindings.bound_providers:
        return _skip("not configured (no provider binding uses sonder_inference)")
    try:
        from sonder_runtime.adapters.inference.sonder_inference_gateway import (
            SonderInferenceGateway,
        )
    except Exception as exc:  # pragma: no cover - import guard
        return _skip("Sonder Inference adapter unavailable (%s)" % exc)
    gateway = gateway if gateway is not None else SonderInferenceGateway(env=env)
    entry = gateway.provider_status()["sonder_inference"]
    fallback = bindings.fallbacks.get("sonder_inference")
    where = entry.get("base_url") or "unconfigured endpoint"
    detail = "%s: %s" % (where, entry.get("detail") or entry.get("state"))
    api_version = entry.get("api_version")
    if api_version is not None and api_version != 1:
        return {"status": STATUS_FAIL, "detail": detail}
    if entry.get("state") == "ready":
        if entry.get("synthetic") is True:
            return {
                "status": STATUS_WARN,
                "detail": detail + " -- MOCK backend: synthetic output, not a "
                "quality or performance signal",
            }
        return {"status": STATUS_OK, "detail": detail}
    if fallback is not None:
        return {
            "status": STATUS_WARN,
            "detail": detail + " -- requests it never receives fall back to %s"
            % fallback,
        }
    return {
        "status": STATUS_FAIL,
        "detail": detail + " -- start `sonder-infer serve` or set "
        "SONDER_INFERENCE_FALLBACK=ollama",
    }


def _check_sonder_inference_scope(*, env=None) -> dict:
    """Say plainly which surfaces a sonder_inference binding does not reach.

    Provider bindings are honoured by ModelGateway consumers only.  The REPL,
    MCP, autopilot and fleet still generate through the legacy Ollama path, so
    an operator who bound every tier to Sonder Inference must not assume
    those surfaces stopped using Ollama.
    """
    bindings, failure = _inference_binding(env)
    if failure is not None:
        return failure
    if "sonder_inference" not in bindings.bound_providers:
        return _skip("not configured (no provider binding uses sonder_inference)")
    return {
        "status": STATUS_WARN,
        "detail": (
            "REPL, MCP, autopilot and fleet generate through the legacy "
            "Ollama path regardless of provider bindings; only ModelGateway "
            "consumers use sonder_inference"
        ),
    }


def sonder_inference_checks(env=None) -> list[tuple[str, CheckCallable]]:
    """Bind the Sonder Inference checks to one environment snapshot."""
    return [
        ("sonder_inference", lambda: _check_sonder_inference(env=env)),
        ("sonder_inference_scope", lambda: _check_sonder_inference_scope(env=env)),
    ]


def storage_checks(
    config=None, *, throughput: bool = False, discover_models: bool = True,
):
    """Build storage checks for a validated config without running them yet.

    ``discover_models`` lets ``storage_models`` ask a loopback Ollama daemon
    which model root it really uses (read-only ``/api/tags`` + ``/api/show``).
    ``OLLAMA_MODELS`` is the daemon's setting; this process's copy of it is
    often absent or different, so without discovery the reported root is an
    assumption and is labelled as one.
    """
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

        import os

        cfg = loaded_config()
        process_root = os.environ.get("OLLAMA_MODELS", "").strip()
        discovered = None
        if discover_models and getattr(
            getattr(cfg, "membership", None), "mode", "static"
        ) != "external":
            import sonder_runtime.adapters.inference.ollama_model_root as ollama_model_root

            discovered = ollama_model_root.discover_daemon_model_root(
                getattr(cfg.ollama, "url", ""),
                allow_remote=getattr(cfg.ollama, "allow_remote", False) is True,
            )
        notes: list[str] = []
        if discovered:
            roots = (discovered,)
            notes.append("reported by the local Ollama daemon")
            if process_root and (
                os.path.normcase(os.path.abspath(os.path.expanduser(process_root)))
                != os.path.normcase(os.path.abspath(discovered))
            ):
                notes.append(
                    "OLLAMA_MODELS in this process (%s) differs from the "
                    "daemon's root" % process_root
                )
        else:
            roots = sonder_storage.model_roots()
            notes.append(
                "from OLLAMA_MODELS in this process; daemon root not verified"
                if process_root else
                "assumed Ollama default: OLLAMA_MODELS is unset in this process "
                "and the daemon's root was not discovered"
            )
        records = [
            sonder_storage.inspect_root(
                root,
                minimum_free_bytes=cfg.state.minimum_free_disk_bytes,
                role="models",
            )
            for root in roots
        ]
        mismatch = len(notes) > 1
        status = (
            STATUS_WARN
            if mismatch or any(r["warnings"] for r in records)
            else STATUS_OK
        )
        return {
            "status": status,
            "detail": "%s [%s]" % (
                " | ".join(sonder_storage.summarize(r) for r in records),
                "; ".join(notes),
            ),
        }

    return [("storage_state", state_check), ("storage_models", models_check)]


def ollama_checks(config) -> list[tuple[str, CheckCallable]]:
    """Bind the Ollama probes to one already-validated configuration.

    The unbound defaults reload configuration from the environment, which
    ignores ``--config``/``--set`` and probes a different endpoint than the
    one the ``config`` line of the same report names.
    """
    return [
        ("ollama", lambda: _check_ollama(config=config)),
        ("ollama_workers", lambda: _check_ollama_workers(config=config)),
        ("ollama_residency", lambda: _check_ollama_residency(config=config)),
    ]


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
        ("schema_epoch", schema_epoch_check()),
        ("backup", backup_check()),
        ("self_heal", _check_self_heal),
        ("memory_quality", _check_memory_quality),
        ("runtime_policy", _check_runtime_policy),
        ("ollama", _check_ollama),
        ("ollama_workers", _check_ollama_workers),
        ("ollama_residency", _check_ollama_residency),
        ("sonder_inference", _check_sonder_inference),
        ("sonder_inference_scope", _check_sonder_inference_scope),
    ]


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import sys

    _report = run_doctor()
    print(render_report(_report))
    # Exit non-zero when the consolidated health is failing so scripts and
    # CI can gate on it; a warn state is informational and still exits 0.
    sys.exit(1 if _report.get("overall") == STATUS_FAIL else 0)
