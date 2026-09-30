# Tool side-effect traits

`sonder_runtime/domain/tools/traits.py` owns `TriState` and `ToolTraits`.
`TRUE`, `FALSE`, and `UNKNOWN` are enum values; Boolean coercion raises so
an omitted declaration cannot silently become false. All five fields default
to `UNKNOWN`. `max_result_bytes` is an optional positive advisory bound.

The trust-aware properties are the decision boundary:

| Trait | Unknown handling |
| --- | --- |
| read_only | May mutate |
| destructive | Potentially destructive for external metadata; built-in permission grades remain catalog-owned |
| idempotent | No physical replay |
| concurrency_safe | Exclusive execution wherever an admission gate is composed |
| open_world | External reach remains possible |

`Effect` and `ToolEffect` remain supported. A binary `READ_ONLY` or an explicit
`READ_FILES`-only legacy descriptor maps to a host read contract. An empty
effect set remains unknown. Typed registry snapshots, copied descriptors and
internal inventories preserve traits. MCP tool names, inputs and public schemas
are unchanged. Version-1 client/SDK capabilities and permission projections
retain their existing fields; traits remain internal descriptor metadata, and
SDK validation continues to reject unknown fields.

## Built-ins

`domain/tools/builtin_traits.py` preserves the existing repository/catalog
read contracts and typed aliases. The native registry attaches these
declarations to every descriptor. Unclassified tools receive unknown traits.
Only the listed independent filesystem inspections declare concurrency safety.
Host probes, cache refreshers, memory/status tools, compute jobs and build/debug
execution retain unknown concurrency safety. Execution and mutations generally
retain unknown replay safety. Network reach is unknown except where the host
contract establishes local-only work or a network operation. Result sizes
remain unknown unless explicitly bounded by host configuration.

Permission risk for built-ins comes from the command catalog and existing
static work tables (`EXECUTION_TOOLS`, `NATIVE_MCP_WORK`, etc.). UNKNOWN host
traits do not reclassify a built-in, including execution and mutation tools.
Only an explicit host `destructive=TRUE` declaration can escalate that grade;
host read-only declarations retain the existing safe reads. Stronger catalog
grades, explicit denies, mode rules and operator grants still apply.
Scoped writes/edits/copies/moves/patches and `build_fix_restore` retain UNKNOWN
destructiveness: their effects depend on arguments and existing files, and a
mutation is not itself an unconditional destructive declaration. Deletion,
SQL mutation, task deletion and destructive Git operations retain explicit
destructive declarations and their existing dangerous grades. No built-in risk
class is intentionally changed. `permission_rules.py` still stores explicit
operator rules; `permission_modes` computes the effective decision.

Typed gateways obtain authority from the registered descriptor, never from
traits supplied in a request. Unknown empty-effect calls participate in the
effect journal. Concurrency admission is opt-in: `ToolGateway` (and
`from_typed_ports`) accept `concurrency_gate=ToolConcurrencyGate()`, under
which only calls whose host traits declare `concurrency_safe=TRUE` overlap and
an UNKNOWN call excludes all others. The composed runtime gateway does not
enable it: one gateway instance serves agents, native MCP, HTTP and lanes, and
`test_run`/`build_job` wait up to 60 s by default, so a process-wide exclusive
gate would stall every unrelated call behind them (measured: a second call
waited the full duration of the first on every probed pair; origin/main: 0 ms). Reconciliation can recover a committed
receipt without sending again; only a proven idempotent operation can be sent
again. Speculation retains its closed allowlist and additionally requires host
read-only authority. Removed or invalidated speculative slots retain their
concurrency reservation until their physical worker exits.
Resource rules also evaluate network reach when `open_world` is true or
unknown; a rule permitting only local file reads cannot authorize that reach.
Legacy gateways without a registry use the host built-in declarations, so
built-in reads with empty effects remain outside the journal and consume no
child-runner call ordinal. Unclassified empty-effect calls remain unknown.

## Measured compatibility with origin/main

Baseline: `45a3e093aa423c3a23f9e356419972d2332015e5`, read using `git show`.
`tests/fixtures/builtin_risk_golden.json` pins all 339 baseline catalog names
plus `playbooks` (added later, `dangerous`).
`tests/fixtures/builtin_behavior_golden.json` pins the 16 speculation names,
31 typed gateway names, existing retry call sites and origin retry decisions.
`tests/test_builtin_behavior_golden.py` exercises the real catalog classifier,
speculation admission and gateway concurrency with inert invokers; the catalog
test supplies registration metadata without importing/starting root `server`.

| Built-in behavior | Changed vs baseline |
| --- | ---: |
| Permission risk (339 catalog commands) | 0 |
| Speculation eligibility (16 allowlisted tools) | 0 |
| Automatic retry/replay eligibility | 0 |
| Typed gateway overlap (31 typed tools, default composition) | 0 |
| Speculation overlap (multi-slot only, distinct names) | 10 |

