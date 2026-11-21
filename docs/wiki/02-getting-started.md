# Getting Started

This page is the conceptual overview: what runs where and which ports it
uses. The ordered, copy-pasteable setup steps live in one place:
**For the ordered setup steps see
[Set up Sonder: start here](../runbooks/setup-start-here.md).**

## Profiles

Two supported profiles (`PROFILES`, `sonder_runtime/platform/config.py:81`):

- **workstation-local**: your machine, intended for loopback. This is the code
  default (`config.py:639`).
- **server-private**: self-hosted Linux, systemd, a TLS reverse proxy for
  remote access. It refuses to start without a `SONDER_API_KEY` of at least
  24 characters (`config.py:1991-1996`).

The listener binds `127.0.0.1` unless configured otherwise
(`config.py:151`). Authentication defaults and the no-key loopback behaviour
are in [configure-authentication](../runbooks/configure-authentication.md).

## Deployment shapes

| Shape | What it is | Setup home |
|---|---|---|
| Workstation-local from source | Python venv on Windows, Linux or macOS, intended for loopback | [install-workstation-local](../runbooks/install-workstation-local.md) |
| Extracted local-system bundle | Self-contained archive, does not change `PATH`; no bare `sonder` wrapper ships for POSIX | [bundle section](../runbooks/install-workstation-local.md#extracted-local-system-bundle) |
| WSL2 | Linux inside Windows, same as native Linux plus NAT and `.wslconfig` caveats | [WSL2 section](../runbooks/install-workstation-local.md#wsl2-linux-inside-windows) |
| Server-private | Dedicated OS user, versioned release, hardened systemd units | [install-server-private](../runbooks/install-server-private.md) |
| Multi-host | A coordinator routing across remote Ollama workers, each behind an HTTPS proxy (never the raw worker port) | [remote-ollama-caddy](../runbooks/remote-ollama-caddy.md), [multi-node-ollama](../runbooks/multi-node-ollama.md) |

Remote access to Sonder itself is a TLS reverse proxy in front of the
loopback listener, never a direct public bind
([secure-remote-access](../runbooks/secure-remote-access.md)).

## Ports (checked 2026-11-20)

| Port | Service | Source |
|---|---|---|
| 11435 | Sonder HTTP server | `platform/config.py:152` |
| 11434 | Ollama | `domain/ollama_policy.py:16` |
| 11436 | Mobile launcher | `sonder_launcher.py:64` |
| 11437 | Sonder Inference gateway | `adapters/inference/sonder_inference_gateway.py:125` |

## Prerequisites in brief

- Python: the installer script accepts 3.11 or newer as a floor
  (`packaging/install_workstation_local.ps1:154`); CI on `main` runs 3.14,
  with a non-required 3.12 compatibility job (`.github/workflows/ci.yml:38,185-195`).
  3.11 is not tested.
- A generative local model; see
  [Model Requirements & Onboarding](19-model-requirements-and-onboarding.md)
  and [Model Tiers & Gateway](08-model-tiers-and-gateway.md).
- State home location and precedence: [Configuration](03-configuration.md#state-home); the default folder is `%LOCALAPPDATA%\sonder` on Windows,
  `~/Library/Application Support/sonder` on macOS and `$XDG_DATA_HOME/sonder`
  (else `~/.local/share/sonder`) on Linux ([full order](../runbooks/install-workstation-local.md#state-home-location)).

## First-run checks

Install and first run: [install-workstation-local](../runbooks/install-workstation-local.md).
Health endpoints (`/live`, `/ready`, `/version`): [HTTP API & Lifecycle](05-http-api-and-lifecycle.md).
Connecting UIs and tools: [connect-clients](../runbooks/connect-clients.md).

## Source checkout updates from the REPL

The REPL banner reports the loaded checkout and the newest cached
`origin/main` revision without contacting the network. Type `/updatecheck` to
refresh that ref and see the installed/newest commits and timestamps.
`/update` is a guarded source fast-forward, not a release-manager install: it
only runs for a clean checkout on `main` using the canonical Sonder remote and
refuses local commits, feature branches, or local edits. Restart the REPL
after it succeeds. See [Update Manager](13-update-manager.md) for signed
release bundles and rollback.

When local work needs to be preserved before that guarded update, `/stash`
shows source-recovery readiness. `/stash save` saves tracked source changes;
`/stash save-untracked` also saves generated/untracked files; `/stash pop`
restores the most recent recovery stash only when the canonical `main`
checkout is clean and the operator typed it at the console, and only a stash
that `/stash save` recorded in that checkout (a stash with the same message
pushed from elsewhere is never popped). These commands use no user-selected
repository, remote, revision, or stash selector. Saved workflows are per-user state by default
(`%LOCALAPPDATA%\sonder\workflows.json` on Windows), not installation files.
On first use, an older checkout-root `workflows.json` is copied into that state
home and left in place for review. Emotion-vector tuning (`/emotion`) is
saved to `emotion_vectors.json` in the same state home; the checkout-root file
is the bundled default, read when no state copy exists and never written
unless `SONDER_EMOTION_VECTORS` explicitly names it.

## Where to go next

- Turn knobs: [Configuration](03-configuration.md).
- Understand what it does per request: [Architecture](01-architecture.md).
- Make it learn: [Memory & Learning](06-memory-and-learning.md).
- Automate work: [Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md).
