"""Attended console writer for the model-list overlay only."""

from __future__ import annotations

import json

import sonder_runtime.adapters.model_lists as model_lists
from sonder_runtime.adapters.filesystem import atomic_json

USAGE = "/runtime models [set <key> <json> | reset <key>] (writes require the console operator)"


def command(arg: str | None, *, operator_attended: bool) -> str:
    action, _, rest = str(arg or "").strip().partition(" ")
    action = action.lower()
    if action in ("", "show", "list", "status"):
        return "\n".join(model_lists.describe())
    if action in ("help", "?"):
        return USAGE
    if action not in ("set", "reset"):
        return "refused /runtime models: " + USAGE
    if operator_attended is not True:
        return "refused /runtime models: writes require the console operator present; nothing was changed"
    key, _, raw_value = rest.strip().partition(" ")
    if key not in model_lists.KEYS:
        return "refused /runtime models: unknown key " + key
    try:
        value = json.loads(raw_value) if action == "set" else None
        if action == "reset" and raw_value:
            raise ValueError(USAGE)
        shipped = model_lists.load_shipped_document()
        path = model_lists.overlay_path()
        if path.resolve() == model_lists.SHIPPED_PATH.resolve():
            raise ValueError("overlay must be separate from the shipped file")
        # Validate a proposed value before taking the lock or creating a file.
        if action == "set":
            model_lists.merge(
                shipped, {"version": model_lists.SCHEMA_VERSION, key: value}
            )
        with atomic_json.file_lock(path):
            try:
                document = model_lists.read_document(path)
            except FileNotFoundError:
                document = {"version": model_lists.SCHEMA_VERSION}
            model_lists.merge(shipped, document)  # never overwrite a bad overlay
            if action == "set":
                document[key] = value
            else:
                document.pop(key, None)
            model_lists.merge(shipped, document)
            atomic_json.write_json_atomic(path, document)
        return (
            "%s %s in overlay %s. Cloud tier defaults and the retired list reach running"
            " Sonder processes on their next tier read; other startup bindings (local"
            " tiers seeded at import) still need a restart."
            % (action, key, path)
        )
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError) as exc:
        return "refused /runtime models: %s; nothing was changed" % exc
    finally:
        model_lists.clear_cache()
