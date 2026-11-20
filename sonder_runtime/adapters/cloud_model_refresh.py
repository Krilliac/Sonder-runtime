"""Refresh Sonder's Ollama cloud models: retire, record the catalog, rebind from the ladder.

One run (``run_refresh``) reads the shipped model lists, the operator overlay,
the operator preferences and the refresh state; probes every known cloud
model with Ollama ``/api/show`` (metadata only, never a prompt); optionally
reads the public ollama.com catalog; and plans:

* retirements, written to the overlay ``retired_cloud_models`` as the union
  of shipped + overlay + new (a list in the overlay replaces the whole key);
* rebinds of a retired tier default to the first live, pulled entry of the
  operator ladder in ``cloud_model_preferences.json`` (never anything else,
  never an environment binding);
* opt-in pulls of missing ladder stubs (``auto_pull_stubs``, default off).

A dry run (the default) writes no overlay, state, backup or change-log file;
it still takes the run lock file.  An apply run writes the overlay
under the same lock as ``/runtime models set``, each file through
``write_json_validated`` (validated by loading it back through the real
model-list loader before the replace, previous copy kept as ``.bak``), then
the state file, then appends the JSONL change log.  Every network read happens
before the first write, so an Ollama failure leaves every file unchanged; a
catalog failure discards only the catalog part unless
``CATALOG_FAILURE_ABORTS_RUN`` is set.

Nothing here enables cloud opt-in, writes runtime policy or permission state,
or reaches a chat/generate/embed endpoint.  Reports carry reason codes and
parsed names only, never response bodies.

Thread affinity: ``run_refresh`` may run on any thread; concurrent runs (any
process) are serialized by the run lock and the losers report ``busy``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.model_lists as model_lists
import sonder_runtime.adapters.ollama_cloud_catalog as catalog_client
import sonder_runtime.domain.cloud_model_refresh as policy
from sonder_runtime.adapters.filesystem import atomic_json
from sonder_runtime.adapters.inference.ollama_cloud_metadata import (
    OllamaCloudMetadata,
    OllamaPullFailed,
    OllamaTransportError,
)
from sonder_runtime.platform import paths as runtime_paths

logger = logging.getLogger(__name__)

PREFERENCES_FILE = "cloud_model_preferences.json"
STATE_FILE = "cloud_model_catalog.json"
CHANGE_LOG = "cloud_model_changes.jsonl"
RUN_LOCK = "cloud_model_refresh.lock"
ENV_PREFERENCES = "SONDER_CLOUD_MODEL_PREFERENCES"
ENV_STATE = "SONDER_CLOUD_MODEL_STATE"
# N2: a catalog failure discards only the catalog part of the run.
CATALOG_FAILURE_ABORTS_RUN = False
# An aborted run writes nothing at all, not even a change-log line.
LOG_ABORTED_RUNS = False
REFRESH_INTERVAL_SECONDS = 24 * 3600
MAX_LOG_BYTES = 1024 * 1024
RUN_LOCK_TIMEOUT = 0.5
OVERLAY_LOCK_TIMEOUT = 10.0
LIVE_EFFECT = (
    "running processes pick up these defaults on their next tier read; local tiers"
    " and other startup bindings still need a restart"
)
_REASON_CHARS = 200
APPLY_NOTE = (
    "apply via the CLI (python -m sonder_runtime models cloud-refresh --apply)"
    " or the daily schedule"
)


@dataclass(frozen=True)
class RefreshPaths:
    """Every file one refresh may read or write; immutable."""

    overlay: Path
    overlay_backup: Path
    preferences: Path
    state: Path
    state_backup: Path
    change_log: Path
    run_lock: Path

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> RefreshPaths:
        overlay = model_lists.overlay_path()

        def place(name: str, env_var: str = "") -> Path:
            if home:
                return Path(home) / name
            return Path(runtime_paths.state_path(name, env_var))

        state = place(STATE_FILE, ENV_STATE)
        return cls(
            overlay=overlay,
            overlay_backup=overlay.with_name(overlay.name + ".bak"),
            preferences=place(PREFERENCES_FILE, ENV_PREFERENCES),
            state=state,
            state_backup=state.with_name(state.name + ".bak"),
            change_log=place(CHANGE_LOG),
            run_lock=place(RUN_LOCK),
        )

    def describe(self) -> dict[str, str]:
        return {
            "overlay": str(self.overlay),
            "preferences": str(self.preferences),
            "state": str(self.state),
            "change_log": str(self.change_log),
        }


def _bounded(text: str) -> str:
    return text[:_REASON_CHARS]


def load_preferences(path: Path) -> tuple[policy.Preferences | None, str | None]:
    """``(preferences, None)``, ``(None, None)`` when absent, ``(None, reason)`` when invalid."""
    try:
        document = model_lists.read_document(Path(path))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError) as exc:
        return None, _bounded("preferences_unreadable: %s" % type(exc).__name__)
    try:
        return policy.parse_preferences(document), None
    except ValueError as exc:
        return None, _bounded("preferences_invalid: %s" % exc)


def load_state(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(state, None)``, ``(None, None)`` when absent, ``(None, reason)`` when invalid."""
    try:
        document = model_lists.read_document(Path(path))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError) as exc:
        return None, _bounded("state_unreadable: %s" % type(exc).__name__)
    try:
        policy.validate_state(document)
    except ValueError as exc:
        return None, _bounded("state_invalid: %s" % exc)
    assert isinstance(document, dict)
    return document, None


