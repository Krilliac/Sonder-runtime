"""Policy helpers for read-only bootstrap health checks.

The bootstrap doctor boundary owns how self-heal findings become a doctor
result.  The legacy self-heal module and environment selection remain injected
by the compatibility entrypoint so this packaged boundary does not acquire a
root-module dependency.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
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


__all__ = ["summarize_self_heal"]
