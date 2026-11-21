# Ollama cloud model refresh

Ollama retires hosted (`:cloud`) models. A retired model still has a name in
Sonder's model lists, and a tier bound to it fails on the first call. The cloud
model refresh keeps those lists current. It runs on demand from the CLI and
once a day inside long-lived `serve` and `mcp` processes, and it does four
things:

1. **Retire (automatic).** It probes every cloud model Sonder knows with Ollama
   `/api/show`. The known models are the shipped and overlay defaults,
   `SONDER_CLOUD_CODE` / `SONDER_CLOUD_GENERAL` (and `SONDER_CODE` when it
   names a cloud model), the legacy general model, the ladder entries, pulled
   cloud stubs and catalog entries. An error answer (at any HTTP status; the
   live API uses 410) of the form
   `<name> was retired on <Month D, YYYY>. Try <suggestion>. (ref: <id>)`,
   where the suggestion and the `(ref: ...)` request reference are optional,
   adds the name to the overlay `retired_cloud_models`. The provider's
   suggestion is recorded; the reference is not.
2. **Catalog (automatic, inert).** It reads the cloud models listed on
   `https://ollama.com/search?c=cloud` and their tags pages, and records them
   in a separate state file with first-seen, last-seen and the `/api/show`
   metadata (parameters, context length, capabilities). Nothing is bound to a
   tier because of the catalog.
3. **Rebind (operator ladder only).** When a cloud tier's effective model is
   retired, the refresh writes the first eligible entry of the operator's
   ladder for that tier to the overlay `default_cloud_models.<tier>`. With no
   ladder, or no eligible entry, it writes nothing and reports
   "needs operator choice" with the provider's suggestion. The tier keeps
   falling back as before.
4. **Pull (opt-in, default off).** With `"auto_pull_stubs": true` it pulls the
   missing `:cloud` stubs of live ladder models, at most four per run. It
   refuses any name that is not a cloud stub.

The refresh never sends a prompt, never calls a cloud tier, never enables
cloud opt-in (`SONDER_ALLOW_CLOUD`), and never writes runtime policy,
permission state or an environment binding. It does not need cloud opt-in
because it sends no prompt: `/api/tags`, `/api/show` and `/api/pull` are
metadata calls to your Ollama daemon, under the configured remote-Ollama
policy.

## Files

All files are in the Sonder state home (`%LOCALAPPDATA%\sonder` on Windows)
unless an override is set. `SONDER_CLOUD_MODEL_PREFERENCES` and
`SONDER_CLOUD_MODEL_STATE` apply only when no state home is configured: a
configured home (`state.home` in the CLI config, or a typed home) wins, and
both files are then read from and written to that home.

| File | Written by | Purpose |
|---|---|---|
| `model_lists.json` (`SONDER_MODEL_LISTS`) | refresh, `/runtime models set` | Operator overlay. The refresh writes only `retired_cloud_models`, `default_cloud_models.<tier>` and `version`. |
| `model_lists.json.bak` | refresh | The previous overlay (one rolling copy). |
| `cloud_model_preferences.json` (`SONDER_CLOUD_MODEL_PREFERENCES`) | the operator only | Ladders and switches. |
| `cloud_model_catalog.json` (`SONDER_CLOUD_MODEL_STATE`) | refresh | State: models seen, probe results, catalog, pending choices, the pins this tool wrote, `last_success_at`. |
| `cloud_model_catalog.json.bak` | refresh | The previous state (one rolling copy). |
| `cloud_model_changes.jsonl` | refresh | Append-only change log: one line per change and pull, plus one `run` line per apply run. It rotates once to `.1` past 1 MiB. |
| `cloud_model_refresh.lock`, `model_lists.json.lock` | refresh | The run lock, and the overlay lock shared with `/runtime models set`. |

The written retired list is always the union of the shipped list, the
existing overlay list and the new entries. A list in the overlay replaces the
whole shipped key, so a shorter list would hide shipped retirements. When a
refresh writes a new retirement, it also restores shipped entries that the
overlay list hides. A run with no new retirement leaves the overlay list
alone, so a shipped entry the operator deliberately left out stays out.

Every write goes to a temporary file in the same directory, is checked by
loading it back through the model-list loader (the same code path that would
otherwise ignore an invalid overlay as a whole), keeps the previous file as
`.bak`, and only then replaces the file. If the overlay is invalid before the
run, the refresh refuses and writes nothing; it never overwrites a bad
overlay.

## Preferences

```json
{"version": 1,
 "code":    ["kimi-k3-code:cloud", "glm-5.3:cloud"],
 "general": ["glm-5.3:cloud", "kimi-k3:cloud"],
 "auto_pull_stubs": false,
 "auto_refresh": true,
 "catalog": true}
```

- `code` and `general` are ordered ladders of at most 16 names. Each name must
  be a cloud stub: the tag after the last `:` is `cloud` or ends in `-cloud`.
  Duplicates are dropped case-insensitively.
- A ladder entry is eligible when it is not retired, `/api/show` reports it
  live in this run, and it is pulled (or was pulled in this run). The provider
  suggestion is never chosen unless it is itself the first eligible entry.
