# Training failure

Training is isolated from serving: a failed training run must never
change the serving alias or runtime policy. Promotion is gated and
rollback is first-class. Setup and the full command set are in
[TRAINING.md](../../TRAINING.md); this page is the failure procedure.
Checked against the code on 2026-11-20.

## Failed training run

1. Inspect the training state: REPL `/training status`, or
   `python adaptive_training.py status`. Both print `training_state.json` from
   the state home. `SONDER_TRAINING_STATE` overrides it only when the process
   has no state-home override of its own (the cloud script sets it), so the two
   commands can read different files if their home overrides differ; see
   [Outputs and state](../../TRAINING.md#outputs-and-state). The values that matter here are `interrupted`, `failed`
   (with `returncode` or `error`), `failed_validation` (the trainer exited 0 but
   the adapter did not validate; `error` says why) and `trained`. The training
   status is **not** in `/runtime status` (that command reports the model
   policy) and is not a section of `/v1/sonder/status`.
2. A failed run leaves the previous adapter and alias active. Verify: the
   `sonder` alias still answers, and `/runtime status` shows the same model
   assignments as before.
3. Clear the cause before retrying. Common ones:
   - `invalid configuration:` (a `ConfigError`, which the CLI commands print
     without a `ConfigError:` prefix) followed on the next line by the
     bullet `  - [capacity].training_jobs must be >= 1`: Sonder cannot load its config at all. Set `[capacity].training_jobs` to 1 or
     more (the key cannot disable training).
   - `Training: disabled` in the plan: free RAM/VRAM or close applications and
     re-run `plan`; see the hardware notes in
     [TRAINING.md](../../TRAINING.md#1-hardware).
   - `Training not started: only N GB disk free.`: free disk on the run root's
     drive.
   - `ERROR: training dependencies are not installed.`: `start` was run from
     the wrong Python environment; use the training environment
     ([TRAINING.md](../../TRAINING.md#2-training-environment-separate-virtual-environment)).
   - CUDA out of memory: the trainer exits with code 4 and the state becomes
     `failed`. Do not resume: resume requires the same base, sequence length,
     batch size, gradient accumulation and GPU, so it would run out of memory
     again (and a different base or setting is refused). Start a new run with
     `start --confirm` and a smaller `--sequence-length` or `--batch-size`, or a
     smaller `--model`.
   - `Training failed safely: promotion evaluation requires a valid loopback
     Ollama host`: the configured Ollama host is not loopback; see
     [Remote or Caddy-fronted Ollama](../../TRAINING.md#remote-or-caddy-fronted-ollama).
4. Retry explicitly. `start --confirm --resume` continues an `interrupted` or
   `failed` run (same base, sequence length, batch size, gradient accumulation
   and GPU); `start --confirm` begins a new run. Training never auto-retries
   into promotion.

## Failed or bad promotion

1. Promotion is a journaled, recoverable transition: alias update, policy
   update and verification either commit together or are restored. If a deploy
   was interrupted, the next `deploy` or `rollback` first restores the verified
   previous alias and policy.
2. If a promoted model misbehaves, roll back:
   - REPL: `/training rollback`
   - shell: `python adaptive_training.py rollback`

   (`/runtime` has no rollback subcommand.) Rollback verifies `sonder:latest`,
   points the `code` and `general` tiers back at it, and leaves the personal
   model and all training artifacts in place. `Rollback blocked: ...` messages
   name what to fix; do not edit the policy by hand.
3. Confirm: `/runtime status` shows `code` and `general` on `sonder:latest`,
   chat answers on it, and `/training status` shows `rolled_back` with the
   resulting `policy_revision` recorded.

## Stuck training: a lifecycle lock or launch claim is held

Training, deploy, adopt-legacy, release-alias and rollback each take file
locks. They are held only while the owning process is alive and have no
expiry, so a crashed process releases them by itself. A second lifecycle
command fails immediately with `another training lifecycle operation is already
running` rather than waiting.

What exists on disk (names under the state home unless noted):

- `training-lifecycle.lock`, `<runtime_policy file>.lifecycle.lock`,
  `<training state file>.lifecycle.lock`, and the per-user
  `~/.sonder/locks/ollama-<hash>.alias.lock`, each with a `<lock>.owner.json`
  sidecar naming the holding process. `training-lifecycle.lock` also has a
  `.lifecycle-owner.json` sidecar.
- `<run root>/runs/<run id>/.launch-claimed`: written by the trainer child with
  its process id, host and process identity; removed when the controller sees
  the child exit.

Diagnose:

1. Read the `*.owner.json` sidecars to find the holder's process id (`pid`;
   the record also holds `host`, a `process_identity` fingerprint and, for the
   shared lock primitive, `started` and `purpose`, but not the program name). A
   waiter can see a stale owner record, so also confirm the process with that
   id is the same process (its start time matches the record), not a reused
   id. If it is, a lifecycle command is genuinely in progress: wait, or stop
   that process normally.
2. If a command reports `an orphaned training child is still running or its
   identity is unknown` or `Training resume blocked: the prior training child
   is still running`, a trainer recorded in `.launch-claimed` is alive, on
   another host, or its record is unreadable; the controller refuses to start
   or resume over it. Stop the trainer process (or wait for it) and retry. When
   the claim's process is gone, the next resume removes `.launch-claimed` itself.
3. A stale `running` status in `training_state.json` with no live process does
   not block a new `start --confirm`; only a live recorded trainer does.
4. The message "Training blocked: deployment recovery/cleanup must be
   completed with `training rollback` or `training deploy` first": an earlier deploy or
   rollback was interrupted. Run `/training rollback` (or `deploy`) to finish
   or restore it; do not delete the journal or cleanup files.

Do not delete lock files or `.launch-claimed` by hand unless you have confirmed
the owning process is gone and a normal retry did not clear it.
