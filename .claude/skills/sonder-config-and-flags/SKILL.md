---
name: sonder-config-and-flags
description: >-
  Complete catalog of Sonder Runtime configuration: every SONDER_* environment
  variable, config precedence, runtime policy, and consent gate. TRIGGER when
  the user asks "what does SONDER_ mean", "environment variable", "config
  precedence", "runtime policy", "consent gate", "enable cloud", "change the
  port", or "add a config option". DO NOT TRIGGER for starting/stopping the
  server or running doctor/preflight as operations — that is
  sonder-run-and-operate; security rationale for the gates lives in
  sonder-security-and-privacy; venv/test setup is
  sonder-build-and-env.
---

# Sonder Runtime: configuration axes and flags

Every knob the runtime reads, its default, its guard, and where it is defined.
All claims below were checked against real source at commit `99162cf9`
(2026-08-22); citations are `file:line` in this repo.

## The two-layer design (read this first)

Sonder splits configuration into two deliberately separate layers
(`sonder_runtime/platform/config.py:1-20`):

| Layer | File / source | Reload | May touch |
|---|---|---|---|
| **Startup config** | typed loader in `sonder_runtime/platform/config.py` | Restart only; fail-closed (`ConfigError` collects all faults, no listener binds on error) | Network, filesystem roots, credentials, consent gates, capacity |
| **Runtime policy** | `runtime_policy.json` in the state home, hot-reloadable (`sonder_runtime/adapters/runtime_policy.py`) | Live, via `/runtime set` | Local model aliases, lane routing, NPU mode — and nothing else |

The runtime policy "may pick model aliases and routing lanes but can never
widen network, filesystem, credential, or cloud permissions"
(`config.py:17-19`, restated at `adapters/runtime_policy.py:3-4`). If a value
controls safety or exposure, it belongs in the startup layer.

**Precedence, lowest to highest** (`config.py:4-10`):

```text
built-in safe defaults  <  profile TOML  <  secrets env file  <  process environment  <  --set CLI overrides
```

- Profiles: `workstation-local` (default) and `server-private` (`config.py:41`).
  `server-private` requires a `SONDER_API_KEY` of at least 24 characters
  (`MIN_API_KEY_LENGTH`, `config.py:55`; enforced for non-loopback binds at
  `config.py:490-493` and for the profile at `config.py:495-500`).
- **Secrets are forbidden in TOML.** Any key named `api_key`, `auth_secret`,
  `backup_key`, `backup_key_file`, `secret`, or `token` in `sonder.toml` is a
  validation error, not a convenience (`config.py:51-53`, `236-245`).
- Boolean env spellings accepted everywhere the typed loader parses them:
  `1`, `true`, `yes`, `on` (case-insensitive) — anything else is false
  (`sonder_runtime/platform/config_environment.py:39-41`).

### File locations and CLI flags

| Thing | Resolution order | Source |
|---|---|---|
| Config TOML | `--config` flag → `SONDER_CONFIG` env → `<state home>/sonder.toml` if it exists → typed defaults | `sonder_runtime/__main__.py:47-69` |
| Secrets env file | `--secrets` flag → `SONDER_SECRETS` env → `<state home>/sonder.env` if it exists | `__main__.py:49-51` |
| CLI override | `--set SECTION.KEY=VALUE` (repeatable), e.g. `--set server.port=12000` | `__main__.py:36-42`, `config.py:687-723` |

Inspect the effective merged config (secrets redacted to presence-only):

```powershell
python -m sonder_runtime config --json
```

(`__main__.py:220-227`; redaction in `config.py:160-165`, `183-206`. The
`sources` list in the output tells you which precedence layers contributed.)

On POSIX the secrets file must not be group/world readable — mode bits `077`
set is a validation error (`config.py:646-652`).

## Catalog: paths and state

