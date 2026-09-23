# CTX-009 multi-turn and multi-worker prefix evidence — 2026-09-23

## Scope

This revision fixes a defect in the live agent-lane request builder and records
a bounded local two-worker probe. Baseline: `origin/main`
`494f2397` (merge of PR #537). The requirement remains
`implemented_unverified`.

## Defect

When a lane has a tool facade, `AgentLaneService._request` rendered the
per-attempt, per-turn line `Tool schema selection id: <attempt>:<turn>` inside
the stable instructions record and the `system_prefix` identity input.
Consequently:

- every turn of the same lane produced a new prefix identity
  (`miss/identity_changed`), so the application prefix cache never hit once
  tools were enabled;
- two workers serving the same project never derived the same prefix key,
  because each lane attempt id is random;
- the provider's byte prefix diverged at that line, before the visible tool
  schemas and the scoped project rules and skill catalog.

The earlier live probes (PR #537) ran without a tool facade and so did not
exercise this path.

## Change

`sonder_runtime/application/agents/interactive_lanes.py` keeps the selection id
visible to the model (tool calls are still bound to the advertised catalog), but
appends it after the stable prefix and the authoritative project context. It is
excluded from the stable instructions record and prefix identity. Visible tool
schemas, project rules, the skill catalog, workspace scope, and model route
identity remain stable inputs.

## Automated evidence

`tests/test_live_agent_context.py` adds three tests on the real lane builder:

- `test_turn_selection_id_is_visible_but_outside_reusable_prefix`: two turns
  of one lane share the key and stable bytes, and the second turn hits. The
  selection id is visible and is the final line.
- `test_independent_workers_derive_identical_prefix_for_same_project`: three
  workers each have their own SQLite stores, planner cache and producer. Two
  on the same project derive the same key, identity and stable bytes, and each
  reports its own honest `miss/cold_start`. A different project derives a
  different key.
- `test_prefix_identity_is_stable_across_processes_and_hash_seeds`: two
  subprocesses (`PYTHONHASHSEED` 1 and 4242) and the test process derive the
  same key.

RED: with `interactive_lanes.py` from `origin/main`, all three tests fail on
cache-key equality (`3 failed, 7 passed`). GREEN with the change:

```text
python -m pytest -q -p no:cacheprovider tests/test_live_agent_context.py
10 passed
python -m pytest -q -p no:cacheprovider tests/test_live_agent_context.py tests/test_interactive_agent_lanes.py tests/test_wp4_ctx004_006_009_010.py tests/test_session_split_capture.py tests/test_session_replay.py tests/test_remaining_session_durable_replay.py tests/test_context_planning_facade.py tests/production/test_ollama_gateway.py tests/test_inference_telemetry.py
163 passed
python -m pytest -q -p no:cacheprovider tests/test_app_managed_authority.py tests/test_app_work_http.py tests/test_delegated_verification.py tests/test_lane_coding_acceptance.py tests/test_lane_continuation.py tests/test_lane_retention.py tests/test_repl_agent_lanes.py
150 passed
```

## Live two-worker probe

Environment: loopback Ollama `0.34.3` at `http://127.0.0.1:11434`
(`OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
`OLLAMA_FLASH_ATTENTION=1`). Model
`hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q3_K_XL`, digest
`1fe95c054ca1ca8dcb3c90eac4247250afae20c2b87f62d19f7d2c0efca02a7e`, resolved
by the configured code tier. Remote worker and cloud settings were removed from
the probe processes only, so the single loopback origin was the only
route. No Node1 service or configuration was used or changed.

Each worker was a separate Python process with its own temporary SQLite stores.
Each process ran `configure_legacy_model_providers()`, built a production
`OllamaGateway`, and built `AgentLaneService` with `LiveAgentContextProducer`,
`ContextPlanningFacade`, and a two-tool facade. It spawned a lane on one shared
temporary project (a public one-line `AGENTS.md` and one `SKILL.md`), then
built and sent two turns through `_request` and `OllamaGateway.generate`. Only
hashes and counts were retained.

| Code | Worker (hash seed) | Turn | Application result | Prefix key (prefix) | Prompt tokens | Provider cached tokens |
| --- | --- | ---: | --- | --- | ---: | ---: |
| change | 1 (7) | 1 | `miss/cold_start` | `b9676991…` | 440 | 0 |
| change | 1 (7) | 2 | `hit/hit` | `b9676991…` | 440 | 422 |
| change | 2 (31337) | 1 | `miss/cold_start` | `b9676991…` | 439 | 392 |
| change | 2 (31337) | 2 | `hit/hit` | `b9676991…` | 439 | 421 |
| `origin/main` | 1 (7) | 1 | `miss/cold_start` | `f0bf2792…` | 443 | 249 |
| `origin/main` | 1 (7) | 2 | `miss/identity_changed` | `cd5eb366…` | 443 | 287 |
| `origin/main` | 2 (31337) | 1 | `miss/cold_start` | `97b21201…` | 440 | 256 |
| `origin/main` | 2 (31337) | 2 | `miss/identity_changed` | `34e59d10…` | 440 | 284 |

With the change, the full prefix key was
`b96769916be957c329d14b1e04732cdb12f4361d274cf1a92b0dce1f039a7cee` in both
processes. The SHA-256 of the stable system text was
`dbfbe0ca88e89072a50d9fd95a1409c370ede00b5324f402c12476a8ed7c3690` in both.
The route template identity hash was identical for all rows. The `origin/main`
rows ran after the change rows against the same warm server, so their non-zero
first-turn cached counts partly reflect reuse from earlier runs. What the rows
show is that reuse stopped near the selection-id line.

Provider cached counts are Ollama's `prompt_eval_cached_count`, forwarded
through the production transport into `InferenceTelemetry`. Ollama picks the
slot and the reuse boundary internally. The rows show reuse across processes
and turns. They do not show a guaranteed reuse length.

## Remaining gaps

- The application `PrefixManifestCache` is process-local. Each worker reports
  its own cold start, and no hit is shared across processes. Across processes,
  only deterministic identity and provider-side KV reuse are shown.
- Only one loopback Ollama origin was used. `_model_prompt_identity` still
  disables reusable prefixes when more than one pool origin is configured, so
  multi-origin worker routing remains unverified.
- Other providers, concurrent model-tag replacement after the identity probe,
  and replay across restart remain unverified.
