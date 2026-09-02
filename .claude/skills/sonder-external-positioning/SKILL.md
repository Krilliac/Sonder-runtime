---
name: sonder-external-positioning
description: >-
  Decide what Sonder Runtime may claim publicly, with per-claim proof obligations and
  release-gate commands. TRIGGER when the user asks "how does Sonder compare",
  "is this novel", "write the release notes", "position against" another framework,
  "what can we claim", "benchmark the moat", or drafts any README/blog/announcement
  text. DO NOT TRIGGER for internal eval mechanics, grader wiring, or test-suite
  work — that is sonder-validation-and-qa — or for the venue, file placement, and
  formatting of release notes and docs, which is sonder-docs-and-writing; this
  skill owns what the text may CLAIM and the evidence behind it.
---

# Sonder external positioning: claims, proofs, and release standards

Every outward-facing sentence about Sonder is either grounded in this repository, proven
by a run at the current revision, or it does not ship. This skill is the runbook for
deciding which bucket a claim is in, and for producing the proof.

**Scope**: novelty analysis, competitor comparisons, release notes, README/blog claims,
and the gates a versioned release must pass before any of those claims go live.

**Not this skill**: how the graders, verifiers, or test suites work internally — use
`sonder-validation-and-qa` for that. This skill only tells you *which* of their outputs
are quotable outside the repo and under what conditions.

**Vocabulary** (defined once):
- *Grounded* — the claim points at specific code/docs in this repo at this commit.
- *Proven* — a command was run at this revision and its output supports the number.
- *Background knowledge* — general ecosystem awareness as of 2026-08, **unverified
  against those projects' current state**. Never quotable as measured fact.

The house meta-rule, applied to positioning: a figure measured at one revision must not
be reported as a fact about another. The repo itself models this — retrieval thresholds
carry their calibration date and corpus (`retriever.py`), and outcome-store counts in
`grounded_outcomes.py` are dated in-source. Match that discipline in anything public.

---

## 1. The claim ledger

For each candidate differentiator: what grounds it, what must be proven before it is
claimed externally, and its current status. Status vocabulary: `grounded` (code exists,
verified), `candidate` (plausible, not yet proven by a run), `open` (unproven).

| # | Candidate claim | Grounding (this repo) | Proof obligation before publishing | Status |
|---|---|---|---|---|
| 1 | Execution-grounded learning with mandatory outcome provenance | `sonder_runtime/domain/memory/rules.py`, `grounded_outcomes.py`, `migrations/memory/0002_outcomes_source.py` | None for the mechanism; a run at this revision for any lift number | grounded |
| 2 | Self-modification that proves its own rollback before staying live | `selfmod.py` (`verify_rollback_ready`, `expected_rollback_receipt`) | Show a deploy log with the rollback receipt check | grounded |
| 3 | Privacy-first local learning loop with opt-in sharing | `contribute.py` (20 rules), README consent gates, `docs/wiki/09-security-model.md` | None for mechanism; privacy-history gate for releases (section 5) | grounded |
| 4 | Bounded fleets with provenance-gated negative claims | `master_orchestrator.py`, `fleet_provenance.py` | None for mechanism | grounded |
| 5 | Signed TUF release path + SBOM + in-toto provenance | `tools/tuf_repo.py`, `.github/workflows/build-apps.yml`, `docs/runbooks/publish-release.md` | Gates must actually pass on the tagged build | grounded |
| 6 | Measured "moat" lift from accumulated private data | `scripts/benchmark_moat.py`, `docs/wiki/17-benchmarking.md` | A real warmed-store run at this revision; see section 4 | open |

### Claim 1 — execution-grounded learning

What the code actually does (all verified at this commit):

- Every row in the `outcomes` table **must** carry a `source`; a row without one is
  impossible at the storage layer, and the vocabulary is closed:
  `caller`, `machine`, `attributed`, `self_curriculum`, `unknown`
  (`sonder_runtime/domain/memory/rules.py`, enforced in
  `sonder_runtime/application/memory/outcome_service.py`; tests in
  `tests/test_outcome_source.py`). The source is "a property of the WRITER, never a
  parameter a caller chooses" — the host stamps it at the tool boundary
  (`server.py` writes `OUTCOME_SOURCE_CALLER` / `OUTCOME_SOURCE_MACHINE` /
  `OUTCOME_SOURCE_SELF_CURRICULUM` at distinct call sites).
