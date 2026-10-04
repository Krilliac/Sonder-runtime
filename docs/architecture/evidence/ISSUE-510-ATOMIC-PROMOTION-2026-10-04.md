# Original issue 510 atomic promotion boundary

Original section 12 of [issue 510](https://github.com/Krilliac/Sonder-runtime/issues/510)
explicitly requires **atomic deploy + rollback point**. Verified backup publication
alone does not satisfy atomic live deployment. This is an original criterion,
not a later strategy-research objective.

## Supported checkout boundary

`selfmod.deploy` now admits exactly one changed file. It checks the bounded
admission rule after obtaining the process-safe deployment lock and inspecting
the candidate diff, before copying/deleting live files, creating a deployment
Git commit, or recording a deployed phase. Human approval, maintenance mode,
typed integration and nightly callers all reach this same check. An empty or
multi-file diff raises `SelfmodStageNotApplied`. The source stays intact and the
run stays approved; its audit bookkeeping and released lock do not constitute a
live deployment. A journaled refusal settles `failed` with a `:not-applied`
receipt, so it does not create an uncertain partially promoted run.

For the supported one-file change, `_atomic_copy` writes the complete candidate
into a sibling temporary file, flushes and fsyncs its bytes, and uses one
`os.replace` as the live visibility boundary. A one-file removal uses one
unlink. Readers see the old file or complete new file, rather than an installed
prefix of a multi-file candidate. The hash-verified sealed rollback bundle is
created before candidate execution and reverified before deployment. Installed
bytes must match tested bytes; rollback readiness and health remain mandatory
existing gates. An interrupted stage after the file boundary can still leave
metadata or its effect receipt uncertain; existing exact restore/reconciliation
is needed. Atomic file visibility is not atomicity of filesystem, Git and
SQLite metadata together, or a claim of power-loss durability on every platform.

Multi-file checkout promotion is **unsupported and refused before live writes**.
Keep the candidate and its evidence for review; reject it or use the existing
[managed signed release workflow](../../runbooks/publish-release.md) when a
coupled change must be installed. Do not split coupled changes into separately
promoted, unevaluated intermediate versions. A future automatic bridge from
selfmod candidates to managed releases would need full-tree staging, bound
verification evidence, retained baseline and a single release-pointer handoff.
That bridge is not implemented by this change.

## Other existing boundaries

| Surface | Atomic boundary and recovery | Scope |
|---|---|---|
| Managed bundles | `adapters/updates/engine.py` stages a complete release directory, writes/fsyncs an activation intent, holds an exclusive activation lock, switches `current`, then CAS-commits active/previous release records. Constructor and new activation reconcile pending intent/pointer/records. | Existing bundle activation, not checkout selfmod. POSIX replacement and pointer-file fallback use single-name switches. Windows replacement of a preexisting directory symlink can need unlink/retry; no blanket all-platform power-loss claim. |
| Runtime model/routing selection | `adapters/runtime_policy.py` serializes CAS revision changes under a cross-process lock and atomically replaces one policy JSON file. Training transition reservation/reconciliation protects the broader deployment flow. | One policy selection record, not an atomic model-alias/backend operation or checkout tree. `training/deployment_rollback.py` also has a reference in-memory repository; its port alone is not production crash evidence. |
| Procedural skill catalog | `adapters/persistence/sqlite/skill_catalog.py` commits active, last-good, revisions and provenance as one sealed generation-counted SQLite row under `BEGIN IMMEDIATE`. Composition restores active revisions from that row after restart; publication compensates ordinary active-port/event failures. | Durable catalog snapshot. In-memory activation and emitted events are outside the SQLite transaction; arbitrary external active ports are not claimed atomic. |

None of these narrower boundaries previously made checkout selfmod's sequential
copies an atomic multi-file deployment. The explicit admission limit removes
that unsupported promotion path rather than relabeling crash recovery as
atomicity.

## Ordinary functional evidence

`tests/test_issue510_atomic_selfmod_deploy.py` covers Git and snapshot checkouts:

- both ordinary two-file promotion cases fail on unchanged `4e876a0a` at the
  first attempted live-copy boundary; the test seam prevents any physical copy;
- multi-file refusal preserves live bytes, candidate bytes, verified manifest,
  approved phase and absence of the proposed new live file;
- two repeated real bootstrap-journaled refusals settle `:not-applied` and leave
  no uncertain effect;
- a real one-file tested candidate deploys, passes its real health and rollback
  checks, and rolls back to the exact original bytes.

Ordinary neighboring regressions retain interrupted-copy restoration, failed
health restoration, missing/tested-byte admission, stale-owner and interrupted
rollback recovery, metadata-failure restore, deployment-lock conservation and
preservation of later user edits. Existing two-file maintenance and end-to-end
fixtures now exercise refusal or the supported one-file boundary; tests are not
silently skipped. The out-of-tree rollback health regression retains its own
single-file candidate and reproducer rather than losing its recovery coverage.

Verification passes the four new tests, thirteen selected ordinary selfmod
recovery/admission regressions and 84 bridge/journal/health tests: 101 distinct
tests in total, with one existing health case deselected from that focused run.
All seven repository gates pass, including the unchanged legacy module-size
ratchet. The final combined full-suite/hosted gate belongs to integration.

This establishes the original atomic-deploy criterion **for supported one-file
checkout evolution** with a verified rollback point and explicit refusal of
unsupported coupled changes. It does not assert automatic multi-file harness
promotion, universal task quality, or filesystem/database/Git distributed
transactions. The original requirement remains an explicit admission contract
for every promotion, including any future multi-file release bridge.
