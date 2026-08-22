"""Pure presentation helpers used by the interactive REPL."""


def elapsed_label(elapsed_ms):
    """Format a non-negative duration for terminal status chrome."""
    elapsed_ms = max(0, int(elapsed_ms or 0))
    if elapsed_ms < 1000:
        return "%dms" % elapsed_ms
    return "%.2fs" % (elapsed_ms / 1000.0)


def response_status_label(label="Sonder", *, error=False):
    """Return the short state label used by the interactive response panel."""
    state = "error" if error else "answer"
    return "%s · %s" % (str(label or "Sonder"), state)


def response_footer(box, timing, *, offer_feedback=False):
    """Build the stable footer for an interactive response panel."""
    feedback = "(/pass or /fail)" if offer_feedback else ""
    return "%s %s  %s" % (box["bl"], str(timing), feedback)
