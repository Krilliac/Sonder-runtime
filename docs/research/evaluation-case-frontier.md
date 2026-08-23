# Grounded evaluation-case frontier

Status: foundation implemented; execution experiments remain unproven.

## Source finding

Sonder already has most of the *result* side of a serious evaluation system:

- `sonder_runtime/application/evaluation/proposal_lifecycle.py` binds suite
  identity, metrics, dimensions, sample count, provenance, shadow/canary
  observations, and attended promotion evidence.
- `sonder_runtime/application/evaluation/trajectory_replay.py` provides
  digest-bound deterministic replay records.
- `sonder_runtime/application/evaluation/corpus_inventory.py` proves bounded
  repository/tool/memory source coverage.
- `verifiers.py` is the existing execution-oracle registry and distinguishes
  unavailable infrastructure from a failed artifact.

The missing seam was the *input* side: no common versioned contract connected a
suite to concrete cases and verifier specifications. Cases instead use at least
three incompatible shapes: dictionaries in `training_tasks.py`, callable-bearing
`MoatTask` values in `scripts/benchmark_moat.py`, and a separate submission
schema in `scripts/benchmark_repository_research.py`. Repository history shows
the evaluation lifecycle arriving in `4e4072fc` and later composition work in
`af260068`, but no shared case manifest. A bounded TODO/FIXME/HACK scan on
2026-08-22 found no real open marker for this gap; it is an architectural seam,
not an abandoned TODO.

## Ecosystem evidence and what maps

- [Inspect AI datasets](https://inspect.aisi.org.uk/datasets.html) give each
  sample an input, target, ID, metadata, and optional sandbox; its
  [task contract](https://inspect.aisi.org.uk/tasks.html) composes datasets,
  solvers, and scorers. Sonder adopts stable case identity and input/target
  separation, but does not adopt executable setup fields or a dependency.
- [Promptfoo assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/)
  separate deterministic assertions from model-assisted metrics. Sonder maps
  these to its existing verifier names and makes `llm_judge` advisory-only.
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) demonstrates the value of
  instance identity and revision-pinned executable evaluation. Sonder records
  source/revision/digest provenance, but this foundation does not fetch or
  prepare repositories.
- [tau-bench](https://github.com/sierra-research/tau-bench) motivates repeated
  trial reliability rather than a single lucky pass. `pass^k` is a later runner
  experiment, not a metric claimed by the manifest validator.

The seam deliberately does **not** import LangGraph, AutoGen, CrewAI, DSPy, or a
hosted evaluation product. Their broader orchestration/optimization surfaces do
not map to this source-proven deficiency and would expand dependencies and
authority.

## Implemented foundation

`sonder_runtime/application/evaluation/case_manifest.py` now provides:

- immutable JSON-only cases, graders, provenance, and suite-bound manifests;
- canonical SHA-256 case and manifest identities, including the existing suite
  digest;
- fail-closed bounds (256 cases, 64 KiB per case, 1 MiB per manifest, bounded
  nesting and tags), finite-number enforcement, strict unknown-field rejection,
  and digest verification on read;
- a content-free diagnostic projection that reports only counts, digests, and
  unavailable verifier names; and
- an explicit rule that model-graded verifiers cannot become promotion gates.

The loader is local and non-executing. It does not resolve paths in case data,
call a model, load a verifier, access the network, write state, or alter
permissions. `scripts/check_eval_manifest.py` is the adapter that compares the
references with the current `verifiers.REGISTRY`:

```powershell
python scripts/check_eval_manifest.py docs/research/examples/evaluation-case-manifest.json
python scripts/check_eval_manifest.py docs/research/examples/evaluation-case-manifest.json --json
```

Exit code 0 means valid, runnable, and backed by at least one deterministic
case; 1 means structurally valid but not locally ready; 2 means invalid. The
JSON Schema in `docs/research/schemas/evaluation-case-manifest.schema.json` is an
interchange aid; Python validation remains authoritative for byte bounds,
ordering, canonical digests, and verifier preflight.

## Falsifiable roadmap

No row below is considered complete merely because this document exists.

| Experiment | Hypothesis | Method | Acceptance criteria | Stop / rollback trigger |
| --- | --- | --- | --- | --- |
| RF-001: migrate real cases | One contract can represent current case sources without weakening their oracles. | Convert at least 20 cases spanning `training_tasks`, moat tasks, and repository-research fixtures; compare old/new case IDs, inputs, targets, and verifier specs mechanically. | 100% field-level equivalence for the representable data; all manifests preflight; digest is identical after JSON key reordering; one unknown deterministic verifier makes the checker exit 1. | Any case needs executable loader hooks, secrets, absolute paths, or a weaker verifier to fit. |
| RF-002: execution adapter | A thin runner can reuse `verifiers.REGISTRY` while keeping infrastructure errors distinct from failures. | Execute only explicit local cases behind the existing permission/execution boundary; capture result IDs and case digests into the existing evaluation lifecycle. | A deliberately broken artifact is RED; missing tooling is `unavailable`, never `failed` or `passed`; default CI performs no model/network calls; legacy harness results are unchanged. | Any new bypass around `permission_modes`, `isolated_runner`, or verifier confinement. |
| RF-003: repeated-trial reliability | Repeated success exposes instability hidden by a one-run pass rate. | With model, revision, temperature, suite digest, and environment fixed, run each case three times and report pass^1 and pass^3 separately. | At least 20 deterministic cases; all three raw outcomes retained; three consecutive full harness runs produce the same deterministic-case results; stochastic metrics are labeled estimates with sample counts. | Aggregating infrastructure errors as failures or collapsing repeated trials into an unsupported single score. |
| RF-004: environment identity | Pinning execution identity prevents incomparable runs from entering one trend. | Add an adapter-owned environment fingerprint (interpreter/tool versions and allowed-root-neutral platform facts) to run records, not case contents. | A changed interpreter or verifier version makes comparison fail closed; fingerprints contain no username, absolute path, prompt, credential, or hardware serial. | Fingerprint leaks local identity or makes portable case digests machine-specific. |
| RF-005: judge calibration | A model judge may add diagnostic coverage but is not reliable enough to gate by declaration alone. | Dual-grade at least 30 cases that already have deterministic outcomes; report agreement, false-pass, false-fail, judge model digest, and rubric digest. | Report is reproducible from stored outcomes; 95% confidence intervals are shown; model scores remain advisory regardless of observed agreement. | Any attempt to make `llm_judge` a promotion gate or send private cases to a cloud judge without explicit consent. |

## Standing acceptance and no-go rules

- Promotion remains attended and digest-bound; this work supplies evidence but
  never promotes automatically.
- Default operation remains local/private. No telemetry or case payload leaves
  the machine.
- A manifest is data, never code. Parsing cannot execute setup, imports,
  callbacks, template expressions, or verifier functions.
- A new dependency requires a measured need. These experiments are stdlib-only
  until an existing verifier itself requires an already-supported tool.
- Case or manifest schema changes require a new schema version; v1 readers stay
  fail-closed on unknown fields rather than guessing.
