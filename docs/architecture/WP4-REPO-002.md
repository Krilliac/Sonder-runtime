# WP4 REPO-002 — language baseline registry

`repository_languages` provides a deterministic language/extension baseline
for repository intelligence across the required C-family, scripting, systems,
SQL, and shader languages. It contains metadata only; parsing and LSP access
remain separate capability adapters.

Evidence: `tests/test_repository_languages.py`, architecture/evidence gates,
and compileall.
