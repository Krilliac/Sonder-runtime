# Sonder Runtime

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/sonder-runtime-banner-dark.svg">
    <img src="docs/assets/brand/sonder-runtime-banner-light.svg" alt="Sonder Runtime" width="720">
  </picture>
</p>

<!-- ci-artifact-badges:start -->
[![Prerelease downloads](https://img.shields.io/badge/app--latest-prerelease-2088FF?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/tag/app-latest)
[![Android prerelease](https://img.shields.io/badge/Android-prerelease-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-android.apk)
[![Linux prerelease](https://img.shields.io/badge/Linux-prerelease-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-linux-x64.tar.gz)
[![Windows prerelease](https://img.shields.io/badge/Windows-prerelease-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-windows-x64.zip)
[![macOS prerelease](https://img.shields.io/badge/macOS-prerelease-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-macos.zip)
<!-- ci-artifact-badges:end -->

Sonder Runtime is a local-first AI runtime written in Python. It wraps an
[Ollama](https://ollama.com) model server with a policy layer, SQLite-backed
memory, guarded host tools, agent orchestration, and optional adapter
training, and exposes them through a terminal REPL, an OpenAI-compatible HTTP
API, an MCP server, and a Flutter client app. Sonder is a runtime, not a
foundation model: it ships no model weights, and names such as `sonder:latest`
are local Ollama aliases. Ollama is the local model server.

Website: <https://sondercore.si>

## Key capabilities

- **Interfaces.** `python -m sonder_runtime` provides `repl`, `serve`
  (OpenAI-compatible API, default `127.0.0.1:11435`), and `mcp`, plus
  operational commands (`preflight`, `doctor`, `status`, `config`, `migrate`,
  `backup`, `restore`, `drain`, `smoke`, `update`). See the
  [CLI reference](docs/wiki/04-cli-and-entrypoint.md).
- **Model tiers.** Local `fast`, `code`, and `general` aliases routed to
  loopback Ollama; hosted/cloud tiers are available only after explicit
  opt-in. See [model tiers and gateway](docs/wiki/08-model-tiers-and-gateway.md).
- **Memory and learning.** Hybrid retrieval over SQLite FTS5 and local
  embeddings; lessons are distilled from outcomes a caller records with
  `record_outcome`. See [memory and learning](docs/wiki/06-memory-and-learning.md).
- **Host tools.** File, repository, code-execution, data, and web tools, each
  subject to the permission policy described under
  [Security model](#security-model). See
  [tools and languages](docs/wiki/10-tools-and-languages.md).
- **Agents.** Single-agent tool loops, durable autopilot, and bounded parallel
  fleets with cancellation. See
  [agent, autopilot, and fleets](docs/wiki/07-agent-autopilot-fleet.md).
- **Operations.** Schema migrations, backup and restore, signed updates, and a
  redacted diagnostics bundle. See
  [backups and recovery](docs/wiki/12-backups-and-recovery.md) and the
  [update manager](docs/wiki/13-update-manager.md).
- **Training (optional).** Adapter training with validation and rollback. See
  [TRAINING.md](TRAINING.md).

## Requirements

- Python 3.11 or later (CI uses 3.12).
- Ollama, installed and running.
- One generative local model. `setup_alias.py` pulls a base model, exposes it
  as `sonder:latest`, and also tries to pull the `nomic-embed-text` embedding
  model. Without an embedding model, chat works but semantic recall is
  unavailable. Vision, reasoning, and other specialist models are optional.
  See [model requirements and onboarding](docs/wiki/19-model-requirements-and-onboarding.md)
  and the [model catalog](docs/wiki/18-model-catalog.md).

Runtime Python dependencies are pinned in
[`requirements-runtime.txt`](requirements-runtime.txt).

## Install

### From source

```bash
# Linux/macOS
git clone https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
python3 -m venv venv
./venv/bin/pip install -r requirements-runtime.txt
./venv/bin/python setup_alias.py
```

```powershell
# Windows PowerShell
git clone https://github.com/Krilliac/Sonder-runtime.git
Set-Location Sonder-runtime
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements-runtime.txt
.\venv\Scripts\python.exe setup_alias.py
```

`setup_alias.py --no-embedding` skips the embedding model. On Windows,
`packaging\install_workstation_local.ps1` performs the venv, install, and
preflight steps in one command; see
[install-workstation-local](docs/runbooks/install-workstation-local.md). For a
self-hosted Linux server behind TLS, follow
[install-server-private](docs/runbooks/install-server-private.md).

### Prerelease app builds

The `app-latest` badges above link to a mutable prerelease snapshot of the
`build-apps` workflow outputs. It may lag `main` and is not a versioned
release. Versioned `app-vX.Y.Z` releases must pass the repository's version,
artifact-integrity, SBOM, and provenance gates; see
[release-version-policy](docs/runbooks/release-version-policy.md). Desktop
bundles include the runtime in a `local-system` directory; run
`bootstrap-engine.cmd` (Windows) or `./bootstrap-engine.sh` (Linux/macOS) from
it once to set up the local model.

## Quick start

From a source checkout, using the venv interpreter from the install step:

```bash
python -m sonder_runtime doctor      # read-only health report
python -m sonder_runtime repl        # interactive terminal REPL
```

On Windows, `sonder.cmd` in the checkout or bundle root launches the REPL. In
the REPL, `/help` lists commands, `/model` lists and switches models, and
`/runtime` shows the tier-to-model policy.

To serve the OpenAI-compatible API on loopback:

```bash
python -m sonder_runtime serve
curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"sonder","messages":[{"role":"user","content":"Hello"}]}'
```

To run as an MCP server, use `python -m sonder_runtime mcp`. Client
configuration examples are in [integrations/](integrations/README.md).

More detail: [getting started](docs/wiki/02-getting-started.md),
[HTTP API](docs/wiki/05-http-api-and-lifecycle.md), and
[thin client and private hosting](CLIENT.md).

## Configuration

Configuration is read from `sonder.toml`, and secrets from `sonder.env`, in
the per-user Sonder home, or from the paths given by `SONDER_CONFIG` and
`SONDER_SECRETS`. `python -m sonder_runtime config` prints the effective,
redacted configuration. Annotated examples are in
[`packaging/sonder.toml.example`](packaging/sonder.toml.example) and
[`packaging/sonder.env.example`](packaging/sonder.env.example); the full
reference is [configuration](docs/wiki/03-configuration.md).

## Security model

Sonder can execute code and modify files on the host, so access to a Sonder
endpoint should be treated as shell access. Read [SECURITY.md](SECURITY.md)
before enabling remote access. The runtime enforces the following:

- **Loopback by default.** The server binds `127.0.0.1`. A non-loopback bind is
  refused unless `tls_terminated_by_proxy=true` is set and `SONDER_API_KEY`
  meets the minimum length. Remote access is supported only through the
  server-private profile behind a TLS reverse proxy
  ([secure remote access](docs/runbooks/secure-remote-access.md)).
- **Cloud off by default.** Hosted/cloud tiers are refused until
  `SONDER_ALLOW_CLOUD=1` is set. A non-loopback Ollama endpoint is refused
  unless `SONDER_ALLOW_REMOTE_OLLAMA=1` is set. The two opt-ins are separate.
- **Approvals.** Permission modes `plan`, `manual` (default), `acceptEdits`,
  and `auto` control when tool calls need approval. Tools classed `dangerous`
  (for example `file_delete`, `sqlite_mutate`, `git_merge`) ask in every mode,
  and unattended callers (MCP, HTTP, agent loops) are refused instead of
  prompted. Raising the mode requires an attended caller.
- **File roots.** File tools are constrained to configured roots, and
  `file_delete` is a dry run unless confirmed.

Optional container execution is described in
[ISOLATED_EXECUTION.md](docs/security/ISOLATED_EXECUTION.md). Report
vulnerabilities privately through the repository's GitHub Security tab.

## Limitations

- A small local model is not a frontier model. Give it the facts, delegate
  bounded transformations, and review its work.
- Learning is grounded only when a caller records a real outcome; self-graded
  success is not treated as proof.
- Multi-PC inference (`SONDER_OLLAMA_WORKERS`) is request-level pooling, not
  model-weight sharding or shared-memory GPU federation. Each host runs its
  own Ollama and model store.
- The optional [NPU path](NPU.md) is a utility accelerator for routing and
  embeddings; token generation remains on the model server's CPU/GPU path.
- The [unsafe lab](docs/runbooks/unsafe-lab.md) removes model-loop host-tool
  policy; it does not provide OS isolation.

## Behavior status

Labels follow the
[documentation status vocabulary](docs/architecture/DOCUMENT-AUTHORITY-INDEX.md#documentation-status-vocabulary).
Unfinished implementation work is tracked only in the
[master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md).

| Behavior | Status | Boundary |
|---|---|---|
| Terminal REPL, OpenAI-compatible API, MCP server, and thin client | Implemented | Local by default; remote access only through the documented TLS profile. |
| `app-latest` desktop and mobile app builds | Experimental | Mutable prerelease snapshot that may lag `main`; not a versioned, release-ready build. |
| Missing optional models (embedding, reasoning, or vision) | Degraded | Chat keeps working; semantic recall or the unbound tier stays unavailable until configured. |
| Multi-PC Ollama request pooling (`SONDER_OLLAMA_WORKERS`) | Implemented | Opt-in request-level scheduling with bounded transport failover. |
| Model-weight sharding or shared-memory GPU federation across PCs | Unsupported | Each host runs its own Ollama model store. |
| Speech or reranker tags without a provider-backed integration | Unsupported | Installing a tag in Ollama does not by itself enable a Sonder feature. |
| Unsafe lab mode ([runbook](docs/runbooks/unsafe-lab.md)) | Experimental | Exact acknowledgement and disposable isolated hosts only; it provides no OS isolation. |
| Tested helper-process update activation on Windows and macOS | Proposed | UPDATE-002 tracks completing activation and self-replacement beyond Linux. |

## Project layout

| Path | Contents |
|---|---|
| `sonder_runtime/` | Layered package (`domain`, `application`, `adapters`, `interfaces`, `platform`, `bootstrap`); entry point `python -m sonder_runtime`. |
| `server.py`, root `*.py` | Tool host and runtime modules used by the package. |
| `app/` | Flutter client for Android, Linux, Windows, and macOS ([app/README.md](app/README.md)). |
| `packaging/` | Installers, systemd units, reverse-proxy and configuration examples. |
| `integrations/` | MCP client configuration for Claude Code and Codex. |
| `migrations/` | Schema migrations applied by `migrate`. |
| `scripts/` | CI gate scripts, packaging, and maintenance tools. |
| `docs/` | [Wiki](docs/wiki/README.md), [runbooks](docs/runbooks/README.md), and architecture records; overview in [ARCHITECTURE.md](ARCHITECTURE.md). |
| `tests/` | pytest suite. |

## Development and testing

```bash
pip install -r requirements-dev.txt
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python scripts/check_error_signals.py
python scripts/check_lint_ratchet.py
python scripts/check_history_privacy.py --json
python scripts/check_doc_links.py
python scripts/check_documentation_authority.py
python -m pytest -q -n auto --dist load
```

These are the gates of the `tests` job in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml), which also runs the
offline evaluation lanes (`eval_harness.py`) and the TUF update-trust suites.
Tests marked `network` or `model` are skipped unless `--run-network` or
`--run-model` is passed. See [CONTRIBUTING.md](CONTRIBUTING.md) for the review
workflow.

## Related projects

- [Sonder Inference](https://github.com/Krilliac/Sonder-Inference): an
  optional generation provider the runtime can use in place of Ollama
  ([runbook](docs/runbooks/sonder-inference.md)).
- [Sonder Observatory](https://github.com/Krilliac/Sonder-Observatory): a
  viewer for the runtime's live telemetry export, launchable from the app.
- Website: <https://sondercore.si>

## License

[Apache License 2.0](LICENSE).
