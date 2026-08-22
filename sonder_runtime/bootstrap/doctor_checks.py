"""Policy helpers for read-only bootstrap health checks.

The bootstrap doctor boundary owns how self-heal findings become a doctor
result.  The legacy self-heal module and environment selection remain injected
by the compatibility entrypoint so this packaged boundary does not acquire a
root-module dependency.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


def summarize_self_heal(
    check: Callable[[str], Iterable[Any]],
    db_path: str,
) -> dict[str, str]:
    """Run an injected self-heal inspection and summarize its findings.

    This function is deliberately read-only: ``check`` is the inspection
    collaborator, and no repair operation is exposed or invoked here.
    """
    try:
        issues = check(db_path)
    except Exception as exc:  # noqa: BLE001 - a doctor check must not abort
        return {
            "status": "skipped",
            "detail": "self-heal check failed (%s)" % exc,
        }
    issues = tuple(issues)
    if not issues:
        return {"status": "ok", "detail": "no issues"}
    repairable = sum(1 for issue in issues if getattr(issue, "repairable", False))
    status = "warn" if repairable == len(issues) else "fail"
    return {
        "status": status,
        "detail": "%d issue(s), %d repairable" % (len(issues), repairable),
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
    try:
        conn = connect(db_path)
    except Exception as exc:  # noqa: BLE001 - doctor checks must degrade safely
        return {"status": "skipped", "detail": "cannot open memory db (%s)" % exc}
    try:
        findings = audit(conn)
    except Exception as exc:  # noqa: BLE001 - doctor checks must degrade safely
        return {"status": "skipped", "detail": "audit failed (%s)" % exc}
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
        return {
            "status": "fail",
            "detail": "%d lessons, %d severe issue(s)" % (total, severe),
        }
    if hygiene:
        return {
            "status": "warn",
            "detail": "%d lessons, %d hygiene issue(s)" % (total, hygiene),
        }
    return {"status": "ok", "detail": "%d lessons clean" % total}


__all__ = ["summarize_memory_quality", "summarize_self_heal"]
