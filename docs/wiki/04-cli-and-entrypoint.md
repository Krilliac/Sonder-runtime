# CLI & Entry Point

`python -m sonder_runtime <command>` is the single supported entry point
for production operations. The historical launch scripts remain as
compatibility surfaces and delegate here.

## Commands

| Command | Purpose |
|---|---|
| `serve` | Run the HTTP adapter. Runs preflight first and migrations before binding; refuses to bind on failed preflight. |
| `mcp` | Run the MCP adapter (tool surface for MCP clients). |
| `repl` | Interactive REPL with slash commands. |
| `preflight` | Run startup checks and report; opens no listener. |
| `doctor` | Consolidated health report for config, state/model storage, schema migrations, schema-epoch adoption, backup freshness, self-heal, memory quality, runtime policy, and Ollama reachability. `schema_epoch` is FAIL (exit 1) until `migrate --adopt-epoch2` has run, because `serve` refuses to start before that. Self-heal and memory quality inspect `SONDER_DB` when set, otherwise `<state home>/memory.db` of the selected configuration; a missing or not-yet-initialized database is reported as skipped. Doctor never creates, initializes or migrates a database (the memory database is opened `mode=ro`, which may leave SQLite `-wal`/`-shm` sidecar files); storage inspection writes only when the explicit probe flag is supplied. |
| `status` | Local build / config / schema status. |
| `diagnostics` | Redacted diagnostic bundle (config, schemas, preflight). |
| `config` | Print the effective, redacted configuration. |
| `migrate` | Apply pending schema migrations (all stores or `--store`). `--adopt-epoch2` runs the crash-safe SPEC-5 epoch-2 adoption that `serve` requires; it backs the databases up to `backups/pre-epoch2-*` first, is a verified no-op (no new copy) when the home is already adopted, and rejects `--store` (exit 2) because adoption always covers every domain database. |
| `backup` | `create` / `verify` / `list` / `prune`. |
| `restore` | `verify` / `smoke` / `apply` a backup. |
| `smoke` | Minimal end-to-end check (config, migrate, ops roundtrip). |
| `drain` | Request graceful drain of a running server. |
| `rotate-key` | Rotate `SONDER_API_KEY` with an overlap window. The secrets file is `--secrets`, else `SONDER_SECRETS`, else `sonder.env` in the state home. `--config`/`--set` choose the state home that receives `secrets/rotation.json` and the `API_KEY_ROTATED` audit event; the configuration is validated as `serve` validates it, and an invalid one exits 2 before anything is rotated. |
| `update` | `status` / `build` / `import` / `install` / `rollback` / `cancel` (see [Update Manager](13-update-manager.md)). |

Common flags: `--config <toml>`, `--secrets <env>`, `--set section.key=value`
(highest precedence), `--json`, and `--skip-ollama` where a check would
otherwise probe the model server.

## Typical sessions

```bash
# Bring a fresh install up
python -m sonder_runtime preflight --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env
python -m sonder_runtime migrate  --config /etc/sonder/sonder.toml
python -m sonder_runtime serve    --config /etc/sonder/sonder.toml

# Operate
python -m sonder_runtime status --json
python -m sonder_runtime doctor --json
python -m sonder_runtime doctor --skip-ollama  # fully local checks only
python -m sonder_runtime doctor --storage-probe # explicit bounded state-volume benchmark
python -m sonder_runtime backup create --json
python -m sonder_runtime restore smoke /var/backups/sonder/<dir>
python -m sonder_runtime rotate-key --secrets /etc/sonder/sonder.env --overlap-seconds 86400
python -m sonder_runtime drain     # asks the running server to drain
```

The automatic storage checks report free space for the configured state home
and the configured or platform-native Ollama model root. They use native volume
metadata where safely available and warn on network, removable, or potentially
slow filesystems. Paths derive from configuration, environment, and the current
user profile; no drive letter or machine-specific layout is assumed.

`--storage-probe` is never implied by `doctor`, `status`, preflight, or service
startup. When explicitly selected it probes only the existing configured state
directory. The probe is capped at 8 MiB and 5 seconds and uses one worker-owned
anonymous handle on supporting systems or delete-on-close handle on Windows. It
never exposes or reopens a generated pathname. A scrubbed, isolated worker owns
the handle; a fixed-size result pipe is the only output, and the parent kills
the worker at the five-second wall deadline so process teardown cleans up
blocked setup, I/O, sync, or close. The probe does not scan files, alter model
data, or probe every mounted volume.

## Exit codes

- `0` success; `1` operational failure (e.g. preflight/migration/backup
  failed); `2` configuration/usage error (fails before any side effect);
  `130` interrupted.
- Exception by design: `status` and `diagnostics` are always-available
  reports for collecting evidence from a broken install. On an invalid or
  missing `--config`/`--secrets` they still exit `0` and emit their payload,
  with the problems under `config_errors` and a `WARNING: configuration is
  invalid` line on stderr. Gate scripts on `config`, `doctor` or `preflight`,
  which exit `2` for the same input.

`serve` startup order is **preflight → MIGRATING → migrations → READY →
bind**. A failed required check or a failed migration means no socket ever
opens — the fail-closed contract from [Configuration](03-configuration.md).

## REPL slash commands (selection)

`/stats` (learning/token stats), `/activity` (current response actions),
`/permissions` and `/filepolicy` (guardrails), `/run [sec]` (execute the
last code block, guarded), `/train [N]` (grounded practice),
`/autopilot status|resume|cancel`, `/runtime status`, `/pass` `/fail`
(record outcomes), `/sessions` `/replay [id|title] [N]` `/resume <id|title>`
(list, re-render read-only, and continue past threads). Plain English also
triggers many of these. Dangerous commands over HTTP require
developer/admin authorization.

The REPL has no memory-replication send, retry, promotion, takeover, or
failback command. `/runtime status` is read-only and does not contact a
replication peer. The bounded fact-only service's `replicate_once()` method is
an application-owned operator seam with no public CLI, MCP, REPL, or HTTP
mutation surface. Use the authenticated status/API and System screen only to
inspect its truthful capability posture; see
[Bounded, explicit fact replication](../runbooks/memory-replication.md).

## Scripted REPL output

Piped REPL use (`sonder < script.txt`, `echo /stats | sonder`) prints plain
text with no terminal chrome; that shape is a stable scripting contract.
Setting `SONDER_REPL_NDJSON=1` opts a piped session into one JSON line per
completed chat turn instead (schema `sonder.repl-turn.v1`: `answer`,
`error`, `elapsed_ms`, `interaction_id`, `feedback_offered`, `label`, and
`hint` — the known-failure next step, `""` when none applies).
The flag never changes interactive terminals, and the flagless piped
default never changes. Known failure shapes additionally get a one-line
`hint:` under the interactive error panel only — piped output stays
byte-stable.

## REPL logs

`python -m sonder_runtime repl` writes its log records to
`SONDER_HOME/logs/repl.log` (JSON, owner-only `0600`, rotating 5 x 1 MB) at
`SONDER_REPL_LOG_LEVEL` (default `INFO`), so no JSON line lands on the
terminal. On a terminal, WARNING and above are also queued and shown between
turns as one short notice; piped and `--json` runs print only ERROR records
to stderr, as text. `SONDER_REPL_LOG_STDERR=1` restores the old behaviour:
JSON on stderr at `SONDER_REPL_LOG_LEVEL` (default `WARNING`). `serve` and
`mcp` logging is unchanged.