| Variable | Default | Meaning | Source |
|---|---|---|---|
| `SONDER_HOME` | per-OS: Windows `%LOCALAPPDATA%\sonder`; macOS `~/Library/Application Support/sonder` (or legacy `~/.local/share/sonder` if it already exists); Linux `$XDG_DATA_HOME/sonder` or `~/.local/share/sonder` | Canonical state home (databases, policy, audit) | `platform/paths.py:142-170` |
| `SONDER_STATE_HOME` | unset | Historical alias for `SONDER_HOME` | `config.py:369-375`, `paths.py:146-153` |
| `SONDER_FILE_ROOTS` | none | `os.pathsep`-separated list of workspace roots; every entry must be an absolute path or config validation fails | `config.py:376-383`, `520-522` |
| `SONDER_CONFIG` / `SONDER_SECRETS` | `<home>/sonder.toml` / `<home>/sonder.env` | Explicit config/secrets file paths | `__main__.py:48-51` |
| `SONDER_DB` | `<home>/memory.db` | Main memory database override (legacy DB auto-migrates under a cross-process lock) | `paths.py:201-214` |
| `SONDER_MACHINE_HOME` | Windows `%PROGRAMDATA%\Sonder`; POSIX `/opt/sonder` | Machine-wide (not per-user) root | `paths.py:118-129` |

Per-store database overrides all follow the `state_path(filename, ENV_VAR)`
pattern: `SONDER_FLEET_DB`, `SONDER_JOBS_DB`, `SONDER_SESSIONS_DB`,
`SONDER_OPERATIONS_DB`, `SONDER_UPDATES_DB`, `SONDER_AUTOPILOT_DB`,
`SONDER_FANOUT_DB`, `SONDER_EXTENSIONS_DB`, `SONDER_EMBED_CACHE_DB`,
`SONDER_GOAL_DB`, `SONDER_NPU_MANIFEST_DIR`, `SONDER_TRAINING_STATE`,
`SONDER_SELFMOD_HOME` (`sonder_runtime/bootstrap/app.py:364-486`,
`sonder_runtime/adapters/persistence/migrations.py:137-158`, `goal_store.py:79`,
`selfmod.py:121`).

**Alias trap (real, verified):** the typed config prefers `SONDER_HOME` over
`SONDER_STATE_HOME` (`config.py:373`), but the raw path helper used by legacy
code prefers `SONDER_STATE_HOME` over `SONDER_HOME` (`paths.py:149-151`). If
both are set to different directories, canonical `serve` and legacy path
lookups can disagree. Set exactly one.

## Catalog: HTTP server

All defaults from the frozen `ServerConfig` dataclass (`config.py:67-89`);
env mapping in `_apply_environment` (`config.py:301-437`); validation ranges
in `_validate` (`config.py:440-585`).