Retry has no per-built-in automatic retry allowlist in the baseline. The generic
`DurableLoopControl.retry`/`TransportRetryExecutor` APIs receive an explicit
effect contract, not a tool name, and no built-in dispatcher calls those APIs.
Their existence must not be reported as 339 retry-eligible built-ins. Existing
keyed process-start, compute-submit and compute-cancel journal declarations are
unchanged and pinned by the golden. When traits are supplied, physical replay
requires host `idempotent=TRUE`. The generic non-idempotent effect contract now
refuses a second send even after a retry-safe reconciliation; recovering an
already committed result remains allowed. This is a generic API safety change,
with zero affected built-in dispatch callers.

The following concurrency restrictions are deliberate. UNKNOWN is retained
where the implementation does not establish a concurrency contract; old
unrestricted overlap is not evidence of safety. Single-call eligibility is
unchanged. Speculation restrictions affect configured multi-slot execution
(`SONDER_SPECULATION_SLOTS > 1`); the default is one slot. The typed gateway
has no default restriction; the table below lists the traits that an opt-in
`concurrency_gate` would act on.

| Speculation overlap exception | Reason for retaining UNKNOWN concurrency safety |
| --- | --- |
| `activity_status` | Reads changing activity/worker state. |
| `command_registry_list` | Reads refreshable global registry state. |
| `context_health` | Reads mutable runtime/context state. |
| `data_inspect` | Dispatches format-specific inspectors and shared activity recording. |
| `memory_search` | Uses shared memory/embedding/cache services. |
| `permission_policy` | Reads mutable policy state and invokes reload handling. |
| `program_search` | PATH/registry inspection includes shared reload/activity handling. |
| `script_search` | Filesystem inspection includes shared reload/activity handling. |
| `status` | Aggregates mutable runtime/model state. |
| `workspace_inventory` | Refreshes repository/inventory observations. |

| Typed tool with FALSE/UNKNOWN concurrency safety (opt-in gate only) | Reason |
| --- | --- |
| `edit_file` (`file_edit`) | May race edits to the same file. |
| `file_batch_write` | May race a multi-file write. |
| `file_copy` | May race the destination/source state. |
| `file_delete` | May delete paths another call uses. |
| `file_move` | Mutates source and destination paths. |
| `json_patch` | May race a read/modify/write operation. |
| `make_directory` (`directory_create`) | May race creation or path mutation. |
| `text_patch` | May race patch preconditions and writes. |
| `write_file` (`file_write`) | May overwrite a concurrently used path. |
| `build_fix` | Mutates build inputs and coordinates host work. |
| `build_fix_restore` | Restores files that another call may use. |
| `build_job` | Runs host build work in shared workspaces. |
| `test_run` | Runs host tests with shared workspace/resources. |
| `build_fix_result` | Reads mutable job/result state without a declared parallel contract. |
| `build_job_result` | Reads mutable job/result state without a declared parallel contract. |
| `test_run_result` | Reads mutable job/result state without a declared parallel contract. |
| `debug_run_result` | Reads mutable debugger/job state without a declared parallel contract. |
| `tool_inventory` | Reads refreshable tool/probe state. |
| `build_model` | Inspects shared build configuration/probe state. |
| `crash_triage` | Coordinates debugger/symbol/artifact inspection state. |
| `crash_digest` | Launches host debugger work. |
| `profile_digest` | Inspects shared profiler/artifact state. |
| `profile_capture_digest` | Launches host profiler work. |
| `program_search` | No established parallel contract across the shared inspection path. |
| `script_search` | No established parallel contract across the shared inspection path. |

There are 10 speculation exceptions and no default typed gateway exception
(25 typed tools would serialize under an opt-in gate). The golden tests require
exactly these exception sets and hold the first worker open while testing
overlap, so fast completion cannot mask a new serialization regression; they
also require that a held `test_run`/`build_job`/`write_file` call never delays
an unrelated call on the default gateway.

## External MCP

The host passes a `tools/list` response to
`ExternalMcpBridge.ingest_tool_list(server_name, response)`. Only already
allowlisted tool names are accepted. `tool_annotation_traits()` exposes the
raw hints and `tool_traits()` exposes the conservative merge with host policy.
An omitted hint stays unknown, including SDK fields populated by defaults but
absent from `model_fields_set`. Malformed hints cannot grant authority.

Server configuration may explicitly set `trust_annotations: true` (default
false). Trusted hints can fill unknown host declarations; contradictory hints
can tighten but never override explicit host restrictions. Untrusted hints
can only tighten handling. This follows the MCP
[ToolAnnotations trust guidance](https://modelcontextprotocol.io/specification/2025-06-18/schema#toolannotations).
Explicit host `read_only` settings remain compatible, while omitted settings
do not establish read-only authority. External calls are serialized at the
bridge; MCP has no concurrency-safety hint. No automatic discovery network
traffic or automatic retries are introduced.

The bridge's tool allowlist, capability allowlist, endpoint checks and byte
limits remain independent authority. Ingestion never adds an executable tool,
changes those limits, or grants an annotation permission to bypass them.
