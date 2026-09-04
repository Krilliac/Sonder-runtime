"""Policy helpers for read-only bootstrap health checks.

The bootstrap doctor boundary owns how self-heal findings become a doctor
result.  The legacy self-heal module and environment selection remain injected
by the compatibility entrypoint so this packaged boundary does not acquire a
root-module dependency.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)


def bounded_join(items: Sequence[str], *, limit: int = 8) -> str:
    """Join ``items`` with a bound on how many are rendered.

    A doctor detail line must stay legible even when a deployment configures
    many worker endpoints or has many resident models. Beyond ``limit`` this
    appends a ``+N more`` marker instead of growing the line unboundedly.
    """
    values = list(items)
    if len(values) <= limit:
        return ", ".join(values)
    shown = ", ".join(values[:limit])
    return "%s, +%d more" % (shown, len(values) - limit)


def summarize_self_heal(
    check: Callable[[str], Iterable[Any]],
    db_path: str,
) -> dict[str, str]:
    """Run an injected self-heal inspection and summarize its findings.

    This function is deliberately read-only: ``check`` is the inspection
    collaborator, and no repair operation is exposed or invoked here.
    """
    logger.debug(f"summarize_self_heal: running check against db_path={db_path!r}")
    try:
        issues = check(db_path)
    except Exception as exc:  # noqa: BLE001 - a doctor check must not abort
        logger.error(f"self-heal check failed, degrading to skipped", exc_info=True)
        logger.warning(f"self-heal check raised an exception, degrading to skipped: {type(exc).__name__}")
        logger.debug(f"summarize_self_heal: check raised: {exc}")
        return {
            "status": "skipped",
            "detail": "self-heal check failed (%s)" % exc,
        }
    issues = tuple(issues)
    repairable = sum(1 for issue in issues if getattr(issue, "repairable", False))
    logger.info(f"self-heal check completed, issues={len(issues)}, repairable={repairable}")
    logger.debug(f"summarize_self_heal: found {len(issues)} issue(s), {repairable} repairable")
    if not issues:
        return {"status": "ok", "detail": "no issues"}
    status = "warn" if repairable == len(issues) else "fail"
    if repairable > 0:
        logger.warning(f"self-heal found {len(issues)} issue(s), {repairable} repairable -- consider running repair")
    return {
        "status": status,
        "detail": "%d issue(s), %d repairable" % (len(issues), repairable),
    }


def summarize_worker_probe(
    up: Sequence[str],
    down: Sequence[str],
    total: int,
) -> dict[str, str]:
    """Roll up per-worker Ollama probe results into one doctor status.

    ``up`` and ``down`` hold ``"host: detail"`` strings for the workers that
    answered and the ones that did not; ``total`` is the configured worker
    count (``len(up) + len(down)`` may be less than ``total`` only if a caller
    mis-tallies, so callers must account for every worker in one bucket).
    """
    logger.info(f"worker probe completed, up={len(up)}/{total}, down={len(down)}/{total}")
    logger.debug(f"summarize_worker_probe: up={len(up)}, down={len(down)}, total={total}")
    if not up:
        logger.critical(f"all {total} configured worker(s) are unreachable -- no inference capacity available, pool is fully degraded")
        logger.error(f"all {total} configured worker(s) are unreachable, inference pool is fully degraded")
        logger.warning(f"all {total} configured worker(s) are unreachable, pool is fully degraded")
        return {
            "status": "fail",
            "detail": "%d/%d worker(s) unreachable: %s" % (
                len(down), total, bounded_join(down)
            ),
        }
    if down:
        logger.warning(f"pool running with fewer workers than configured: {len(up)}/{total} reachable, {len(down)} unreachable")
        return {
            "status": "warn",
            "detail": "%d/%d worker(s) unreachable: %s | reachable: %s" % (
                len(down), total, bounded_join(down), bounded_join(up)
            ),
        }
    return {
        "status": "ok",
        "detail": "%d worker(s) reachable: %s" % (total, bounded_join(up)),
    }


def summarize_memory_quality(
    connect: Callable[[str], Any],
    audit: Callable[[Any], Mapping[str, Any]],
    db_path: str,
) -> dict[str, str]:
    """Run and summarize a read-only memory-quality audit.

    ``connect`` and ``audit`` are injected so this bootstrap policy owns the
    diagnostic decision without importing legacy root modules.  The opened
    connection is always closed, including when the audit raises.
    """
    logger.debug(f"summarize_memory_quality: auditing db_path={db_path!r}")
    try:
        conn = connect(db_path)
    except Exception as exc:  # noqa: BLE001 - doctor checks must degrade safely
        logger.error(f"memory quality check cannot open database at db_path={db_path!r}, degrading to skipped", exc_info=True)
        logger.warning(f"memory quality check cannot open database, degrading to skipped: {type(exc).__name__}")
        return {"status": "skipped", "detail": "cannot open memory db (%s)" % exc}
    try:
        findings = audit(conn)
    except Exception as exc:  # noqa: BLE001 - doctor checks must degrade safely
        logger.error(f"memory quality audit failed, degrading to skipped", exc_info=True)
        logger.warning(f"memory quality audit failed, degrading to skipped: {type(exc).__name__}")
        return {"status": "skipped", "detail": "audit failed (%s)" % exc}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info(f"memory quality audit completed, findings={len(findings)}")
    logger.debug(f"summarize_memory_quality: audit returned {len(findings)} finding keys")
    severe = sum(
        int(findings.get(name, 0))
        for name in ("path_or_secret_like", "missing_fts", "orphan_fts")
    )
    hygiene = sum(
        int(findings.get(name, 0))
        for name in (
            "exact_duplicate_prunable",
            "no_embedding",
            "vague_without_anchor",
        )
    )
    total = int(findings.get("total_lessons", 0))
    if severe:
        logger.warning(f"memory quality audit found {severe} severe issue(s) in {total} lessons -- investigate path/secret leaks or FTS corruption")
        return {
            "status": "fail",
            "detail": "%d lessons, %d severe issue(s)" % (total, severe),
        }
    if hygiene:
        logger.warning(f"memory quality audit found {hygiene} hygiene issue(s) in {total} lessons -- consider running quality repair")
        return {
            "status": "warn",
            "detail": "%d lessons, %d hygiene issue(s)" % (total, hygiene),
        }
    return {"status": "ok", "detail": "%d lessons clean" % total}


__all__ = [
    "bounded_join",
    "summarize_memory_quality",
    "summarize_self_heal",
    "summarize_worker_probe",
]
