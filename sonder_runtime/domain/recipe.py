"""Portable, provider-neutral recipe manifests and subrecipe validation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType


_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
MAX_RECIPE_TEXT = 32_768
MAX_RECIPE_ITEMS = 256


class RecipeError(ValueError):
    """Raised when a portable recipe manifest is invalid."""


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise RecipeError(f"{label} must be a bounded lowercase identifier")
    return value


def _text(value: object, label: str, limit: int = MAX_RECIPE_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RecipeError(f"{label} must be non-empty bounded text")
    return value


@dataclass(frozen=True, slots=True)
class RecipeStep:
    step_id: str
    instruction: str
    subrecipe: str | None = None

    def __post_init__(self) -> None:
        _name(self.step_id, "step_id")
        _text(self.instruction, "instruction")
        if self.subrecipe is not None:
            _name(self.subrecipe, "subrecipe")

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "instruction": self.instruction,
            "subrecipe": self.subrecipe,
        }


@dataclass(frozen=True, slots=True)
class RecipeManifest:
    name: str
    version: str
    description: str
    instructions: str
    extensions: tuple[str, ...] = ()
    parameters: Mapping[str, str] = MappingProxyType({})
    steps: tuple[RecipeStep, ...] = ()

    def __post_init__(self) -> None:
        _name(self.name, "name")
        _text(self.version, "version", 64)
        _text(self.description, "description", 4096)
        _text(self.instructions, "instructions")
        if len(self.extensions) > MAX_RECIPE_ITEMS or len(self.steps) > MAX_RECIPE_ITEMS:
            raise RecipeError("recipe item count exceeds limit")
        extensions = tuple(sorted({_name(item, "extension") for item in self.extensions}))
        if len(extensions) != len(self.extensions):
            raise RecipeError("extensions must be unique")
        parameters = dict(self.parameters)
        if len(parameters) > MAX_RECIPE_ITEMS:
            raise RecipeError("parameter count exceeds limit")
        for key, value in parameters.items():
            _name(key, "parameter")
            _text(value, f"parameter {key}", 4096)
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise RecipeError("step IDs must be unique")
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sonder.recipe.v1",
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "instructions": self.instructions,
            "extensions": list(self.extensions),
            "parameters": dict(sorted(self.parameters.items())),
            "steps": [step.to_dict() for step in self.steps],
        }

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_recipe_graph(recipes: Mapping[str, RecipeManifest]) -> None:
    """Reject missing subrecipes and cycles before a recipe is executable."""
    if not isinstance(recipes, Mapping) or len(recipes) > MAX_RECIPE_ITEMS:
        raise RecipeError("recipe graph is invalid or too large")
    normalized = dict(recipes)
    if any(key != recipe.name for key, recipe in normalized.items()):
        raise RecipeError("recipe graph keys must match recipe names")
    for recipe in normalized.values():
        for step in recipe.steps:
            if step.subrecipe is not None and step.subrecipe not in normalized:
                raise RecipeError(f"missing subrecipe: {step.subrecipe}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise RecipeError("recipe graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for step in normalized[name].steps:
            if step.subrecipe is not None:
                visit(step.subrecipe)
        visiting.remove(name)
        visited.add(name)

    for name in sorted(normalized):
        visit(name)


__all__ = [
    "RecipeError", "RecipeManifest", "RecipeStep", "validate_recipe_graph",
]