| Variable | Default | Guard / range | Source |
|---|---|---|---|
| `SONDER_HOST` | `127.0.0.1` | Non-loopback binding is **rejected** unless `tls_terminated_by_proxy=true` AND a >=24-char API key exist (`config.py:480-494`) | `config.py:69` |
| `SONDER_PORT` | `11435` | 1..65535 | `config.py:70`, `451` |
| `SONDER_AUTH_MODE` | `api-key` | one of `api-key`, `account`, `both`, `either`; account-bearing modes refuse the historical dev secret `sonder-local-dev-secret` | `config.py:71`, `453`, `59-64`, `502-514` |
| `SONDER_MAX_REQUEST_BYTES` | `1048576` (1 MiB) | 1..16 MiB | `config.py:72`, `455-456` |
| `SONDER_MAX_CONCURRENT_REQUESTS` | `4` | >= 1 | `config.py:73`, `457` |
| `SONDER_REQUEST_TIMEOUT_SECONDS` | `300` | >= 1 | `config.py:74`, `459` |
| `SONDER_STREAM_IDLE_TIMEOUT_SECONDS` | `60` | >= 1 | `config.py:75`, `461` |
| `SONDER_CORS_ORIGINS` | empty | comma-separated | `config.py:76`, `351-358` |
| `SONDER_REQUIRE_ACCOUNT` | `false` | with default `api-key` mode, flips effective mode to `account` | `config.py:77`, `490-492` in `__main__.py` |
| `SONDER_ALLOW_REGISTRATION` | `false` | boolean | `config.py:78` |
| `SONDER_REASONING_AUDIENCE` | `developer` | `developer` or `all` | `config.py:79`, `472-473` |
| `SONDER_HTTP_SESSION_STATE_LIMIT` | `128` | 2..1024 | `config.py:80`, `463-464` |
| `SONDER_HTTP_SESSION_STATE_OWNER_LIMIT` | `32` | 1..limit-1 | `config.py:81`, `465-469` |
| `SONDER_TRAIN_MAX_N` | `500` | >= 1 | `config.py:84`, `470-471` |
| `SONDER_TLS_TERMINATED_BY_PROXY` | `false` | operator declaration that a TLS proxy fronts non-loopback exposure; loopback never needs it | `config.py:86-89` |
| `SONDER_QUEUE_DEPTH` | `32` | capacity section; >= 1 | `config.py:131`, `559-563`; reader `adapters/web/lifecycle.py:365` |
| `SONDER_METRICS` | `1` (enabled) | export of `[observability].metrics_enabled` | `__main__.py:547`; reader `adapters/web/lifecycle.py:354` |

TOML-only server keys with no env spelling: `trusted_proxy_cidrs` (default
`127.0.0.1/32`, `::1/128`) and `owner_max_inflight` (0 = derived cap)
(`config.py:83-85`).

`_export_runtime_environment` (`__main__.py:462-551`) is the reverse bridge:
after validation it writes the typed values back into `os.environ` so legacy
adapters and child processes see exactly the validated settings — anything not
exported there "is a validated setting the runtime never sees"
(`__main__.py:465-474`).

## Catalog: Ollama connectivity

| Variable | Default | Guard | Source |
|---|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | scheme auto-prefixed with `http://` if missing; non-loopback rejected unless remote consent is on; remote must be `https` | `config.py:102`, `384-387`, `524-536` |
| `SONDER_ALLOW_REMOTE_OLLAMA` | `0` (false) | the remote-Ollama consent gate; without it any non-loopback Ollama URL or worker fails validation | `config.py:103`, `397-400`, `527-531` |
| `SONDER_OLLAMA_WORKERS` | empty | comma- or semicolon-separated pool origins; each entry needs a hostname AND explicit port, no inline credentials; remote entries need the consent gate AND `https` | `config.py:104`, `388-396`, `538-557`; pool built in `adapters/inference/ollama_pool.py:241-248` |

`[ollama].startup_timeout_seconds` (60) and `request_timeout_seconds` (300)
are TOML-only (`config.py:105-106`).

## Catalog: consent gates

All live in `FeaturesConfig` and **default to False** (`config.py:110-120`).
They are startup-config only: the hot-reloadable runtime policy structurally
cannot represent them (its schema is models/routing/npu only,
`domain/runtime_policy/rules.py:243-247`), so nothing a model or API caller
does at runtime can turn one on. This mirrors the frozen-capabilities
precedent in `docs/adr/ADR-003-startup-capabilities.md`: consent-shaped
booleans are parsed at startup and immutable afterwards.

| Variable | Feature field | What turning it on permits | Source |
|---|---|---|---|
| `SONDER_ALLOW_CLOUD` | `features.cloud` | hosted/cloud model tiers (data leaves the machine) | `config.py:401-402` |
| `SONDER_WEB_TOOLS` | `features.web` | web egress tools | `config.py:403-404` |
| `SONDER_LIVE_RELOAD` | `features.live_reload` | live source reload | `config.py:405-408` |
| `SONDER_EXPOSE_REASONING` | `features.expose_reasoning` | reasoning exposure beyond the configured audience | `config.py:409-412` |
| `SONDER_ALLOW_PRIVATE_COT` | `features.allow_private_cot` | private chain-of-thought handling; per test coverage this also requires an explicit allow rule in the permissions system (`tests/test_private_cot_opt_in.py`) | `config.py:413-416` |
| `SONDER_LOCATION_CONSENT` | `features.location_consent` | location use | `config.py:417-420` |