- `grounded_outcomes.py` attributes later verifications (`test_run`, `build_run`,
  `typecheck_run`, ...) back to pending generations, bounded by a 900-second window,
  a 64-entry ledger, and project scope. Its stated design rule: "A wrong attribution
  is worse than a missing one ... so every rule here fails toward recording nothing."
  Attributed verdicts are additionally barred from driving lesson eviction
  (`EVICTION_INELIGIBLE_OUTCOME_SOURCES`).
- Retrieval thresholds are calibrated, and the calibration provenance is recorded in
  source: `DEFAULT_MIN_SIM = 0.62` was recalibrated 2026-07-06 against a 557-lesson
  corpus via `tune_min_sim.py` (recall 0.95, noise 0.00, best Youden's J), with a
  stricter 0.70 gate for semantically uncorroborated hits (`retriever.py` lines 10-25).
- `calibration.py` refuses to average populations: caller-judged and self-curriculum
  outcomes answer different questions, and below `MIN_SAMPLE = 20` the verdict is
  `unmeasured`, not good ("ignorance fails closed").

**The precise contrast with LLM-as-judge evaluation.** Background knowledge, 2026-08,
unverified against those projects' current state: mainstream eval tooling in the
DeepEval/Ragas style scores outputs primarily with model-graded rubrics. Sonder's
differentiator is **grounded-vs-judged with provenance separation**, not "no judging":
`verifiers.py` ships an `llm_judge` verifier for non-executable artifacts, and its own
docstring calls it a "weak oracle." State the boundary exactly this way — execution
results are the reward where anything executes; model judgment is confined to artifacts
with no executable check; and the `source` column keeps the populations separable
forever. Do not disparage judge-based tools; the claim is about where the ground truth
comes from and that the two populations are never blended into one accuracy figure.

**Numbers you may repeat, with framing.** In-source comments record a snapshot of one
private outcome store (~9,200 rows; 8,883 self-curriculum `tests_passed`; 192
caller-judged at 52.6% good) — `grounded_outcomes.py:3-6` header, as committed at this
revision. A second, differently dated in-source snapshot of the **same store** exists
at `server.py:5801` (9,049 of 9,450 rows `tests_passed`); the two figures were
measured at different times and neither supersedes the other — quote whichever you
cite with its source and date, never blended (cross-noted in
`sonder-memory-and-training`). This is a dated observation about one machine's
history used to justify a design decision. It is not a product benchmark and must
never be quoted as one.

### Claim 2 — selfmod proves its rollback before it counts as deployed

Verified in `selfmod.py`:

- `verify_rollback_ready(run_id)` dry-runs **both** rollback routes — the in-tree
  `rollback()` → `restore()` manifest walk and the emergency recovery route — against a
  throwaway temporary tree, then prints a receipt.
- The expected receipt is derived independently by `expected_rollback_receipt(run_id)`
  from the backup manifest ("derived here, not there"), so a `verify_rollback_ready`
  that has been reduced to a no-op cannot print a matching receipt. This is a
  withheld-receipt design: the checker cannot fabricate the pass value.
- After deployment, `_verify_deployed_rollback` runs the probe as a fresh process
  against the *deployed* code; a missing or mismatched receipt flips the run to
  `rollback_requested` and performs the rollback automatically ("deployed code cannot
  perform a rollback; automatic rollback completed").

**Comparison framing.** Background knowledge, 2026-08, unverified against those
projects' current state: agent self-repair loops in the OpenHands/SWE-agent style
verify the *patch* (tests pass) but do not generally require the deployed artifact to
demonstrate its own undo path before being accepted. Present this as a design
difference you can show in code, not as a measured head-to-head result — no such
measurement exists in this repo.

### Claim 3 — privacy-first local learning loop

Verified:

- `contribute.py` gates lesson export behind a 20-rule `PRIVATE_RULES` classifier
  (paths, emails, credential assignments, vendor token formats, JWTs, opaque tokens,
  private keys, ...). Export is "strictly OPT-IN: nothing here uploads or opens a PR
  automatically" — it writes a local outbox file the user reviews and sends manually.
- Consent gates are explicit environment acknowledgements: cloud prompts leave only
  after `SONDER_ALLOW_CLOUD=1`; pointing `OLLAMA_HOST` at a non-loopback server is
  blocked unless `SONDER_ALLOW_REMOTE_OLLAMA=1` (README, "How it fits together").
  These are separate gates, not one switch.
- `operations.db` telemetry is identifier-only: "identifiers, counts, hashes,
  durations, and redacted paths only — never prompts, memory text, workspace contents,
  or credentials" (`docs/wiki/09-security-model.md`). Redaction failure degrades
  observability, never privacy (`[REDACTION_FAILED]` + metric).

**Comparison framing.** Background knowledge, 2026-08, unverified against those
projects' current state: hosted observability platforms in the LangSmith/Braintrust
style center on shipping traces (including prompt content) to a cloud service. Sonder's
default is the inverse: content stays on the host, and even local operational events
exclude content. Same discipline as claim 1: state the architecture difference, cite
your own code, make no factual assertions about the current behavior of those products.

### Claim 4 — bounded fleets with provenance-gated negative claims

Verified in `master_orchestrator.py` and `fleet_provenance.py`:

- Fanout is admission-capped: `DEFAULT_MAX_AGENTS = 16`, hardware-derived ceiling,
  `ABSOLUTE_MAX_AGENTS = 64` hard clamp; `SONDER_MAX_AGENTS` can lower or raise only
  up to the absolute limit.
- Protected fleet tasks carry `[objective:...|file:...|symbol:...]` markers.
  `fleet_provenance.validate_delegation` runs **before and after** the model call with
  expected task digests, so a delegated target that drifts mid-call is reported as
  `TASK_DRIFT` rather than silently accepted.
- A worker's *negative* claim ("no such implementation exists" and variants, matched by
  the `NEGATIVE_CLAIM` regex) is not accepted without tool evidence — read/search
  evidence markers from `file_read`/`text_search`-class tools must be present, else the
  output is failed with `EVIDENCE_REQUIRED`. This is the provenance-gated-absence rule:
  the fleet cannot assert absence it did not look for.

### Claim 5 — signed release path

Verified in `tools/tuf_repo.py`, `docs/runbooks/publish-release.md`, and
`.github/workflows/build-apps.yml`:

- python-tuf end to end, no custom signature code. Role thresholds:
  root **2 of 3** keys, targets 2 of 3, snapshot 1 of 1, timestamp 1 of 1
  (`THRESHOLDS` / `KEY_COUNTS` in `tools/tuf_repo.py`). Expiries: root 365 d,
  targets 90 d, snapshot 7 d, timestamp 1 d — freeze protection by construction.
- The `build-apps` workflow's tagged-release job runs
  `python scripts/check_release_version.py --require-release --json` and
  `python scripts/check_history_privacy.py --require-clean --json` and requires the
  integrity artifact: `SHA256SUMS`, `sonder-runtime-sbom.cdx.json` (CycloneDX 1.5),
  and `sonder-runtime-provenance.intoto.json` (in-toto statement with SLSA provenance
  fields). All three are required release assets; publication fails if any is absent.
- Say it exactly as the runbook does: the SBOM/provenance metadata "is evidence, not a
  signature" — cryptographic trust is the TUF chain, and the in-toto statement is
  currently **unsigned**. Overstating this one is the easiest way to publish a false
  security claim.

### Claim 6 — the measured moat (open until you run it)

The claim "accumulated private data measurably improves the local model" is **open** at
any given revision until `scripts/benchmark_moat.py` has been run against a real model
and, for a personal claim, a real warmed store. See section 4 for exactly what the
harness does and does not prove.

---

## 2. Known-practice register — where Sonder deliberately follows the ecosystem

Claiming novelty for any of these would be wrong. They are sound choices, and the
honest positioning is "standard, on purpose":

| Practice | Where in this repo | Status vs ecosystem |
|---|---|---|
| MCP as the tool protocol | `mcp==2.0.0` in `requirements-runtime.txt`; `python -m sonder_runtime mcp` | Standard protocol, standard use |
| OpenAI-compatible HTTP API | README quick start; `/v1/chat/completions` on `127.0.0.1:11435` | De-facto ecosystem interface |
| Transactional outbox | `docs/adr/ADR-004-transactional-outbox.md` (outbox_events per state DB, projected into operations.db) | Textbook pattern, applied locally |
| Ports-and-adapters / modular monolith | `docs/architecture/adr/ADR-001`, `ADR-004-ports-and-adapters.md` | Standard architecture styles |
| Strangler-fig migration | The WP1 slice series (README tail; `docs/architecture/WP1-*.md`) | Named, standard migration pattern |
| RRF hybrid retrieval | `retriever.py` (`rrf_scores`, k=60, FTS5 + embeddings) | Standard IR fusion technique |
| QLoRA / PEFT training stack | `requirements-train.txt` (`peft`, `bitsandbytes`), `qlora_train.py` | Standard fine-tuning stack |
| Ollama as the model server | `docs/architecture/adr/ADR-002-ollama-external.md` | Standard local serving choice |

Note the ADR numbering trap when citing: there are two ADR-004 files —
`docs/adr/ADR-004-transactional-outbox.md` (SPEC-5 era) and
`docs/architecture/adr/ADR-004-ports-and-adapters.md` (architecture-program era). Both
directories are historical namespaces; new ADRs are date-prefixed under `docs/adr/`.
Cite the full path, never the bare number.

---

## 3. Honest boundaries — the README's own list, restated faithfully

Any outward positioning piece must include these, in substance (README, "Honest
boundaries" section; SECURITY.md, "What this software does, stated plainly"):

1. "A small local model is not a frontier model." Delegate bounded transformations,
   give it the facts, review its work.
2. "Learning is grounded only when a caller records a real outcome; self-graded
   success is not treated as proof."
3. Multi-PC inference (`SONDER_OLLAMA_WORKERS`) is "request-level pooling, not
   model-weight sharding or shared-memory GPU federation." Never describe it as
   distributed inference of one model.
4. NPU support "is an optional utility path ... token generation remains on the model
   server's CPU/GPU path." Never a fourth generative tier (ARCHITECTURE.md agrees).
5. The unsafe lab "removes model-loop host-tool policy; it does not provide OS
   isolation."
6. The runtime executes code by design. SECURITY.md: "Those are the product, not a
   bug ... Treat access to a Sonder endpoint as equivalent to shell access on the
   host." Remote access claims must carry this framing, plus "Never expose the
   convenience loopback service directly to a network."

The measured offload guidance is also part of honest positioning
(`integrations/README.md`): "Send it transformation, not recall" — over seven judged
offloads, transformation was 4/4 usable and recall 3/3 wrong. Quote it with its sample
size; it is seven observations, not a benchmark.

---

## 4. Evidence standards for any number quoted externally

**The identity rule.** The evaluation-history store "reports trends only within an
exact model + model digest + suite + suite version + suite digest identity"
(`sonder_runtime/adapters/evaluation_history_store.py`). Apply the same rule to prose:
any externally quoted number names its suite, suite version, and model digest, or it
does not ship. `scripts/benchmark_adaptive.py` enforces the same for checkpoint
comparisons (model digest, suite digest, hardware digest, exact task-name set).

**The moat harness** (`scripts/benchmark_moat.py`) is the prove-the-moat instrument.
What it actually does, per its own docstring and `docs/wiki/17-benchmarking.md`:

- Runs one bounded suite (`DEFAULT_SUITE`, 6 tasks) three ways against the **same**
  model: `bare`, `runtime_cold` (retrieval path, empty store), `runtime_warm`
  (retrieval path plus warmed lessons/facts). Deterministic graders (substring,
  all-of partial credit, regex, integer-parse); graders check the answer, never the
  augmentation text.
- Headline number is warmed − bare; cold − bare is the do-no-harm honesty check
  (0 by construction today, kept as a tripwire for future scaffolding overhead).
- What it does **not** prove, verbatim scope limits: it measures retrieval-augmentation
  lift on a fixed model on a bounded task set; it is not a general capability
  benchmark; the default warm lessons are authored, not distilled, so the default
  suite demonstrates the mechanism only; with a fake `model_fn` it measures the
  harness, not a model; one stochastic run is a point estimate — fix temperature
  (CLI default 0.2), run more than once.

```bash
# Real run: repo root, runtime venv, Ollama serving the selected model.
python scripts/benchmark_moat.py                              # scorecard to stdout
python scripts/benchmark_moat.py --json out.json --markdown card.md
python scripts/benchmark_moat.py --temperature 0.0            # most deterministic
```

A *personal-moat* claim (about a real accumulated store, not authored lessons) requires
injecting your own `retrieve_fn(task, warm)` pointed at the live `memory.db` — the wiki
documents this as the run "that produces a claim about *your* moat."

**Known harness divergence, disclose if quoting.** `build_augmented_prompt` is pinned
to the pre-2026-08-10 prompt shape and no longer mirrors `orchestrator.build_prompt`
(fact fencing, task directive). The divergence is recorded in-source and must only be
synced together with a re-baselined run — so moat numbers are comparable across runs of
the same harness version, and must not be presented as measuring the exact current
runtime prompt shape.

**Release channel rule.** `app-latest` is a mutable prerelease snapshot that "may lag
`main`" and "is not a versioned, release-ready build" (README). Versioned claims come
only from `app-vX.Y.Z` releases that passed the version, artifact-integrity, SBOM, and
provenance gates. Never attach a number or capability claim to `app-latest`.

---

## 5. Release and positioning workflow

The pipeline any outward statement rides on, in order:

1. **Version identity** — `docs/runbooks/release-version-policy.md`: one public SemVer
   at publication; runtime `sonder_version.VERSION` must be stable `X.Y.Z`, Flutter
   `X.Y.Z+BUILD`, tag `app-vX.Y.Z`, build revision a full 40-char SHA. Check:

   ```bash
   python scripts/check_release_version.py --json                       # diagnostics
   python scripts/check_release_version.py --tag app-v1.2.3 \
     --revision <full-40-char-sha> --require-release --json             # release form
   ```

2. **History privacy** — `python scripts/check_history_privacy.py --require-clean --json`
   against complete non-shallow history. The release form refuses to publish while any
   pinned sensitive object/path pair remains reachable; deleting a file only at `HEAD`
   is intentionally insufficient (publish-release runbook).

3. **Artifact integrity** — the `build-apps` release job requires exactly one artifact
   per supported platform, opens each archive, refuses release without `LICENSE`, and
   requires `SHA256SUMS` + SBOM + provenance as named release assets.

4. **Cryptographic trust** — the TUF ceremony (`docs/runbooks/publish-release.md`):
   `python tools/tuf_repo.py init <repo>` once on the offline signer, then per release
   `python -m sonder_runtime update build ...` and
   `python tools/tuf_repo.py bundle <tuf-repo> <build-out> <publish-dir>`, then verify
   through the real trust path (`verify_bundle_trust(..., allow_unverified=False)`
   must print `tuf`) before distributing. Root rotation is a re-signed sequential
   `root.json`, never a key swap in place.

5. **Ecosystem surface** — `integrations/` is the outward-facing agent surface:
   `integrations/IMPORT_PROMPT.md` (paste-at-your-agent setup),
   `integrations/claude/CLAUDE.sonder.md` + `mcp-config.md`,
   `integrations/codex/AGENTS.sonder.md` + `config.toml.example`. Its guidance
   "describe[s] behaviour that was measured, not assumed" — keep any edits to it under
   the same evidence standard as this skill.

---

## 6. Before-you-publish checklist

Run through this for every external statement — release notes, comparison table, blog
paragraph, README edit:

- [ ] Is every claim grounded in a file you can cite at the current commit?
- [ ] Was every quoted number produced by a run **at this revision**, with suite +
      suite version + model digest named?
- [ ] Are all ecosystem comparisons labeled *background knowledge, 2026-08, unverified
      against those projects' current state* — never phrased as measured fact?
- [ ] Does the piece include the honest-boundaries substance (section 3), including
      the SECURITY.md executes-code-by-design framing if remote access is mentioned?
- [ ] Is the claim about `app-vX.Y.Z` (gated) rather than `app-latest` (mutable)?
- [ ] Privacy gates clean: `check_history_privacy.py --require-clean` passes, and no
      example output contains anything the 20 `PRIVATE_RULES` in `contribute.py`
      would redact?
- [ ] Is anything unproven labeled `open` or `candidate` rather than asserted?
- [ ] Does nothing contradict README "Honest boundaries", ARCHITECTURE.md, or
      SECURITY.md? (Those documents win over marketing instinct, always.)

---

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All file paths, thresholds, role
counts, gate commands, and quoted phrases above were read from the working tree at
that commit; no external project was inspected, and every comparison to other tools is
labeled background knowledge for that reason.

Re-verification one-liners (repo root):

```bash
git log -1 --format=%h                                              # still 99162cf9?
python -c "import contribute; print(len(contribute.PRIVATE_RULES))"  # must print 20
grep -n "THRESHOLDS\|KEY_COUNTS" tools/tuf_repo.py                   # root 2-of-3
grep -n "fails toward" grounded_outcomes.py                          # attribution rule
grep -n "DEFAULT_MIN_SIM\|Recalibrated" retriever.py                 # calibration provenance
grep -n "verify_rollback_ready\|ROLLBACK_RECEIPT_PREFIX" selfmod.py  # rollback proof
grep -n "check_release_version\|check_history_privacy" .github/workflows/build-apps.yml
grep -n "request-level pooling\|not a frontier model" README.md      # honest boundaries
grep -rn "NEGATIVE_CLAIM" fleet_provenance.py                        # provenance-gated absence
python scripts/benchmark_moat.py --temperature 0.0 --json moat-check.json  # needs Ollama serving; writes to cwd, delete after
python scripts/check_release_version.py --json                       # version identity
```

If any command's output no longer matches this document, update the affected section
and re-stamp this block with the new commit and date before relying on it.
