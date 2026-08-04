# Disk exhaustion

Preflight enforces `[state].minimum_free_disk_bytes` (default 5 GiB) and
`sonder_disk_free_bytes` is exported per path class. Act before zero.

## Immediate triage (service degraded or failing writes)

```bash
df -h /var/lib/sonder /var/backups/sonder
du -sh /var/lib/sonder/* | sort -h | tail
du -sh /var/backups/sonder/* | sort -h | tail
```

Reclaim in this order — safest first:

1. Old release directories (keep current + previous):
   `ls -dt /opt/sonder/releases/* | tail -n +3 | xargs rm -rf`
2. Backup retention: `python -m sonder_runtime backup prune --config /etc/sonder/sonder.toml`
   (never removes the newest verified backup).
3. Operation-event retention (default 90 days) — lower
   `[observability].audit_retention_days` and restart if the events table
   dominates.
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
