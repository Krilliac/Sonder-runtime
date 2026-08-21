from __future__ import annotations

import pytest

from sonder_runtime.domain.recipe import RecipeError, RecipeManifest, RecipeStep, validate_recipe_graph


def _recipe(name, subrecipe=None):
    return RecipeManifest(
        name, "1.0.0", "A portable recipe", "Do the requested work.",
        extensions=("filesystem", "mcp.search"),
        parameters={"workspace": "project"},
        steps=(RecipeStep("step-one", "Inspect the workspace.", subrecipe),),
    )


def test_recipe_serialization_and_digest_are_deterministic():
    recipe = _recipe("main")
    restored_shape = recipe.to_dict()
    assert restored_shape["schema"] == "sonder.recipe.v1"
    assert restored_shape["extensions"] == ["filesystem", "mcp.search"]
    assert recipe.digest() == _recipe("main").digest()


def test_recipe_round_trips_from_transport_shape():
    recipe = _recipe("main", "child")
    restored = RecipeManifest.from_dict(recipe.to_dict())
    assert restored == recipe
    assert restored.digest() == recipe.digest()


@pytest.mark.parametrize("mutator, message", [
    (lambda value: value.update(schema="other.v1"), "schema"),
    (lambda value: value.update(steps={}), "steps"),
    (lambda value: value.update(parameters={"workspace": 42}), "strings"),
    (lambda value: value.update(steps=[{"step_id": "only-id"}]), "instruction"),
])
def test_recipe_import_rejects_malformed_transport_shapes(mutator, message):
    value = _recipe("main").to_dict()
    mutator(value)
    with pytest.raises(RecipeError, match=message):
        RecipeManifest.from_dict(value)


def test_subrecipes_require_existing_nodes_and_acyclic_graphs():
    validate_recipe_graph({"main": _recipe("main", "child"), "child": _recipe("child")})
    with pytest.raises(RecipeError, match="missing"):
        validate_recipe_graph({"main": _recipe("main", "missing")})
    with pytest.raises(RecipeError, match="cycle"):
        validate_recipe_graph({"main": _recipe("main", "child"), "child": _recipe("child", "main")})


def test_recipe_rejects_duplicate_step_ids():
    with pytest.raises(RecipeError, match="step IDs"):
        RecipeManifest(
            "main", "1.0.0", "description", "instructions",
            steps=(RecipeStep("same", "one"), RecipeStep("same", "two")),
        )
