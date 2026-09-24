"""Bounded summary search and lazy schemas over one immutable tool inventory."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from ..ports.tool_registry import ExecutableToolInventory, ToolSchemaSelection
from .generated_catalogs import GeneratedCatalogs


class ToolDiscovery:
    """Discovery narrows visibility; execution still crosses the existing gateway."""

    def __init__(self, registry, *, allowed_names=None):
        source = tuple(registry.list_all())
        if len(source) > 256:
            raise ValueError("tool discovery inventory exceeds 256 tools")
        allowed = {x.name for x in source} if allowed_names is None else frozenset(allowed_names)
        if allowed - {x.name for x in source}:
            raise ValueError("tool discovery grant includes unknown tools")
        self._inventory = ExecutableToolInventory(tuple(x for x in source if x.name in allowed))
        self.digest = GeneratedCatalogs.generate(self._inventory.descriptors, event_kinds=()).digest

    def search(self, query: str, *, limit: int = 8) -> dict:
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("tool search query must be at most 512 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("tool search limit must be between 1 and 20")
        words = set(re.findall(r"[a-z0-9]+", query.casefold()))
        scored = []
        for item in self._inventory.descriptors:
            name = set(re.findall(r"[a-z0-9]+", item.name.casefold()))
            description = set(re.findall(r"[a-z0-9]+", item.description.casefold()))
            score = 4 * len(words & name) + len(words & description)
            if not words or score:
                scored.append((-score, item.name, item.description))
        scored.sort()
        return {"inventory_digest": self.digest, "matches": [
            {"name": name, "summary": description[:240]}
            for _, name, description in scored[:limit]
        ], "truncated": len(scored) > limit}

    def load(self, names, *, inventory_digest: str, selection_id: str):
        if inventory_digest != self.digest:
            raise ValueError("tool inventory changed; search again before loading schemas")
        if not isinstance(names, (list, tuple)) or not 1 <= len(names) <= 8:
            raise ValueError("load between 1 and 8 tool schemas")
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise ValueError("tool schema names must be unique strings")
        if not isinstance(selection_id, str) or not 1 <= len(selection_id) <= 160:
            raise ValueError("bounded tool selection identity required")
        if any(self._inventory.get(name) is None for name in names):
            raise ValueError("tool is outside the discovery grant")
        selection = ToolSchemaSelection(frozenset(names), selection_id=selection_id)
        bundle = GeneratedCatalogs.generate(self._inventory.descriptors, event_kinds=(), selection=selection)
        manifest = {"inventory_digest": self.digest, "selection": selection.marker(),
                    "schema_digest": bundle.digest}
        manifest["digest"] = hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        payload = {"tools": bundle.mcp["tools"], "manifest": manifest}
        if len(json.dumps(payload, ensure_ascii=True).encode()) > 48_000:
            raise ValueError("selected tool schemas exceed response budget")
        return selection, deepcopy(payload)


__all__ = ["ToolDiscovery"]
