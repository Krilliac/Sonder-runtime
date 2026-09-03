"""Parse and validate a runtime policy update value as a JSON object."""

from __future__ import annotations

import json


def parse_update_object(value, label: str) -> dict:
    """Accept a dict, a JSON string, None, or empty string; return a dict.

    Raises ValueError when *value* is not (or does not decode to) a JSON
    object.
    """
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be a JSON object: %s" % (label, exc))
    if not isinstance(payload, dict):
        raise ValueError("%s must be a JSON object" % label)
    return payload