TOML-only feature booleans (no env mapping in `_apply_environment`):
`source_modification`, `host_control`, `training`, `npu`
(`config.py:114-117`).

## Catalog: secrets

Accepted only from the secrets env file or process environment, never TOML
(`config.py:45-50`). Redacted to presence-only in `config`/`doctor`/
`diagnostics` output (`config.py:160-165`).

| Variable | Constraint | Source |
|---|---|---|
| `SONDER_API_KEY` | >= 24 chars required for `server-private` profile or any non-loopback bind | `config.py:55`, `490-500` |
| `SONDER_AUTH_SECRET` | account-bearing auth modes refuse the public dev value `sonder-local-dev-secret` | `config.py:64`, `507-514` |
| `SONDER_BACKUP_KEY_FILE` | path to backup key material | `config.py:48`, `425-428` |
| `SONDER_LAUNCHER_HEALTH_TOKEN` | >= 32 chars (`MIN_TOKEN_LENGTH`) for the launcher HMAC health proof; scrubbed from child environments | `domain/launcher_health.py:11-15`, `platform/logging.py:22` |

## Catalog: model, retry, and retrieval tuning (env-read, not typed)

These are read directly from the environment at their call sites — they never
pass through the typed loader, so `python -m sonder_runtime config` does not
show them and there is no startup validation error for a bad value (each has
its own clamp or fallback).

| Variable | Default | Clamp / behavior | Source |
|---|---|---|---|
| `SONDER_TIMEOUT` | `300` | per-request Ollama HTTP timeout, seconds | `server.py:319` |
| `SONDER_KEEP_ALIVE` | `2m` | how long a model stays in VRAM after last call | `server.py:318` |
| `SONDER_LEARN_TIERS` | every configured local tier | comma list of tiers feeding the learning loop, e.g. `code` or `fast,code,general` | `server.py:818-827` |
| `SONDER_LOCAL_RETRIES` | `1` | clamped 0..2 (`MAX_LOCAL_MODEL_RETRIES`) | `platform/local_retry_policy.py:8-18` |
| `SONDER_LOCAL_RETRY_DELAY_MS` | `150` | clamped 0..1000, exponential backoff capped at 1 s | `platform/local_retry_policy.py:21-29` |
| `SONDER_HOSTED_OVERFLOW_RETRY` | off | hosted/remote overflow retry needs this opt-in AND an idempotent request | `platform/model_retry_policy.py:8-27` |
| `SONDER_DISTILLATION_TIMEOUT` | `20` s | clamped 1..live server ceiling | `domain/distillation_policy.py:18-21` |
| `SONDER_MIN_SIM` | `0.62` (`DEFAULT_MIN_SIM`) | retrieval relevance floor override | `retriever.py:18`, `564` |
| `SONDER_RUNTIME_POLICY` | `<home>/runtime_policy.json` | policy file path override | `adapters/runtime_policy.py:57-61` |
| `SONDER_MAX_WORKER_CAP` | `64` (`ABSOLUTE_MAX_WORKERS`) | operator fleet ceiling; invalid values fail safe to 16 (`STANDARD_MAX_WORKERS`) with a warning | `master_orchestrator.py:60-61`, `426-434` |
| `SONDER_FLEET_HEARTBEAT` | `1` (on) | fleet heartbeat toggle | `master_orchestrator.py:144` |
| `SONDER_EMBED_CACHE` | `1` (on) | `0` disables the embed cache | `adapters/embedding_cache.py:46` |
| `SONDER_FALLBACK_LOCAL` | `1` (on) | client-side local fallback toggle | `platform/client_fallback.py:19` |
| `SONDER_SERVER` | required by client | client target URL for `sonder_client.py` | `sonder_client.py:8`, `adapters/client_config.py:30` |

