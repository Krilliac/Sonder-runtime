# Training failure

Training is isolated from serving: a failed training run must never
change the serving alias or runtime policy. Promotion is gated and
rollback is first-class.

## Failed training run

1. Inspect: `/v1/sonder/status` (training/selfmod sections) or REPL
   `/runtime status`.
2. A failed run leaves the previous adapter and alias active — verify:
   the `sonder` alias should still answer, `runtime_policy.json` revision
   unchanged.
3. Clear GPU/disk causes (OOM, disk full) before retrying.
4. Retry explicitly; training never auto-retries into promotion.

## Failed or bad promotion

1. Promotion is atomic from the client's perspective: alias update,
   policy update, and verification either all commit or roll back.
2. If a promoted model misbehaves, roll back:
   - REPL: `/runtime rollback` (or the promotion tooling's rollback
     command). Rollback verifies the identity of the restored adapter.
3. Confirm: chat answers on the restored model, policy revision
   incremented (rollback is a new revision, not a silent revert).

## Stuck training with a maintenance lock held

If a crashed trainer left a `training`/`update` maintenance lock behind,
the lock carries a TTL and expires on its own. To confirm what is held:
`python -m sonder_runtime status --json` and check operations locks. Do
not delete lock rows manually unless the owning process is confirmed dead
and the TTL is unreasonably far out.
