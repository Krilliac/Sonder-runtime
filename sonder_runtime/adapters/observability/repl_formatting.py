"""Pure presentation helpers used by the interactive REPL."""


def elapsed_label(elapsed_ms):
    """Format a non-negative duration for terminal status chrome."""
    elapsed_ms = max(0, int(elapsed_ms or 0))
    if elapsed_ms < 1000:
        return "%dms" % elapsed_ms
    return "%.2fs" % (elapsed_ms / 1000.0)