class _RunStopped(Exception):
    """Internal control flow: end the run with ``outcome``/``reason``."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


def _read_overlay(path: Path, shipped: dict[str, Any]) -> tuple[Any, bytes | None]:
    """``(document, raw bytes)`` of a valid overlay, ``(None, None)`` when absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise _RunStopped("refused", "overlay_unreadable") from exc
    try:
        document = model_lists.read_document(path)
        model_lists.merge(shipped, document)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise _RunStopped("refused", "overlay_invalid") from exc
    return document, raw


def _change_dict(change: policy.Change) -> dict[str, Any]:
    return {
        "action": change.action,
        "key": change.key,
        "before": change.before,
        "after": change.after,
        "model": change.model,
        "evidence": dict(change.evidence) if change.evidence is not None else None,
    }


def _decision_dict(decision: policy.TierDecision) -> dict[str, Any]:
    return {
        "tier": decision.tier,
        "effective_model": decision.effective_model,
        "effective_source": decision.effective_source,
        "action": decision.action,
        "new_model": decision.new_model,
        "suggestion": decision.suggestion,
        "ladder": [{"model": e.model, "state": e.state} for e in decision.ladder],
        "note": decision.note,
    }


def _rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        return


def _append_log(path: Path, lines: Sequence[Mapping[str, Any]]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(path)
    with path.open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_overlay(path: Path, document: Mapping[str, Any], backup: Path) -> None:
    if path.resolve() == model_lists.SHIPPED_PATH.resolve():
        raise _RunStopped("refused", "overlay_is_shipped_file")
    try:
        atomic_json.write_json_validated(
            path, dict(document), validate=model_lists.validate_overlay_file, backup=backup
        )
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise _RunStopped("refused", "overlay_validation_failed") from exc
    finally:
        model_lists.clear_cache()


def _restore_overlay(path: Path, previous: bytes | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        temporary = path.with_name(path.name + ".restore-" + secrets.token_hex(8))
        try:
            temporary.write_bytes(previous)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    model_lists.clear_cache()


def _write_state(path: Path, document: Mapping[str, Any], backup: Path) -> None:
    policy.validate_state(document)

    def check(candidate: Path) -> None:
        policy.validate_state(model_lists.read_document(candidate))

    atomic_json.write_json_validated(path, dict(document), validate=check, backup=backup)


def _known_family_times(state: Mapping[str, Any] | None) -> dict[str, float]:
    catalog = (state or {}).get("catalog")
    families = catalog.get("families") if isinstance(catalog, Mapping) else None
    known: dict[str, float] = {}
    if isinstance(families, Mapping):
        for family, entry in families.items():
            stamp = policy.parse_timestamp(entry.get("tags_fetched_at")) if isinstance(entry, Mapping) else None
            if isinstance(family, str) and stamp is not None:
                known[family] = stamp
    return known


def _state_catalog_tags(state: Mapping[str, Any] | None) -> dict[str, list[str]]:
    catalog = (state or {}).get("catalog")
    families = catalog.get("families") if isinstance(catalog, Mapping) else None
    tags: dict[str, list[str]] = {}
    if isinstance(families, Mapping):
        for family, entry in families.items():
            if isinstance(entry, Mapping) and isinstance(entry.get("tags"), list):
                tags[str(family)] = [str(tag) for tag in entry["tags"]]
    return tags


class _RefreshRun:
    """State of one locked run; owned by the calling thread, discarded after ``execute``."""

    def __init__(
        self,
        *,
        paths: RefreshPaths,
        apply: bool,
        trigger: str,
        env: Mapping[str, str],
        metadata: OllamaCloudMetadata,
        catalog_fetcher: Callable[..., catalog_client.CatalogSnapshot],
        use_catalog: bool | None,
        clock: Callable[[], float],
        report: dict[str, Any],
    ) -> None:
        self.paths = paths
        self.apply = apply
        self.trigger = trigger
        self.env = env
        self.metadata = metadata
        self.catalog_fetcher = catalog_fetcher
        self.use_catalog = use_catalog
        self.clock = clock
        self.report = report
        self.log_lines: list[dict[str, Any]] = []

    # -- reads (no writes) ----------------------------------------------------

    def _tags(self) -> tuple[dict[str, Any], ...]:
        try:
            return self.metadata.tags()
        except OllamaTransportError as exc:
            raise _RunStopped("aborted", exc.reason) from exc

    def _catalog(
        self, prefs: policy.Preferences | None, state: Mapping[str, Any] | None
    ) -> catalog_client.CatalogSnapshot | None:
        wanted = self.use_catalog is not False and (prefs is None or prefs.catalog)
        try:
            allowed = wanted and catalog_client.enabled(self.env)
        except ImportError:
            allowed = False
        if not allowed:
            self.report["catalog"]["status"] = "disabled"
            return None
        try:
            snapshot = self.catalog_fetcher(known_families=_known_family_times(state), now=self.clock())
        except catalog_client.CatalogUnavailable as exc:
            if CATALOG_FAILURE_ABORTS_RUN:
                raise _RunStopped("aborted", "catalog_unavailable_" + exc.reason) from exc
            self.report["catalog"].update(status="unavailable", reason=exc.reason)
            return None
        self.report["catalog"]["status"] = snapshot.status
        return None if snapshot.status == "unparsed" else snapshot

    def _catalog_tags(
        self, snapshot: catalog_client.CatalogSnapshot | None, state: Mapping[str, Any] | None
    ) -> list[str]:
        if snapshot is None:
            return []
        previous = _state_catalog_tags(state)
        tags: list[str] = []
        for family, fresh in snapshot.families.items():
            names = fresh if family in snapshot.refreshed else previous.get(family, [])
            tags.extend(names)
        catalog = self.report["catalog"]
        catalog["families"] = len(snapshot.families)
        catalog["new_families"] = sorted(set(snapshot.families) - set(previous))
        known_tags = {tag for names in previous.values() for tag in names}
        catalog["new_tags"] = sorted(set(tags) - known_tags)[: policy.MAX_PROBES]
        return tags

    def _probe(self, known: Sequence[policy.KnownModel]) -> dict[str, policy.ProbeResult]:
        probes: dict[str, policy.ProbeResult] = {}
        for model in known:
            if not model.probe:
                continue
            try:
                probes[model.name] = self.metadata.show(model.name)
            except OllamaTransportError as exc:
                raise _RunStopped("aborted", exc.reason) from exc
        self.report["probed"] = len(probes)
        self.report["unprobed"] = [model.name for model in known if not model.probe]
        self.report["probes"] = [
            {"model": name, "status": probe.status.value, "detail": probe.detail}
            for name, probe in probes.items()
        ]
        return probes

    # -- pulls (Ollama side effect, before any file write) ----------------------

    def _pull(self, plan: policy.RefreshPlan) -> frozenset[str]:
        rows: list[dict[str, Any]] = [
            {"model": name, "outcome": "refused"} for name in plan.refused_pulls
        ]
        self.report["pulls"] = rows
        if not self.apply:
            rows.extend({"model": name, "outcome": "planned"} for name in plan.pulls)
            return frozenset(name.casefold() for name in plan.pulls)
        pulled: set[str] = set()
        for name in plan.pulls:
            try:
                self.metadata.pull_stub(name)
            except OllamaTransportError as exc:
                rows.append({"model": name, "outcome": "failed"})
                raise _RunStopped("aborted", exc.reason) from exc
            except ValueError:
                rows.append({"model": name, "outcome": "refused"})
                continue
            except OllamaPullFailed:
                rows.append({"model": name, "outcome": "failed"})
                continue
            rows.append({"model": name, "outcome": "ok"})
            pulled.add(name.casefold())
        return frozenset(pulled)

    def _log_pulls(self) -> None:
        for row in self.report["pulls"]:
            if row["outcome"] != "planned":
                self._log("pull", file=None, key=None, before=None, after=row["outcome"],
                          model=row["model"], evidence=None, backup=None)

    # -- the run --------------------------------------------------------------

    def execute(self) -> None:
        shipped = model_lists.load_shipped_document()
        overlay, _raw = _read_overlay(self.paths.overlay, shipped)
        effective = model_lists.merge(shipped, overlay if overlay is not None else {"version": 1})
        prefs, prefs_error = load_preferences(self.paths.preferences)
        state, state_error = load_state(self.paths.state)
        self.report.update(preferences_error=prefs_error, state_error=state_error)
        if prefs_error:
            self.report["notes"].append("preferences ignored (%s): no rebind, no pull" % prefs_error)
        rows = self._tags()
        snapshot = self._catalog(prefs, state)
        previous_models = (state or {}).get("models") or {}
        known = policy.collect_known_models(
            shipped=shipped, effective=effective, env=self.env, pulled=rows, prefs=prefs,
            catalog_tags=self._catalog_tags(snapshot, state), state_models=previous_models,
            now=self.clock(),
        )
        probes = self._probe(known)
        inputs = policy.RefreshInputs(
            shipped=shipped, overlay=overlay, effective=effective, env=self.env, prefs=prefs,
            known=known, probes=probes,
            pulled_before=frozenset(str(row["name"]).casefold() for row in rows),
            catalog_families=snapshot.families if snapshot is not None else None,
        )
        try:
            pulled_now = self._pull(policy.plan_refresh(inputs))
        finally:
            if self.apply:
                self._log_pulls()
        plan = policy.plan_refresh(dataclasses.replace(inputs, pulled_now=pulled_now))
        observed = {tier: str(effective["default_cloud_models"][tier]) for tier in policy.CLOUD_TIERS}
        self._report_plan(plan, known, previous_models)
        if not self.apply:
            _doc, planned = policy.apply_to_overlay(
                overlay if overlay is not None else {"version": 1}, plan,
                shipped=shipped, observed_defaults=observed,
            )
            self.report["overlay_changes"] = [_change_dict(c) for c in planned]
            self.report["notes"].append("dry run: nothing written; re-run with --apply")
            return
        applied = self._apply_overlay(plan, shipped, observed)
        self._apply_state(state, known, probes, plan, applied, snapshot)

    def _report_plan(
        self,
        plan: policy.RefreshPlan,
        known: Sequence[policy.KnownModel],
        previous_models: Mapping[str, Any],
    ) -> None:
        self.report["retirements"] = [
            {"model": name, **retirement.evidence()} for name, retirement in plan.retire_add
        ]
        self.report["retired_after"] = list(plan.retired_after)
        self.report["write_retired"] = plan.write_retired
        self.report["decisions"] = [_decision_dict(d) for d in plan.decisions]
        self.report["pending_choices"] = [
            _decision_dict(d) for d in plan.decisions if d.action == "needs_operator_choice"
        ]
        self.report["new_models"] = [
            model.name for model in known
            if model.name not in previous_models and model.sources & {"catalog", "pulled"}
        ]
        self.report["notes"].extend(plan.notes)

    def _apply_overlay(
        self, plan: policy.RefreshPlan, shipped: dict[str, Any], observed: Mapping[str, str]
    ) -> tuple[policy.Change, ...]:
        path = self.paths.overlay
        with atomic_json.file_lock(path, timeout=OVERLAY_LOCK_TIMEOUT):
            current, previous_bytes = _read_overlay(path, shipped)
            base = current if current is not None else {"version": model_lists.SCHEMA_VERSION}
            document, changes = policy.apply_to_overlay(
                base, plan, shipped=shipped, observed_defaults=observed
            )
            edits = tuple(c for c in changes if c.action != "changed_concurrently")
            self.report["changed_concurrently"] = [
                c.key for c in changes if c.action == "changed_concurrently"
            ]
            if not edits or document == base:
                self.report["overlay_changes"] = []
                return ()
            _write_overlay(path, document, self.paths.overlay_backup)
            if model_lists.load().issues:
                _restore_overlay(path, previous_bytes)
                raise _RunStopped("refused", "post_check_failed")
        backup = str(self.paths.overlay_backup) if previous_bytes is not None else None
        self.report["files_written"].append({"file": str(path), "backup": backup})
        if current is None:
            edits = (policy.Change("create_overlay", "", None, None, None, None), *edits)
        self.report["overlay_changes"] = [_change_dict(c) for c in edits]
        for change in edits:
            self._log(change.action, file=str(path), key=change.key or None, before=change.before,
                      after=change.after, model=change.model,
                      evidence=dict(change.evidence) if change.evidence is not None else None,
                      backup=backup)
        return edits

    def _apply_state(
        self,
        state: Mapping[str, Any] | None,
        known: Sequence[policy.KnownModel],
        probes: Mapping[str, policy.ProbeResult],
        plan: policy.RefreshPlan,
        applied: Sequence[policy.Change],
        snapshot: catalog_client.CatalogSnapshot | None,
    ) -> None:
        now_iso = policy.iso_timestamp(self.clock())
        last_refresh = {
            "run_id": self.report["run_id"], "trigger": self.trigger, "mode": "apply",
            "started_at": self.report["started_at"], "finished_at": now_iso,
            "outcome": "ok", "reason": None, "catalog": self.report["catalog"]["status"],
            "probed": self.report["probed"],
            "retired_added": [c.model for c in applied if c.action == "retire"],
            "rebinds": [
                {"tier": c.key.rpartition(".")[2], "from": c.before, "to": c.after,
                 "ladder_index": (c.evidence or {}).get("ladder_index")}
                for c in applied if c.action == "rebind"
            ],
            "pulls": list(self.report["pulls"]),
            "new_models": list(self.report["new_models"])[: policy.MAX_PROBES],
        }
        document = policy.merge_state(
            state, now_iso=now_iso, run_id=self.report["run_id"], known=known, probes=probes,
            plan=plan, applied_rebinds=applied,
            catalog=snapshot.families if snapshot is not None else None,
            catalog_refreshed=snapshot.refreshed if snapshot is not None else (),
            catalog_source=catalog_client.SEARCH_URL, last_refresh=last_refresh, succeeded=True,
        )
        existed = self.paths.state.exists()
        try:
            _write_state(self.paths.state, document, self.paths.state_backup)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise _RunStopped("aborted", "state_write_failed") from exc
        self.report["files_written"].append(
            {"file": str(self.paths.state),
             "backup": str(self.paths.state_backup) if existed else None}
        )

    def _log(self, action: str, **fields: Any) -> None:
        self.log_lines.append({
            "ts": policy.iso_timestamp(self.clock()), "run_id": self.report["run_id"],
            "trigger": self.trigger, "action": action, **fields,
        })

    def flush_log(self, outcome: str, reason: str | None) -> None:
        """Append this run's change lines plus one ``run`` line (aborted runs: see LOG_ABORTED_RUNS)."""
        if outcome != "ok" and not LOG_ABORTED_RUNS and not self.log_lines:
            return
        self._log("run", file=None, key=None, before=None,
                  after={"outcome": outcome, "reason": reason, "mode": "apply"},
                  model=None, evidence=None, backup=None)
        try:
            _append_log(self.paths.change_log, self.log_lines)
        except OSError:
            logger.warning("cloud model refresh: change log append failed", exc_info=True)
            self.report["notes"].append("change log append failed")
            return
        self.report["files_written"].append({"file": str(self.paths.change_log), "backup": None})


def _new_report(*, apply: bool, trigger: str, started: float) -> dict[str, Any]:
    return {
        "ok": False, "outcome": "aborted", "reason": None,
        "mode": "apply" if apply else "dry_run", "trigger": trigger,
        "run_id": secrets.token_hex(8), "started_at": policy.iso_timestamp(started),
        "finished_at": None, "paths": {},
        "catalog": {"status": "disabled", "note": "inert, nothing bound"},
        "probed": 0, "unprobed": [], "probes": [], "retirements": [], "retired_after": [],
        "write_retired": False, "decisions": [], "pending_choices": [], "pulls": [],
        "overlay_changes": [], "changed_concurrently": [], "new_models": [], "notes": [],
        "preferences_error": None, "state_error": None, "files_written": [],
        "live_effect": LIVE_EFFECT,
    }


@contextlib.contextmanager
def _run_lock(path: Path) -> Iterator[None]:
    with atomic_json.file_lock(path.with_name(path.stem), timeout=RUN_LOCK_TIMEOUT,
                               suffix=path.suffix):
        yield


def _execute(run: _RefreshRun, report: dict[str, Any]) -> None:
    """Run under the lock; record the outcome; append the change log for apply runs."""
    try:
        run.execute()
        report.update(ok=True, outcome="ok", reason=None)
    except _RunStopped as stop:
        report.update(ok=False, outcome=stop.outcome, reason=stop.reason)
    except model_lists.ModelListsConfigurationError:
        report.update(ok=False, outcome="refused", reason="shipped_model_lists_invalid")
    except Exception as exc:  # noqa: BLE001 - a refresh reports, it never takes its host down
        logger.warning("cloud model refresh failed", exc_info=True)
        report.update(ok=False, outcome="aborted", reason="internal_error_" + type(exc).__name__)
    if run.apply:
        run.flush_log(report["outcome"], report["reason"])


def run_refresh(
    *,
    origin: str,
    apply: bool,
    trigger: str = "cli",
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    metadata: OllamaCloudMetadata | None = None,
    catalog_fetcher: Callable[..., catalog_client.CatalogSnapshot] | None = None,
    use_catalog: bool | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run one refresh and return its report; never raises.

    ``outcome`` is ``ok`` (dry run or apply), ``aborted`` (Ollama unreachable,
    zero writes), ``refused`` (bad overlay or failed validation, zero writes)
    or ``busy`` (another run holds the lock).  ``use_catalog=False`` is
    ``--no-catalog``.
    """
    report = _new_report(apply=apply, trigger=trigger, started=clock())
    try:
        paths = RefreshPaths.resolve(home)
        report["paths"] = paths.describe()
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(_run_lock(paths.run_lock))
            except TimeoutError:
                report.update(ok=False, outcome="busy", reason="run_lock_busy")
            else:
                _execute(_RefreshRun(
                    paths=paths, apply=apply, trigger=trigger,
                    env=dict(os.environ if env is None else env),
                    metadata=metadata or OllamaCloudMetadata(origin),
                    catalog_fetcher=catalog_fetcher or catalog_client.fetch_catalog,
                    use_catalog=use_catalog, clock=clock, report=report,
                ), report)
    except Exception as exc:  # noqa: BLE001 - path resolution or lock I/O failed
        logger.warning("cloud model refresh could not start", exc_info=True)
        report.update(ok=False, outcome="aborted", reason="internal_error_" + type(exc).__name__)
    report["finished_at"] = policy.iso_timestamp(clock())
    logger.info(
        "cloud model refresh %s (%s, %s): %s%s", report["run_id"], report["trigger"],
        report["mode"], report["outcome"], " " + report["reason"] if report["reason"] else "",
    )
    return report


# --- read-only status (MCP tool) ---------------------------------------------


def _retired_sources(
    lists: Mapping[str, Any] | None, shipped: Mapping[str, Any] | None, models: Mapping[str, Any]
) -> list[dict[str, str]]:
    if lists is None:
        return []
    shipped_cf = {str(n).casefold() for n in (shipped or {}).get("retired_cloud_models", ())}
    rows: list[dict[str, str]] = []
    for name in lists["retired_cloud_models"]:
        entry = models.get(name)
        if str(name).casefold() in shipped_cf:
            source = "shipped"
        elif isinstance(entry, Mapping) and isinstance(entry.get("retired"), Mapping):
            source = "refresh-evidence"
        else:
            source = "overlay"
        rows.append({"model": str(name), "source": source})
    return rows


def _ladder_status(
    prefs: policy.Preferences | None, models: Mapping[str, Any], retired_cf: set[str]
) -> dict[str, list[dict[str, Any]]]:
    status: dict[str, list[dict[str, Any]]] = {}
    for tier in policy.CLOUD_TIERS:
        rows: list[dict[str, Any]] = []
        for name in policy.ladder(prefs, tier) if prefs is not None else ():
            entry = models.get(name)
            entry = entry if isinstance(entry, Mapping) else {}
            last = entry.get("status")
            pulled = "pulled" in (entry.get("sources") or ())
            retired = name.casefold() in retired_cf or last == "retired"
            rows.append({"model": name, "last_status": last, "pulled": pulled,
                         "retired": retired, "eligible": last == "live" and pulled and not retired})
        status[tier] = rows
    return status


def _effective_tiers(
    lists: Mapping[str, Any] | None, env: Mapping[str, str], retired_cf: set[str]
) -> dict[str, dict[str, Any]]:
    effective: dict[str, dict[str, Any]] = {}
    for tier in policy.CLOUD_TIERS:
        value = str(env.get("SONDER_CLOUD_" + tier.upper(), "") or "").strip()
        if value and value.casefold() not in retired_cf:
            effective[tier] = {"model": value, "source": "env"}
        else:
            default = lists["default_cloud_models"][tier] if lists is not None else None
            effective[tier] = {"model": default, "source": "default"}
    return effective


def _pin_status(
    state: Mapping[str, Any],
    lists: Mapping[str, Any] | None,
    shipped: Mapping[str, Any] | None,
    retired_cf: set[str],
) -> dict[str, dict[str, Any]]:
    pins = state.get("overlay_pins")
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(pins, Mapping):
        return result
    for tier, pin in pins.items():
        if not isinstance(pin, Mapping) or tier not in policy.CLOUD_TIERS:
            continue
        current = lists["default_cloud_models"][tier] if lists is not None else None
        shipped_default = (shipped or {}).get("default_cloud_models", {}).get(tier)
        active = current == pin.get("model")
        result[tier] = {
            **dict(pin),
            "active": active,
            "current_default": current,
            "shipped_default": shipped_default,
            "hides_newer_shipped_default": bool(
                active
                and shipped_default
                and shipped_default != pin.get("replaced")
                and str(shipped_default).casefold() not in retired_cf
            ),
        }
    return result


def status_payload(
    *, home: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read-only refresh status from the state files, the model lists and ``env``.

    No network and no file writes; safe on any thread.
    """
    environment = os.environ if env is None else env
    paths = RefreshPaths.resolve(home)
    prefs, prefs_error = load_preferences(paths.preferences)
    state, state_error = load_state(paths.state)
    issues: list[str] = []
    lists: Mapping[str, Any] | None = None
    shipped: Mapping[str, Any] | None = None
    try:
        config = model_lists.load()
        lists = config.values
        shipped = model_lists.load_shipped_document()
        issues = [issue.text() for issue in config.issues]
    except model_lists.ModelListsConfigurationError as exc:
        issues = [str(exc)]
    retired_cf = {str(n).casefold() for n in (lists or {}).get("retired_cloud_models", ())}
    current: Mapping[str, Any] = state or {}
    models = current.get("models") or {}
    last_success = current.get("last_success_at")
    stamp = policy.parse_timestamp(last_success)
    last_refresh = current.get("last_refresh") or {}
    notes = [APPLY_NOTE]
    if state is None:
        notes.append("never refreshed")
    return {
        "ok": True,
        "last_refresh": last_refresh or None,
        "last_success_at": last_success,
        "next_due_after": (
            policy.iso_timestamp(stamp + REFRESH_INTERVAL_SECONDS) if stamp is not None else None
        ),
        "retired": _retired_sources(lists, shipped, models),
        "new_models": list(last_refresh.get("new_models") or []),
        "ladder": _ladder_status(prefs, models, retired_cf),
        "pending_choices": list(current.get("pending_choices") or []),
        "overlay_pins": _pin_status(current, lists, shipped, retired_cf),
        "effective": _effective_tiers(lists, environment, retired_cf),
        "auto_pull_stubs": prefs.auto_pull_stubs if prefs is not None else False,
        "auto_refresh": prefs.auto_refresh if prefs is not None else True,
        "preferences_error": prefs_error,
        "state_error": state_error,
        "model_lists_issues": issues,
        "paths": paths.describe(),
        "notes": notes,
    }


__all__ = [
    "CATALOG_FAILURE_ABORTS_RUN",
    "CHANGE_LOG",
    "ENV_PREFERENCES",
    "ENV_STATE",
    "LIVE_EFFECT",
    "LOG_ABORTED_RUNS",
    "PREFERENCES_FILE",
    "REFRESH_INTERVAL_SECONDS",
    "RUN_LOCK",
    "RefreshPaths",
    "STATE_FILE",
    "load_preferences",
    "load_state",
    "run_refresh",
    "status_payload",
]
