# WP4 — typed context assembly integration

`sonder_runtime.application.context_integration.ContextAssemblyService` is
the smallest application use case connecting the typed model-aware planner to
the existing immutable priority selection policy. It derives requested section
costs from typed `ContextItem` candidates, plans the model input budget, and
selects each section against its planned ceiling. The returned assembly and
selection explanations are immutable snapshots; the candidate collections are
not modified.

`sonder_runtime.adapters.context_planning.RuntimeContextPlanningAdapter` is the
runtime-facing binding. It resolves the configured requested context through
the existing context-selection adapter, then invokes the application service.
This keeps runtime sizing at the adapter boundary and keeps the application
service provider- and transport-neutral.

Compaction remains a separate candidate transformation. This slice does not
replace source history or add a second compaction pass; existing compaction
services continue to return append-only/non-destructive candidates.

Verification:

```text
python -m pytest -q tests/test_wp4_context_integration.py tests/test_wp4_context_planner.py tests/test_wp4_ctx003_005.py tests/test_context_selection_adapter.py
python -m compileall -q sonder_runtime/application/context_integration.py sonder_runtime/adapters/context_planning.py
git diff --check
```
