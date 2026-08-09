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
| `doctor` | Consolidated health report for config, state/model storage, self-heal, memory quality, runtime policy, and Ollama reachability. Storage inspection is read-only unless the explicit probe flag is supplied. |
| `status` | Local build / config / schema status. |
| `diagnostics` | Redacted diagnostic bundle (config, schemas, preflight). |
| `config` | Print the effective, redacted configuration. |
| `migrate` | Apply pending schema migrations (all stores or `--store`). |
| `backup` | `create` / `verify` / `list` / `prune`. |
| `restore` | `verify` / `smoke` / `apply` a backup. |
| `smoke` | Minimal end-to-end check (config, migrate, ops roundtrip). |
| `drain` | Request graceful drain of a running server. |
| `rotate-key` | Rotate `SONDER_API_KEY` with an overlap window. |
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
directory, using one randomized temporary file in a killable child process.
The probe is capped at 8 MiB and 5 seconds and removes the temporary file on
success, failure, or timeout. It does not scan files, alter model data, or probe
every mounted volume.

## Exit codes

- `0` success; `1` operational failure (e.g. preflight/migration/backup
  failed); `2` configuration/usage error (fails before any side effect);
  `130` interrupted.

`serve` startup order is **preflight → MIGRATING → migrations → READY →
bind**. A failed required check or a failed migration means no socket ever
opens — the fail-closed contract from [Configuration](03-configuration.md).

## REPL slash commands (selection)

`/stats` (learning/token stats), `/activity` (current response actions),
`/permissions` and `/filepolicy` (guardrails), `/run [sec]` (execute the
last code block, guarded), `/train [N]` (grounded practice),
`/autopilot status|resume|cancel`, `/runtime status`, `/pass` `/fail`
(record outcomes). Plain English also triggers many of these. Dangerous
commands over HTTP require developer/admin authorization.