- `auto_pull_stubs` (default false) pulls missing live ladder stubs.
- `auto_refresh` (default true) set to `false` turns off the daily run.
- `catalog` (default true) set to `false` skips the ollama.com catalog.
- An unknown key, a non-boolean flag or a bad name makes the whole file
  invalid. An invalid file is ignored and reported: retirement and the catalog
  still run, but there is no rebind and no pull.

## CLI

```text
python -m sonder_runtime models cloud-refresh [--apply] [--json] [--no-catalog] [--config PATH] [--set SECTION.KEY=VALUE]
```

The default is a dry run. It writes no overlay, state, backup or change-log
file; it still takes the run lock (`cloud_model_refresh.lock` and the lock's
owner sidecar) and may create the state home directory. The report lists:

- retirements, with the provider's date and suggestion;
- rebinds, as `default_cloud_models.<tier>: A -> B (ladder #n)`;
- `needs operator choice` lines, with the provider suggestion and each ladder
  entry's state (`retired`, `not_live`, `not_pulled`, `error`, `unprobed`,
  `invalid`, `live`, `selected`);
- pulls;
- the catalog status and counts, marked "inert, nothing bound";
- notes about environment bindings.

`--apply` writes the files and lists them with their backups. Exit status: 0
ok, 1 aborted / refused / busy, 2 usage.

## MCP

`cloud_models_status` is read-only. It reads the state files, the model lists
and the environment, makes no network calls and writes nothing. Host paths in
its answer (file paths, model-list issues, errors) go through the same display
redactor as the other model-visible tools. It reports:

- the last refresh, `last_success_at` and `next_due_after`;
- the effective retired list, with each entry's source (shipped, overlay or
  refresh-evidence);
- new catalog models;
- each ladder entry's last status and whether it is pulled, retired and
  eligible;
- pending operator choices;
- the overlay pins this tool wrote, flagged when a pin now hides a newer
  shipped default;
- each cloud tier's effective model and whether it comes from the environment
  or the defaults;
- the preference switches, and any preference or model-list errors.

Applying a refresh is the CLI's or the schedule's job.

## Schedule

Sonder has no general periodic scheduler. `serve` (after its listener binds)
and `mcp` (after inference membership starts) each start one owned daemon
thread, `sonder-cloud-model-refresh`. The thread:

1. waits 120 s, so startup and the first request are not affected;
2. checks once an hour, and runs `--apply` when the state file's
   `last_success_at` is missing or at least 24 h old;
3. after a failed attempt, waits 6 h before trying again in that process.

The run lock and the persisted timestamp mean that `serve` and `mcp` on one
state home refresh once a day between them. The thread only logs: on MCP stdio,
stdout is the protocol channel. It is off when `SONDER_CLOUD_MODEL_REFRESH=0`,
when the preferences say `"auto_refresh": false`, and in the test suite.

## Live effect

`model_lists.load()` re-reads a changed overlay on the next call, and the live
cloud tier bindings are re-synced before every tier read. So an overlay change
from the refresh (or from `/runtime models set`) reaches a running `serve` or
`mcp` process without a restart:

- a tier bound to a retired model moves to the current default;
- a tier that follows the defaults moves to a changed `default_cloud_models`
  value;
- a tier bound by a live `SONDER_CLOUD_<TIER>` is left alone;
- the 402 extra-usage fallback uses the current code default.

An overlay that fails to load (the loader then serves the shipped values)
leaves the live cloud tiers as they are, and the 402 fallback uses the
process's startup code default: shipped values standing in for a broken
overlay never undo an applied refresh.

Local tiers and other startup bindings still need a restart. The
`DEFAULT_CLOUD_*` and `RETIRED_CLOUD_MODELS` constants in `server.py` remain
import-time compatibility values.

## Catalog limits

ollama.com has no official catalog API, so the catalog is best-effort. It
fetches only two URL shapes, `https://ollama.com/search?c=cloud` and
`https://ollama.com/library/<family>/tags`, through the bounded public-web
transport. That transport sends the generic `sonder-local-agent/1.0`
User-Agent, no cookies, no body and no user data, and caps the response size.

- A family's tags page is re-read at most once a week.
- A run spends at most 90 s on the catalog; past that the catalog status is
  `partial`.
- A page that parses to nothing is `unparsed`: the layout changed, and the
  catalog section is left untouched.
- An HTTP or transport failure makes the status `unavailable` and discards the
  catalog for that run only. Retirement and rebinds from the Ollama probes
  still apply, so on a catalog failure the overlay and state can still be
  written; only an Ollama failure leaves every file unchanged.
- Only the first URL is checked against the two shapes. The transport follows
  up to five redirects, each re-validated as a public address, to any host.

The catalog is off when `SONDER_WEB_TOOLS=0`, when `SONDER_CLOUD_CATALOG=0`,
when the preferences say `"catalog": false`, or with `--no-catalog`.

## Failures

| Failure | Effect |
|---|---|
| Ollama unreachable (tags, show or pull) | `aborted`, no file written |
| Catalog unreachable | catalog discarded for the run (`unavailable`) |
| Invalid overlay | `refused`, no file written |
| Invalid preferences | reported; retirement and catalog only |
| Invalid state file | treated as empty; the bad file becomes the `.bak` on the next write |
| Validation of a new file fails | `refused`, nothing replaced, temporary file removed |
| Another refresh running | `busy`, nothing read or written |

Reports and the change log carry reason codes and parsed model names only,
never response bodies.