## Catalog: launcher, terminal, and training bootstrap

| Variable | Default | Meaning | Source |
|---|---|---|---|
| `SONDER_TERMINAL_BOOTSTRAP` | on unless `0` | `sonder.cmd` runs the headless engine bootstrap before the REPL | `sonder.cmd:38` |
| `SONDER_BOOT_LOG` | random `%TEMP%\sonder-bootstrap-*.log` | bootstrap log path (auto-deleted when auto-generated) | `sonder.cmd:33-35`, `80` |
| `SONDER_PYTHON` | auto-detected | interpreter used by launchers | `sonder_headless.py:57` |
| `SONDER_CONTEXT_SIZE` | model-aware auto sizing | pins the requested context window (supports `8k`-style suffixes); `SONDER_SESSION_NUM_CTX` is the related session variable | `platform/context_policy.py:43-66` |
| `SONDER_NUM_THREAD` | CPU thread default | Ollama option passthrough | `platform/environment_options.py:55` |
| `SONDER_NUM_GPU` | unset (bootstrap exports `999`) | GPU layer count | `environment_options.py:59`, `bootstrap_engine.py:413` |
| `SONDER_NUM_BATCH` | `512` | batch size | `environment_options.py:61` |
| `OLLAMA_FLASH_ATTENTION` | `1` (set by launchers/bootstrap) | flash attention for the Ollama server | `bootstrap_engine.py:415`, `sonder.cmd:11` |
| `SONDER_LAUNCHER_CONTROL_GATE` | unset | launcher control-gate env name; scrubbed from model subprocess envs | `sonder_launcher.py:72`, `platform/logging.py:28` |
| `SONDER_RAM_GB` | measured | overrides detected system RAM for model selection | `bootstrap_engine.py:42`, `platform/system_profile.py:443` |
| `SONDER_BASE_MODEL` | hardware-chosen | forces the base model for bootstrap/alias | `bootstrap_engine.py:84`, `setup_alias.py:21` |
| `SONDER_ALLOW_CPU_OFFLOAD` | `0` | permits CPU offload during training | `qlora_train.py:332`, `adaptive_training.py:3238` |
| `SONDER_MAX_VRAM_GB` / `SONDER_MAX_SYSTEM_RAM_GB` | unset | training memory ceilings | `adaptive_training.py:3241-3242` |

## Catalog: unsafe lab and risk policy (handle with care)

| Variable | Behavior | Source |
|---|---|---|
| `SONDER_UNSAFE_LAB_ACK` | Must **exactly** equal the 132-character sentence in `SECURITY.md` ("I UNDERSTAND SONDER UNSAFE LAB MODE GIVES MODELS UNRESTRICTED HOST TOOL ACCESS AND I AM RUNNING IN A DISPOSABLE ISOLATED ENVIRONMENT"). Truthy/abbreviated/whitespace-modified values are refused. Even with the exact string, activation is refused when `SONDER_HOST` is non-loopback, when the process is elevated/root, when `SONDER_ALLOW_CLOUD` is on, or when `OLLAMA_HOST` is non-loopback. The typed loader validates this against the final effective host including `--set` overrides. | `platform/unsafe_lab_policy.py:12-16`, `87-111`; `config.py:669-674`; `SECURITY.md:121-132` |
| `SONDER_UNSAFE_LAB_AUDIT_PATH` | Overrides the durable activation audit trail, default `$SONDER_HOME/audit/unsafe-lab.jsonl` | `adapters/security/unsafe_lab.py:21`, `SECURITY.md:130-132` |
| `SONDER_EXECUTION_RISK_POLICY` | `report` (default) \| `deny-high` \| `deny-medium` \| `deny-unknown`. Per `SECURITY.md:79-85`, the `deny-*` modes currently fail closed for **every** launch because a portable exact inspected-handle-to-interpreter handoff is not yet available — this is deliberate, avoiding a pathname-swap bypass; `report` remains advisory. | `adapters/artifact_risk.py:415-418` |
| `SONDER_PROCESS_INSPECTION` | Must equal `enabled:bounded-read-only` to allow the bounded read-only process/memory scanner; anything else keeps it disabled | `adapters/process_risk.py:17-18`, `SECURITY.md:86-91` |
| `SONDER_UPDATE_ALLOW_UNSIGNED` | `=1` plus the `--allow-unverified` CLI flag are both required to apply an unsigned update; never production | `adapters/updates/service.py:758-776`, `__main__.py:955-957` |

