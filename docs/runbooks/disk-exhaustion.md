# Disk exhaustion

Preflight enforces `[state].minimum_free_disk_bytes` (default 5 GiB) and
`sonder_disk_free_bytes` (labelled by path class) is registered but nothing sets it yet, so `/metrics` carries no samples for it; `doctor` reports free space for the state and model roots. Act before zero.

## Immediate triage (service degraded or failing writes)

```bash
df -h /var/lib/sonder /var/backups/sonder
du -sh /var/lib/sonder/* | sort -h | tail
du -sh /var/backups/sonder/* | sort -h | tail
```

Reclaim in this order — safest first:

1. Old release directories (keep current + previous):
   run `python -m sonder_runtime update status` and delete only directories under `/opt/sonder/releases/` that are neither the `active_release` nor the `previous_release` `install_path` (failed releases are kept there too, so modification time does not identify current + previous)
2. Backup retention: `python -m sonder_runtime backup prune --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env`
   (never removes the newest verified backup).
3. Operation-event retention (default 90 days) — lower
   `[observability].audit_retention_days` and restart; `serve` deletes old
   `operation_event` rows only after preflight passes (below
   `[state].minimum_free_disk_bytes` that needs `--skip-preflight`), and the
   plain `DELETE` frees pages for SQLite reuse without shrinking
   `operations.db`. Use this when the `operation_event` table dominates.
4. Ollama model cache: `ollama list` and remove unused models — this is
   model storage, not Sonder state.

## Never delete

- The newest verified backup.
- `*.db` files while the service runs.
- `runtime_policy.json`, `/etc/sonder/sonder.env`.

## Afterwards

`systemctl restart sonder` if writes failed while full — SQLite handles
full-disk safely, but restart clears any wedged WAL state, and preflight
re-checks the threshold.
