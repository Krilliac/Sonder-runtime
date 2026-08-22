"""Pure completion-query normalization for served command discovery."""

COMPLETE_DEFAULT_LIMIT = 12
COMPLETE_MAX_LIMIT = 50


def completion_limit(raw):
    """Clamp a completion ``limit`` into the supported 1..50 range."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return COMPLETE_DEFAULT_LIMIT
    return max(1, min(COMPLETE_MAX_LIMIT, value))
