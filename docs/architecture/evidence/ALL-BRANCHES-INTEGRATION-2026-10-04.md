# Runtime remote-branch integration, 2026-10-04

Baseline: `2713303d3ec255c547819fabb3f33f3f36eedbaa` (qualified shared-redaction/HTTP main).
This union incorporates all nineteen audited remote non-main branch tips as Git
ancestors. Eight feature branches carry implementation modules absent from that
baseline; they are included alongside four defensive code changes and the
historical diagnostic harness. The five fix branches already have merged PR
receipts; their folds preserve current behavior. Associated-PR queries returned
no PRs for the eight new feature tips, four defensive tips or diagnostic tip.

| Branch | Audited tip | Integration decision |
| --- | --- | --- |
| `codex/tls-ci-timeout-diagnostic-20261002` | `625380e840c9399536ea9a045495b081d26cc8b2` | Retained harness; separate opt-in PR workflow, preserving required CI. |
| `feat/atif-trajectory-export` | `815b803f0bd3213c0e8ed1b25c7666cf802fb9b8` | Additional bounded ATIF-v1.7 export; default formats retain current privacy behavior. |
| `feat/effect-journal-hash-chain` | `fc9d1d06b753cee2332aa2c03635493e37e92fba` | Additive hash-chain evidence and optional recorded-response replay; content capture remains opt-in. |
| `feat/evaluator-cheat-trials` | `d6804d3238310aac818ba355aaf4d1cf32103813` | Protected-write and evaluation-integrity evidence; atomic promotion and rollback remain intact. |
| `feat/external-resource-leases` | `61a5c32b25cf289e6d0a2a9314acd2b6e8bbb8e2` | Durable cooperative resource leases plus desktop ownership; control references retained. |
| `feat/inference-profile-routing` | `45a3e093aa423c3a23f9e356419972d2332015e5` | Already reachable from main; associated merged [PR #596](https://github.com/Krilliac/Sonder-runtime/pull/596). |
| `feat/memory-fact-validity` | `bd6e84ebd241accd50be1c07a61001c15ac751f7` | Explicit validity/supersession and additive unbackfilled migration; authoritative scope fences retained. |
| `feat/powershell-ast-gate` | `a3eb7226364ee53633bbfa5d652ee0bdb43adaac` | Argument-aware native parser inspection, with typed traits and existing rule/approval precedence. |
| `feat/prefix-cache-telemetry-prewarm` | `3be563c48a97c009030c46111ea99981f8876445` | Content-free cache evidence/local prefill, preserving playbooks, provider usage, cloud refusal and per-model residency. |
| `feat/resume-reality-barrier` | `1872c79451e69a5cf763d606088cafe7e279edd5` | Bounded host observations before resumed mutation; retain ownership, effect recovery and deferred-verification rules. |
| `fix/a8-decision-alias-hook` | `14fe2cfaaaa9f64064d905f888304add1be2f74c` | Already merged [PR #646](https://github.com/Krilliac/Sonder-runtime/pull/646); retain current normalization and regenerate catalogs. |
| `fix/chat-golden-linux-only` | `b85332edca24daff3cfce872f4ca3184316e99da` | Already merged [PR #629](https://github.com/Krilliac/Sonder-runtime/pull/629); tree-preserving ancestry fold. |
| `fix/default-app-test-leaks` | `6695237210637c3d37b680e6ba61ffe6da6d45c0` | Already merged [PR #644](https://github.com/Krilliac/Sonder-runtime/pull/644); tree-preserving ancestry fold. |
| `fix/long-path-creation-test` | `8d816c933f19408c63cc747dda8fa46595e84c5c` | Already merged [PR #645](https://github.com/Krilliac/Sonder-runtime/pull/645); retain newer portable-workspace tests and Windows-safe path bounds. |
| `fix/windows-local-test-parity` | `8748bcb94c75855bf5562f840a87b03228200d11` | Already merged [PR #630](https://github.com/Krilliac/Sonder-runtime/pull/630); tree-preserving ancestry fold. |
| `sec/sec-http` | `2a03c0dc42ccfa6ba8e09f28104690f1f6b43faa` | Integrate origin-header validation; qualify ordinary HTTP/CORS behavior. |
| `sec/sec-path` | `f60ec967f0f2bffb480cf573da0ab85c586119a3` | Integrate pinned debug-run containment; qualify ordinary real process/debug lifecycle. |
| `sec/sec-redos` | `7ef3ffced602a78fd4077bc8eb08334a5d787c5d` | Integrate parser changes and ordinary intent/C++ symbol/summary parity fixtures. |
| `sec/sec-tls` | `6b0923182940343b2c271e3d845239977c4e1624` | Explicit TLS 1.2 minima; preserve current deterministic private-CA controls. |

## Conflict and compatibility decisions

Generated architecture/runtime references are regenerated from the completed
union using `scripts/generate_documentation_catalogs.py --write`, rather than
selecting a stale branch snapshot. Local prefix composition keeps framed owner
playbook notes and usage accounting; dynamic lane context stays in the request
prompt. OpenRouter usage metrics, cloud consent, bounded batch admission,
per-model keep-alive, atomic checkout promotion and deployed-path rollback are
retained. Desktop state keeps both control references and lease ownership.
PowerShell inspection adds its risk floor after typed traits, retaining existing
rule/approval precedence and the resume mutation barrier.

The ATIF default-format parity golden was independently regenerated through the
unchanged baseline checkout using the branch's deterministic fixture helper.
All sixteen outputs preserve current main's transcript redaction metadata and
privacy markers. Invalid new export formats/empty ATIF turns use `InvalidInput`
instead of adding legacy `ERROR:` signals. Fact-validity migration tests retain
the current public receipt contract: repeated migration reports the complete
applied ledger, with no pending migrations or changed legacy facts.

Ordinary resource-failure controls exposed two inherited lease defects. Failed
schema/transaction admission now immediately closes the created SQLite
connection. Same logical-owner reacquisition refuses explicitly changed PID or
process identity before renewing; an explicit release/fresh acquisition or a
new owner id with the existing cleanup rule is required. Four controls failed
before these corrections and passed after them. These are cooperative host
leases, not an authentication boundary for arbitrary in-process code.

The historical diagnostic branch replaced `ci.yml` and explicitly disclaimed
use as required product gates. Its workflow is retained separately as
`.github/workflows/tls-ci-timeout-diagnostic.yml`, PR-label opt-in only (tls-diagnostic), with
its historical pinned comparison revisions. Required `ci.yml` is byte-identical
to baseline. No required context is renamed or relaxed.

Three WIP scratch programs (`_bench_chain.py`, `_scratch_parity.py`,
`_scratch_pw_probe.py`) remain available in the merged branch history but are
excluded from the published runtime tree. Crafted socket/link-swap and
pathological-input reproduction fixtures from the defensive branches are also
excluded. Ordinary functional controls qualify the integrated code; this is not
an adversarial reproduction or a verified attack-resistance claim.

## Local qualification

Interpreter: `/workspace/sonder-qualified-venv/bin/python`, Linux CPython 3.12.
Focused executions overlap and are not a unique-test total:

- ATIF/fact-validity/PowerShell/evaluation-integrity cohort: **360 passed,
  64 skipped** (native PowerShell parser unavailable).
- Persistence, effect recovery/hash-chain, migrations, leases/desktop and
  prefix-cache cohort: **178 passed**.
- Existing gateway/batching, atomic deploy/rollback, shared export privacy,
  HTTP/TLS, debugger, intent/symbol, playbook/prewarm and metric contracts:
  **683 passed, 7 skipped**. Skips are documented adapter seams, unavailable
  private worker and POSIX share-deny behavior.
- Local-system packaging/release contracts: **40 passed**; real
  `scripts/package_local_system.py --out dist/local-system` build succeeded.
- Diagnostic fake smoke: all five cap/failure/preflight/launch/timeout controls
  passed with no owned process left behind. Reduced thresholds apply only in
  disposable smoke copies.
- Architecture, requirement evidence, error-signal, lint-ratchet,
  history-privacy, doc-link, documentation-authority and generated-reference
  checks pass; history privacy retains seven acknowledged baseline debts and
  introduces none. No ratchet limits were increased.

The coordinator folded all twenty-four original local branch tips after mapping
their current implementations to merged receipts; the eighteen additional
non-ancestor tips were folded without changing this semantic source tree. All
forty-three original local/remote tip refs are actual ancestors of the union.

The first complete four-worker native run exposed **43 failures, 25,310 passes,
407 skips**. All failure groups were addressed without dropping feature code or
relaxing gates. Retry tests had replaced shared stdlib `time.sleep`, so the new
Git snapshot subprocess polluted their simulated retry clock; a private
controller sleep seam retains fresh snapshots and resume enforcement. The
prefix feature intentionally uses versioned local-system/3 ordering (identity,
profile, framed owner playbooks, request, emotions, goal); existing parity and
emotion controls now check that explicit layout, with independent expected
joining and the current hosted-context fence. The prewarm ownership control
isolates model-context discovery and verifies refusal produces zero requests,
then recovery makes exactly one one-token prefill with the expected context.
Production prewarm uses the platform option policy directly. The REPL refusal
double accepts the new PowerShell argument context; diagnostic pins retain the
same immutable SHAs with their existing main's human-readable v7 labels.

Those seven failure groups plus prefix-cache and resume permission controls
pass **211 tests, one platform skip**. Final complete-suite, process-contention,
interoperability and protected hosted checks remain coordinator merge gates.
Native Windows PowerShell, real model/GPU performance and external services are
not demonstrated by this Linux functional qualification. Mock/provider fixtures
make no model-quality or throughput claim.

Hosted qualification flagged historical-code execution in a privileged manual
dispatch context as a default-branch cache risk, even after dependency caching
was removed. Historical comparisons therefore run only in the pull-request
context, opt-in by the `tls-diagnostic` label. There is no privileged manual
dispatch event or shared dependency cache. Required `ci.yml` stays unchanged.

## Hosted qualification repairs

The final application source of local revision `88598a500c7b4a68f1d38605fa28842321fb134b`
passed **25,353 tests, 407 skips, four subtests** in 661.42 seconds. Workflow and
evidence changes produced revision `7bf7d734b2ff6ee5abd4695b5a9197accad7b81f`;
all three CodeQL analyses and the aggregate passed there. Premerge interoperability
on that head, Inference `ff883402` and Observatory main `9ea5f788` passed all
**30 gates in 79.3 seconds**, including Chromium/Flutter, shutdown/fallback,
fourteen process-group cleanup and unchanged Git/home paths.

The hosted full run on `7bf7d734` found **two failures, 25,442 passes, 309 skips,
four passing subtests** in 1,094.09 seconds. Its 64 native PowerShell cases
executed without skips: 63 passed and one classification case failed. The
native helper now treats escaped literal command heads as requiring inspection,
preserving ordinary literal escapes in arguments and nested scriptblock data.
It continues to use only `Parser.ParseInput` with JSON stdin and never executes
candidate text. Four new controls use benign command/data spellings; no
decoding or evaluation path was added.

The other failure was the two-service resume test's stale refusal whitelist.
The existing revision fence correctly refused a contender after its sibling
committed revision 8. The test now admits only the exact existing typed refusal,
retains one winner/one loser/one runner and exact ambiguous-claim reconciliation,
and deterministically orders a losing contender after the winner enters.
Production revision fences and effect recovery are unchanged. The old whitelist
failed that deterministic control; the corrected durable-resume cohort passed
**29 tests**. The combined native parser, permission/generated/surface and
durable-resume cohort passed **269 tests with no skips** using official MIT
PowerShell 7.5.3, whose archive SHA256 matches its pinned release asset digest.
The parser tool is outside the repositories. Independent review found no
blocking regression. Fresh full-suite and exact-head protected hosted checks
remain merge gates for this repair revision.
