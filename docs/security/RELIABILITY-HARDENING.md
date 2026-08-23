# Security & reliability hardening — August 2026 pass

Scope of this pass: tool policy durability, failure recovery, durable
locks, lease fencing, redaction, and trace boundaries. Each change is
grounded in a specific observed failure mode and carries adversarial
tests; nothing here weakens an existing safeguard or broadens a
permission.

## What changed

### Policy durability (`permission_rules.py`)

`permissions.json` was published with a plain `write_text`. A torn write
left invalid JSON, and every later load then degraded to the built-in
defaults — silently discarding the operator's deny rules until someone
noticed the warning. Saves are now atomic (serialize first, same-directory
temp file, fsync, `os.replace`), refuse symlinked policy paths, and
`add_rule` preserves the original bytes of a degraded policy in a
`permissions.json.invalid` sidecar before rewriting it.
Tests: `tests/test_permission_rules.py` (durable-saves section).

### Stash-exact updates (`safe_update.py`)

The git stash stack is shared across every worktree and concurrent
session, but the updater restored and dropped local edits with bare
`git stash apply` / `git stash drop` (implicitly `stash@{0}`). A stash
pushed by another process mid-update would be applied into the rebased
checkout and destroyed. The backup entry is now tagged with a unique
token, resolved to its commit SHA, applied by SHA, and dropped by
re-resolving the SHA to its current position; an unidentifiable entry
aborts before fetch/rebase. Tests: `tests/test_safe_update.py`
(concurrent-stash test verified to fail against the old behavior).

### Durable locks and the curriculum store (`durable_locks.py`, `curriculum_store.py`)

`curriculum_store` was the one root store with no concurrency
protection: unlocked, unfsynced JSONL appends that could interleave
between processes, and a `load()` that raised on the first torn or
corrupt line — one bad record made the whole curriculum unreadable.
`durable_locks.exclusive_file_lock` is the shared cross-process lock
primitive (the runtime already had two ad-hoc implementations in
`command_recovery` and `adaptive_training`; new callers should use this
one instead of writing a fourth). Appends now serialize under the
sidecar lock, write one pre-serialized buffer, and fsync; `load()`
ignores an unterminated final line as crash evidence and skips interior
corruption with a counted warning.
Tests: `tests/test_curriculum_store_durability.py` (including a
4-process interleaving drill).

### Lease fencing (`tests/test_autopilot_stale_lease.py`)

The autopilot store's lease design was audited and found sound; the
fencing invariants that make split-brain impossible are now pinned by
tests: an expired lease reconciles to `interrupted`; the old owner's
heartbeat, progress writes, and re-claim are fenced after reconcile and
after takeover; and the pre-reconcile single-owner resurrection window
is documented as deliberate.

### Recovery prevalidation (`selfmod_recover.py`)

Emergency recovery promises never to partially restore a bundle, but a
malformed `mode_before` reached `os.chmod` mid-restore, and the rollback
of a created file used `target.exists()` (which follows symlinks), so a
created file that became a dangling symlink survived. Both are fixed in
prevalidation/rollback. Tests: `tests/test_selfmod.py` (manifest
hardening section).

### Redaction seam (`sonder_runtime/domain/security/redaction.py`)

The runtime had at least nine independent redaction implementations, and
the production tool-facade composition used the no-op
`IdentityRedactor`. The canonical credential-shape pattern set now lives
in the pure domain module, with a bounded structure walker that replaces
over-deep/over-budget subtrees with `[REDACTED]` rather than passing
content it stopped examining. `PatternOutputRedactor` implements the
gateway's `OutputRedactor` port over it, and `runtime_container` injects
it composed with the platform `Redactor` (live secret env values).
Layering forbids `platform -> domain` imports, so `platform/logging.py`
keeps its own pattern set; a drift-guard test pins the two sets
byte-identical. The `compose()` default remains `IdentityRedactor` —
the truthful-receipt contract (`redaction_applied=False` when nobody
configured authority) is deliberate and pinned by existing tests.
Tests: `tests/test_redaction_domain_seam.py`.

### Trace-context seam (`sonder_runtime/application/observability/trace_context.py`) — experimental

Pure, strict W3C `traceparent` parse/format/child-context. Nothing on
the hot path uses it yet; it exists so the interface layer can accept
and emit trace context without inventing its own parser. Parsing is
total over attacker-controlled headers. Tests:
`tests/test_trace_context.py`.

## Known gaps deliberately not addressed here

- `sonder_runtime/application/security/path_archive_safety.py` and
  `race_resistant_paths.py` have tests and are cited as SEC-003
  evidence, but nothing in production imports them; the live containment
  engine is `adapters/filesystem/file_ops.py`, whose private helpers are
  re-implemented by several inspection tools. Wiring one shared
  containment API through those callers is the highest-value follow-up,
  but it touches the live enforcement path and deserves its own
  reviewed change, not a side effect of this pass.
- `command_recovery.CommandJournal` (durable idempotency receipts) is
  not wired to the direct MCP tool dispatch path; mutating tool calls
  still have no idempotency key there.
- Redaction remains fragmented across the older call sites
  (`model_error_formatting`, `tracing_health`, `query_export`,
  `tool_audit`, `local_observability`, …). The domain module is the
  convergence target; each migration should carry its own regression
  tests.
