# Model trials: the strongest local model, then a three-model escalation ladder (2026-09-03)

Two live trials handed the same tasks to Sonder through its real surfaces
(the terminal REPL and the OpenAI-compatible served API), first with every
tier bound to the strongest model the host could run, then with three
distinct models on the capability ladder so the new automatic escalation had
somewhere to go. This note records the host, what was measured, what each
model did with each task, where escalation fired, and what the runtime
learned from it.

## Host and runtime

| Item | Value |
| --- | --- |
| CPU | 4 vCPU, Intel Xeon @ 2.10 GHz, no GPU |
| Memory | 15 GB |
| Model server | Ollama 0.33.2, extracted from the Docker Hub image layers (the release download host was unreachable from this container), CPU backend only |
| Sonder source | the escalation worktree (this change) on top of `27009dcd` |
| Lab | a seeded git workspace (`ledger/core.py` with a sign defect in `balance()`, one failing test) reset before every task; `SONDER_HOME` fresh per configuration; permission mode `auto`; `SONDER_TIMEOUT=1800` |

Pulled models (all Q4_K_M):

| Tag | Parameters | Role in the ladder |
| --- | --- | --- |
| `qwen2.5-coder:1.5b` | 1.5 B | `fast`, `code`, the `sonder:latest` alias in trial 2 |
| `qwen2.5:3b` | 3 B | the general-chat model in the multi-model paths |
| `qwen2.5-coder:7b` | 7.6 B | `general` in trial 2 |
| `qwen2.5-coder:14b` | 14.8 B | every tier in trial 1; `reasoning` in trial 2 |
| `nomic-embed-text` | 137 M | embeddings |

Measured throughput on this host (one warm request each, 8 k context):

| Model | Prompt tokens/s | Generation tokens/s |
| --- | --- | --- |
| `qwen2.5-coder:1.5b` | 186 | 22.8 |
| `qwen2.5-coder:14b` | 19.5 | 2.9 |

## Tasks