Note ADR-003 records that the acknowledgement-env path is slated for removal
in favor of the startup flags `--unrestricted-tools` / `--unrestricted-selfmod`
(`docs/adr/ADR-003-startup-capabilities.md`); at commit `99162cf9` the env
path is still present and enforced.

## Catalog: test-harness force-set values (do not chase these in prod)

`conftest.py:17-35` force-sets a block of `SONDER_*` values for every pytest
run (throwaway state home, disabled caches/heartbeat, offline port-1
sentinels). If you see them in a debugger during tests, they are harness
isolation, not production defaults. The full table lives in
`sonder-validation-and-qa` §2.

## The runtime policy file

Location: `<state home>/runtime_policy.json`, overridable with
`SONDER_RUNTIME_POLICY` (`adapters/runtime_policy.py:57-61`). Guarded by a
cross-process file lock plus a `<name>.transition.json` marker that blocks
concurrent edits during a model deployment (`runtime_policy.py:42-54`,
`64-67`, `128-189`).

Structure (`domain/runtime_policy/rules.py:15-55`):

| Concept | Values | Rules |
|---|---|---|
| Base tiers | `fast`, `code`, `general` | Always bound to a model; default `sonder:latest` for all three |
| Optional tiers | `reasoning`, `vision` | May be explicitly unset (`none`/`off`/`disabled`/`-`); router degrades to a base tier |
| Embedding | `embedding_model`, default `nomic-embed-text` | Separate vector space; never a chat tier (`rules.py:38-40`) |
| Reserved | `sonder-personal:latest` | Only assignable during an authorized validated deployment (`runtime_policy.py:245-254`) |
| Lanes | `router`, `workbench`, `autopilot`, `fleet`, `review` | Pin only to **base** tiers; defaults: router→fast, everything else→code (`rules.py:42-48`, `191-201`) |
| NPU | mode `off`/`shadow`/`prefer`, per capability `routing`/`embeddings` | Default all off; never names models or providers (`rules.py:49-54`) |
| Cloud names | rejected | Any model containing `-cloud` or ending `:cloud` raises immediately (`rules.py:58-60`, `89-90`) |

Environment seeds (`SONDER_FAST`, `SONDER_CODE`, `SONDER_GENERAL`,
`SONDER_REASONING`, `SONDER_VISION`, `SONDER_EMBED_MODEL`,
`SONDER_CODE_LOCAL`) apply **only when the file is first created**; once the
shared file exists, normalization uses stable built-ins so separately launched
surfaces cannot drift with their inherited env (`rules.py:96-120`, `166-173`).

Operate it from the chat commands (usage text at `server.py:2130-2137`;
server chat dispatches `/runtime` with alias `/models`, `server.py:2713`):

```text
/runtime status
/runtime set fast=<model> code=<model> general=<model>
/runtime set reasoning=<model> vision=<model>
/runtime set embedding=<installed-embedding-model>
/runtime set router=<tier> workbench=<tier> autopilot=<tier> ...
/runtime reset
```

`/model <tag-or-tier>` (per-session model pin, no tool call) is a **REPL**
command, not a server chat command — dispatched at
`sonder_runtime/interfaces/repl/repl.py:1984`, usage at `repl.py:1689`.

