# Research port: portable recipes and subrecipes

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Goose documents portable YAML recipes containing instructions, extensions,
parameters, and reusable subrecipes. Sonder already has executable saved
workflows, but those are adapter-owned action lists and lack an immutable,
provider-neutral manifest and subrecipe graph validation.

Source: <https://block.github.io/goose/index.html>

## Implemented slice

`domain.recipe` now provides a portable manifest contract with:

- bounded descriptions, instructions, extensions, parameters, and steps;
- deterministic JSON-safe serialization and SHA-256 identity;
- strict `from_dict` rehydration at transport boundaries, including schema,
  collection, and nested-step validation;
- optional subrecipe references;
- missing-node and cycle validation before execution.

JSON is the canonical in-runtime representation; a future YAML adapter can
map to this contract without coupling execution to a parser.

Evidence: `tests/test_recipe_manifest.py`.