| Id | Surface line | Verified how |
| --- | --- | --- |
| T1 | Read `ledger/core.py` and explain in three sentences what `balance()` computes and where it is wrong | reading the answer against the seeded defect |
| T2 | `/work` fix the defect so `balance()` subtracts credits, run the tests, report | an independent `pytest` run of the workspace after the task, plus `git diff --stat` |
| T4 | ledger arithmetic (three entries, per-account balances, zero-sum check) | the arithmetic (cash 1250, revenue -1000, expenses 250, loan -500; sum 0) |
| T5 | summarise `README.md` in one sentence | reading |
| T6 | T4 prefixed with "Think step by step" (the router's reasoning cue) | as T4; also which tier answered |

T3 (add `trial_balance()` with a test) was dropped from the live runs: at
the 14B's step rate it alone would have taken longer than the rest of the
trial, and T2 already exercises the edit-validate-report loop.

## What the first attempt taught before any result

The first run of trial 1 produced no model output at all, twice over:

- Every REPL line was a natural work request, and with no workspace selected
  the REPL answered "Where should I create or work on this project?" and
  never reached a model. The driver now sends `/workspace <dir>` first.
- The served API classified T1 as workbench work and ran the agent, whose
  first decision call on the 14B hit the runtime's default 300 s model-call
  limit: the agent step prompt is several thousand tokens and this host
  processes them at ~20 tokens/s. `ERROR contacting local Ollama ... timed
  out` after 300 s, checklist 1/4, no answer. The lab sets
  `SONDER_TIMEOUT=1800`; the troubleshooting table in the onboarding page
  now says so.

## Trial 1: every tier on the strongest model (`qwen2.5-coder:14b`)

`fast`, `code`, `general` and `reasoning` were all bound to the 14B and
`sonder:latest` aliased to it, so the escalation plan collapsed to one rung:
this trial measures the model, not the ladder.

| Task | Surface | Wall | Model / tool calls | Outcome |
| --- | --- | --- | --- | --- |
| T1 | REPL, workbench agent | 672 s | 2 / 8 | Correct. It read the file and named the defect ("credits are added instead of subtracted"). About 560 s of the wall was the first decision: a ~12k-token agent prompt at ~20 tokens/s. |
| T2 | REPL, `/work` | 1080 s | 12 / 14 | Fixed. A one-line change to `ledger/core.py`; the agent ran the tests itself, and the independent `pytest` afterwards passed 3/3. |
| T4 | REPL, chat | 382 s | 1 / 0 | Wrong on the account signs: it booked the cash movements as credits to cash, noticed the balances did not sum to zero, and "corrected" itself mid-answer. The arithmetic within each attempt was consistent. |
| T5 | REPL, chat | 93 s | 1 / 0 | Never reached the file. The work classifier did not treat "Summarize README.md in one sentence." as workspace work, so the plain chat route answered that it cannot read files. Fixed in this change (below). |
| T1 | served API, workbench agent | 924 s | 8 / 11 | Wrong: "ledger/core.py was not found". The served route scopes routed work to the request's `project` field; the lab sent none, so the agent ran with no workspace. The driver now sends the workspace as `project`. |
| T4 | served API, chat | 271 s | 1 / 0 | The sum-to-zero check passes, but two balances carry the wrong sign (cash 250 instead of 1250, loan +500 instead of -500). |

On T4 in both surfaces: the prompt's compact convention ("cash from
revenue 1000" moves 1000 into cash) was read inconsistently by the 14B.
That is a capability observation, not a runtime defect, and it shows the
limit of objective escalation: no verifier exists for a chat answer, so a
wrong-but-confident answer never steps up (see Limitations).

Two findings changed code or the lab:

- **A file summary is workspace work.** `intents.classify_work` now treats
  summarize / summarise / describe / outline of a file-like target (a path,
  or a name with a letter extension of at least two characters) as work, so
  the request reaches the workbench and the file; the same verbs on a topic
  ("summarize this conversation") stay chat. Pinned in `tests/test_intents.py`.
- **The served route needs `project`, and only a local-open deployment
  passes it through.** Routed workbench work on the OpenAI-compatible
  surface is scoped to the request's `project` field; the lab then sent
  the workspace, and trial 2 showed that an authenticated deployment
  namespaces the field to an opaque id before routing (findings below).

## Trial 2: three models on the escalation ladder

`sonder:latest`, `fast` and `code` bound to the 1.5B, `general` to the 7B,
`reasoning` to the 14B. For a code-class request the plan is therefore
`sonder (1.5B) -> general (7B) -> reasoning (14B)`, and a reasoning-class
request starts on the 14B with the 1.5B as its fall-back. The container was
restarted mid-trial (after T4); the remaining tasks ran against the same
worktree after the Ollama server and the alias were restored.

| Task | Surface | Wall | Outcome |
| --- | --- | --- | --- |
| T1 | REPL, workbench agent | 768 s | **Escalated.** The 1.5B could not drive the loop; the runner reran on the 7B, which read the file and named the defect. Output led with `model escalation: code (qwen2.5-coder:1.5b) -> general (qwen2.5-coder:7b): failed`. |
| T2 | REPL, `/work` | 119 s | **Not escalated, and wrong.** The 1.5B made 12 model calls and 7 tool calls (checklist updates and one `artifact_verify` of a path that does not exist), changed no file, ran nothing, and returned a final answer; the run was marked unverified but, having finished, it stood. Independent `pytest`: 1 failed, 2 passed; empty diff. This is the observation that added the vacuous-completion rule (commit `d16e6ae3`): the same run now steps to the 7B and then the 14B. |
| T4 | REPL, chat | 73 s | **Not escalated, and wrong.** The 1.5B booked every entry as a positive balance, found the sum was 1750, declared "a mistake in the problem statement" and repeated itself. Nothing objective failed, so nothing stepped up. |
| T5 | REPL, workbench agent (after the file-summary routing fix) | 919 s | **Escalated twice, then the top rung could not load.** The request now reached the workbench; the 1.5B and then the 7B could not drive the loop, and the runner stepped to the 14B, whose load Ollama abandoned after its own start-up timeout (`HTTP 500 ... timed out waiting for llama-server to start`, a cold page cache after the container restart). The output led with both steps and ended with that error: the ladder was spent by the model server, not by the runtime. |
| T6 | REPL, chat (reasoning-class prompt) | 421 s | **Pre-routed to the 14B, then fell back.** The plan started on the bound `reasoning` tier as designed; the 14B call failed after 301 s (Ollama abandoned its load again) and the turn fell back to the default route: `model_escalation chat: reasoning (qwen2.5-coder:14b) -> sonder (sonder:latest): failed`. The 1.5B then answered, wrongly (it reached cash 1250, expenses 250, loan 500, summed them to 2000, and looped over an imagined "2500" for expenses). |
| T1 | served API, workbench agent | 1124 s | **Escalated to the 7B, which then ran without a workspace.** The 1.5B could not drive the loop; the 7B read the task and asked for `ledger/core.py` three times, and each read resolved against the wrong base (see the findings below), so the no-progress guard ended the run. The route header named the tier that answered (`tier: general -> qwen2.5-coder:7b`). |
| T4 | served API, chat | 53 s | **Not escalated, and wrong** (the 1.5B: expenses -250, sum "1250", then "confirming" zero). No verifier, nothing steps up. |
| T6 | served API, chat (reasoning-class prompt) | 598 s | **Pre-routed to the 14B, which answered.** With the page cache warm the 14B loaded in time; the zero-sum check carried the correct balances (cash 1250, loan -500, revenue -1000, expenses 250; sum 0) although the per-account section above it had the loan and revenue signs flipped. The served receipt named the default route rather than the reasoning model; fixed in `1c8ec263` (the observer now reports every attempt's target). |

Findings for the owner (recorded, not changed here; both touch the
filesystem containment and the served privacy boundary, which are
maintainer-intent changes):

- **The served route cannot scope routed work to a directory on an
  authenticated deployment.** `_hosted_storage_id` namespaces `project` to
  an opaque per-principal id before `_handle_work_intent` routes the
  request, so the workbench agent runs with no project scope even when the
  client names its directory; only a local-open deployment passes it
  through. A client-chosen directory would also become the agent's
  authorized root (`_project_scope_args`), so passing it through is a reach
  decision, not a bug fix.
- **Relative paths with no project scope resolve against the package
  directory.** `file_ops.workspace_root()` returns the directory of
  `file_ops.py` itself, so the 7B's `ledger/core.py` became
  `sonder_runtime/adapters/filesystem/ledger/core.py`, contained and
  harmless but never what a caller means. The line-range read also reported
  that as "image file not found"; that wording is fixed here.
- **Ollama's default 5-minute load timeout is too short for a 9 GB CPU
  load with a cold page cache** (mmap is disabled for CPU loads). After the
  container restart the 14B failed to load twice (T5 and the REPL T6);
  restarting Ollama with `OLLAMA_LOAD_TIMEOUT=30m` and warming it once
  (59 s) made the rung reachable for the reruns below.

## Trial 2, warm rerun: the 14B rung reachable

Ollama restarted with `OLLAMA_LOAD_TIMEOUT=30m`; one warming request loaded
the 14B in 59 s. Same bindings as trial 2, run against the worktree at the
receipt fix (`1c8ec263`).

| Task | Surface | Wall | Outcome |
| --- | --- | --- | --- |
| T5 | REPL, workbench agent | 1048 s | **Escalated twice and answered at the top rung.** `code (1.5B) -> general (7B): failed; general (7B) -> reasoning (14B): failed` led the output; the 14B read the file and summarised it correctly in one sentence ("a tiny double-entry ledger that ensures the totals of all entries sum to zero"). The full ladder, end to end, on one request. |
| T6 | REPL, chat (reasoning-class prompt) | 578 s | **Pre-routed to the 14B, which answered wrongly** (it summed 1250 + 1000 + 250 + 500 and called the setup mistaken). An answer stood, so nothing fell back: without a verifier the ladder cannot see a wrong answer. |
| T6 | served API, chat (reasoning-class prompt) | 163 s | **Pre-routed to the 14B; the receipt now names it** (`model qwen2.5-coder:14b`, `tier reasoning`, the fix from `1c8ec263` live). The answer was again wrong on the same convention (sum 1250, "a discrepancy"). |

## The multi-model paths that already existed

Run in-process against the worktree with four distinct models bound
(`fast` 1.5B, `code` 7B, `general` `qwen2.5:3b`, `reasoning` 14B), all on
the T4 ledger prompt.

| Path | Wall | What happened |
| --- | --- | --- |
| `consult` | 469 s | Asked the `code` (7B, 134 s) and `reasoning` (14B, 252 s) tiers independently and reported both answers under one verdict: `tiers DISAGREE (heuristic only) - confidence unknown`. The 7B's answer ended in a chain of subtractions that "reached" zero from 2500; the 14B's reasoned from the accounting equation and stopped short of the zero-sum check. Divergence is exactly what consult exists to surface: the caller is told to verify. |
| `ensemble_answer` | 1533 s | All four models answered (`fast` 1.5B in 28 s, `code` 7B in 176 s, `general` 3B in 239 s, `reasoning` 14B in 344 s) and the 14B synthesised one answer: wrong (cash 1500 before a "re-evaluation" to 1250; revenue and loan with the wrong sign). Four models did not make the compound answer right; the runtime's own note on `consult` ("measured ensembles did not improve accuracy") held here too. |
| `model_fanout` | 349 s | First refused (`login required`): on a deployment that authenticates callers the tool wants a developer account, and the lab's API key is not one. Rerun on a local-open deployment (no API key) with `OLLAMA_MAX_LOADED_MODELS=1`: 5 local chat models answered the same prompt serially (qwen2.5-coder:1.5b 23s (cut at the token limit), qwen2.5-coder:14b 178s (cut at the token limit), qwen2.5-coder:7b 86s (cut at the token limit), qwen2.5:3b 40s (cut at the token limit), sonder:latest 22s (cut at the token limit)), the embedding model was skipped as non-chat, and the receipt states `prompt_leaves_machine: false` with no cloud target selected. Every answer got the ledger wrong in its own way; the receipt is the point, not the consensus. |

## The live eval harness, per model

`python eval_harness.py run --suite smoke_python --provider ollama:<tag> --live`
against the worktree (`--no-record-history`, so the lab never wrote the
repository's durable evaluation history). The suite is three function-writing
scenarios plus one built-in task, each verified by execution.

| Model | Trials | Result |
| --- | --- | --- |
| `qwen2.5-coder:1.5b` | 3 | 3 pass / 1 fail (75%); `slugify` failed in all three trials |
| `qwen2.5:3b` | 3 | 4 pass / 0 fail (100%), pass@3 4/4 |
| `qwen2.5-coder:7b` | 3 | 4 pass / 0 fail (100%), pass@3 4/4 |
| `qwen2.5-coder:14b` | 1 | 2 pass / 0 fail / 2 infra: the two cases that ran passed. The first two never ran: the trace records `ModelCallError('llama-server process has terminated: signal: killed')` on each attempt, the kernel killing the 14B's server while the 1.5B, 3B and 7B were still resident from the ensemble pass on a 15 GB host; the harness recorded one error and one abandonment. |

The harness classifies those two 14B outcomes as infrastructure, not model
failures, which is the honest reading: the suite says nothing about the
14B's code on those cases. The model server was then restarted with
`OLLAMA_MAX_LOADED_MODELS=1` for the fanout pass in the multi-model table.

## What the runtime learned (changes in this branch)

| Observation | Change |
| --- | --- |
| The ladder existed with no live caller | `application/routing/tier_escalation.py` and the loops in `_sonder_impl_serialized`, `_answer_with_history_impl` and `_workbench_agent_escalating` (commit `771946c7`) |
| A file summary never reached the file | summarize / summarise / describe / outline of a file-like target is workspace work (`443a3633`) |
| A 1.5B "completed" a fix after changing nothing | a completion claim with no change and no validation on a change request is a failure and steps up (`d16e6ae3`) |
| The served receipt named the route, not the model that answered a pre-routed turn | every attempt reports its target (`1c8ec263`) |
| A missing text file was reported as a missing image | the line-range read says "file not found" (`1c8ec263`) |
| Two boundary findings (served `project` namespacing; `workspace_root()` falling back to the package directory) | recorded for the owner above, not changed |

## Limitations

- **Escalation sees failure, not wrongness.** Every wrong ledger answer in
  these trials stood, on every model, because nothing objective failed. A
  verifier for chat answers does not exist; the gateway policy's
  `verifier_failure` trigger has a consumer only where the agent's own
  standing (no change, no validation) can be read. Anything stronger needs
  a judge, and measured ensembles did not help on this prompt.
- **Time.** On a 4-core CPU host the 14B costs about ten minutes per agent
  step on a fresh prompt (prompt processing dominates: ~12k tokens at ~20
  tokens/s), so a full ladder run on one request took up to 17 minutes.
  The same ladder on a GPU host would be minutes end to end; nothing in the
  runtime changes, only the wall clock.
- **Memory.** Ollama's CPU loads disable mmap, so resident models are real
  RAM; with four models bound and 15 GB the kernel killed the 14B once.
  `OLLAMA_MAX_LOADED_MODELS=1` is the safe setting for a ladder on a host
  this size.
- **One host, one prompt family.** The ledger tasks are small and the
  convention in T4/T6 is compact; the trials measure the machinery (which
  rung answered, when a step happened, what a receipt said), not the
  models' general accuracy. The eval harness rows are the closest thing to
  a capability measure here.

## Decisions taken later the same day

The owner delegated the recorded decisions; each was taken the narrow way.

- **Served `project` reaches routed work only inside the configured roots.**
  `served_work_project` passes a client value through when it names an
  existing directory inside `file_ops.allowed_roots()`, so the agent gains
  a base for relative paths and nothing else; bare names and outside
  directories keep the namespaced id (`_work_project_for_request`).
- **`workspace_root()` is the checkout again.** The strangler migration had
  moved `file_ops.py` four levels down and the "directory of this file"
  expression with it; the base for unscoped relative paths is once more the
  directory that contains the `sonder_runtime` package.
- **The code gate is a verifier the ladder can see.** On both chat paths a
  runnable code block that still fails the execution-grounded gate after
  its repair round-trip steps to the next rung, with the failed attempt's
  interaction discarded. Prose answers still have no verifier; that
  limitation stands.
- **Remote branch deletion** was retried and is still refused by this
  session's permission classifier; the command list in
  `RETIRED-BRANCHES-2026-09-02.md` remains the owner's to run.

## Reproduction

The lab lives outside the repository (a scratch directory): a seeded
workspace with the ledger defect, `drive.py` (binds `/workspace`, sends one
task per fresh REPL or one served request per task, verifies with an
independent `pytest` and `git diff`), `multimodel.py`, and the two trial
scripts. The runtime knobs that mattered: `SONDER_TIMEOUT=1800`, the
`SONDER_FAST/CODE/GENERAL/REASONING` bindings, `sonder:latest` copied onto
the ladder's first model with `ollama cp`, `OLLAMA_LOAD_TIMEOUT=30m` and
`OLLAMA_MAX_LOADED_MODELS=1` on the model server, and `permission_mode.json`
set to `auto` in a fresh `SONDER_HOME` per configuration.