## How to add a new configuration axis

Follow the layer decision first, then the checklist:

1. **Pick the layer.** Hot-tunable model/routing knob → runtime policy
   (`domain/runtime_policy/rules.py` + `adapters/runtime_policy.py`).
   Anything touching network, filesystem, credentials, capacity, or consent →
   startup config. Consent-shaped booleans are startup-only by design
   (ADR-003 frozen-capabilities precedent; the policy schema cannot hold them).
2. **Typed field**: add it to the right frozen dataclass in
   `platform/config.py` with a safe default (defaults must be loopback/closed —
   `tests/production/test_config.py:20-28` asserts this posture).
3. **Env mapping**: wire the `SONDER_*` spelling in `_apply_environment`
   (`config.py:301-437`) using `env_bool`/`env_int` so bad values become
   collected `ConfigError` entries, not crashes.
4. **Validation**: add range/consistency checks to `_validate`
   (`config.py:440-585`). Fail closed; collect the error, do not clamp
   silently in the startup layer.
5. **Export**: if legacy adapters or child processes read the env, add the
   reverse mapping in `_export_runtime_environment`
   (`sonder_runtime/__main__.py:462-551`). A validated setting that is not
   exported is invisible to the legacy runtime — this exact bug class is
   documented in that function's docstring.
6. **Redaction**: if it is secret-adjacent, add it to `SECRET_ENV_KEYS`
   (`config.py:45-50`) and the scrub list in `platform/logging.py:22-28`, and
   confirm `config --json` / `doctor` show presence only.
7. **Test**: add coverage in `tests/production/test_config.py` (defaults,
   TOML load, env override, invalid value, precedence).
8. If you write the operator-facing gate rationale, cross-reference
   `sonder-security-and-privacy` rather than duplicating `SECURITY.md`.

## When NOT to use this skill

- Starting/stopping/draining the server, preflight, doctor, backups as
  *operations* → `sonder-run-and-operate`.
- Setting up a venv, running pytest, compile checks → `sonder-build-and-env`.
- Gate/merge policy for changing code → `sonder-change-control`.
- Threat-model reasoning behind the consent gates →
  `sonder-security-and-privacy` (backed by `SECURITY.md`).

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Every table row above was
checked at its cited `file:line`. Re-verify with (repo root, Git Bash):

```bash
grep -n "SONDER_" sonder_runtime/platform/config.py            # env mapping + secret keys
grep -n "SONDER_" sonder_runtime/__main__.py                   # export bridge + CLI paths
grep -n "SONDER_KEEP_ALIVE\|SONDER_TIMEOUT\|SONDER_LEARN_TIERS" server.py
grep -n "SONDER_" sonder_runtime/platform/local_retry_policy.py
grep -n "DEFAULT_" sonder_runtime/domain/runtime_policy/rules.py
grep -n "SONDER_RUNTIME_POLICY\|transition" sonder_runtime/adapters/runtime_policy.py
grep -n "SONDER_STATE_HOME\|SONDER_HOME\|SONDER_DB\|SONDER_MACHINE_HOME" sonder_runtime/platform/paths.py
grep -rn "state_path(\"" sonder_runtime/ | grep "SONDER_"      # per-store DB overrides
sed -n '17,35p' conftest.py                                     # test-harness force-set block
grep -n "SONDER_UNSAFE_LAB\|SONDER_EXECUTION_RISK\|SONDER_PROCESS_INSPECTION" SECURITY.md
python -m sonder_runtime config --json                          # live effective config + sources
```

Volatile facts most likely to drift: the default port (11435), the tier
defaults (`sonder:latest`, `nomic-embed-text`), the `deny-*` execution-risk
fail-closed behavior (explicitly temporary per `SECURITY.md:82-85`), and the
ADR-003 plan to replace `SONDER_UNSAFE_LAB_ACK` with startup flags.
