"""Pure operator-facing rendering of the runtime source update status.

``git_tools.runtime_update`` remains the authority that repeats every check
before touching a checkout; this module only renders the status dictionary
and the presentation-only eligibility verdict so ``/updatecheck`` and
``/update`` describe the same outcome. It is explicit-input and side-effect
free: the canonical update branch is injected by the caller. Moved from
``server.py`` in the WP1 Three-Hundred-First Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations


def format_runtime_update(data, *, updated=None, update_branch):
    """Format a bounded, operator-facing Git update report.

    ``update_branch`` is the canonical branch the update action may
    fast-forward; the caller injects it so this renderer stays free of the
    Git adapter.
    """
    lines = [
        "Sonder source update status:",
        "  installed: %s (%s)" % (
            str(data.get("installed_commit") or "unknown")[:12],
            data.get("installed_commit_time") or "unknown time",
        ),
        "  newest %sorigin/main: %s (%s)" % (
            "known " if not data.get("remote_ref_refreshed") else "",
            str(data.get("newest_commit") or "unknown")[:12],
            data.get("newest_commit_time") or "unknown time",
        ),
        "  state: %s (behind=%s, ahead=%s; worktree=%s)" % (
            data.get("state") or "unknown", data.get("behind", "?"),
            data.get("ahead", "?"),
            "clean" if data.get("clean") else "dirty",
        ),
        "  checkout: %s (source root: %s)" % (
            data.get("branch") or "detached HEAD",
            data.get("root") or "unknown",
        ),
        "  remote: %s%s" % (
            data.get("remote") or "unknown",
            "" if data.get("trusted_remote") else " [not canonical; update refused]",
        ),
        "  checked: %s" % (data.get("checked_at") or "unknown"),
    ]
    running = str(data.get("running_commit") or "").strip()
    if running:
        lines.insert(2, "  running: %s%s" % (
            running[:12], " [restart required]" if data.get("restart_required") else "",
        ))
    if data.get("restart_required"):
        lines.append("  restart: required; running source differs from the installed checkout")
    if updated is True:
        lines.append("  update: fast-forwarded; restart Sonder to run the new source")
    elif updated is False:
        lines.append("  update: already current; no files changed")
    else:
        lines.append("  update: %s" % runtime_update_eligibility(data, update_branch=update_branch))
    return "\n".join(lines)


def runtime_update_eligibility(data, *, update_branch):
    """Describe whether the deliberately narrow update action may run.

    This is presentation-only.  ``git_tools.runtime_update`` remains the
    authority and repeats every check immediately before modifying a checkout.
    Giving the same verdict to ``/updatecheck`` avoids a surprising approval
    prompt followed by a safe refusal for an observable checkout condition.
    """
    if not data.get("trusted_remote"):
        return "refused; remote is not the canonical Sonder origin"
    branch = str(data.get("branch") or "").strip()
    if branch != update_branch:
        current = branch or "detached HEAD"
        return "refused; checkout must be %r (current: %r)" % (
            update_branch, current,
        )
    if not data.get("clean"):
        return "refused; source checkout is dirty"
    try:
        ahead = int(data.get("ahead") or 0)
    except (TypeError, ValueError):
        # A malformed status must never be presented as permission to update.
        return "refused; local commit status is unavailable"
    if ahead:
        return "refused; local commits require manual reconciliation"
    if data.get("state") == "current":
        return "eligible; already current"
    return "eligible; /update can fast-forward canonical main"
