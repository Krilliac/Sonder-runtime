# Set up Sonder: start here

This is the ordered path through every setup runbook. It holds no commands of
its own: each stage names the one page that owns those steps. Do the stages in
order and skip the optional ones you do not need. It applies to both profiles,
**workstation-local** and **server-private** (see
[Getting Started](../wiki/02-getting-started.md) for what each means).

Preconditions: a machine that can run Python and a local model server (see
[Model Requirements & Onboarding](../wiki/19-model-requirements-and-onboarding.md)
for the exact minimum), and shell access with permission to create a state
home. Volatile facts on the linked pages are date-stamped there.

## 1. Install

Pick one. On a fresh state home the first command must be `serve`
(`preflight` is safe before it). `serve` stamps a new home at epoch 2
(`__main__.py:1245-1246`, `stamp_fresh_home` in `bridge_migration.py:98-110`).
Running `migrate`, `repl`, `mcp` or `smoke` first creates stores without that
stamp, and the next `serve` then refuses the home
(`migration required before serve`, `__main__.py:1248-1252`).

- From source on Windows, Linux or macOS:
  [install-workstation-local.md](install-workstation-local.md). See
  [Fresh state home and the epoch-2 gate](install-workstation-local.md#fresh-state-home-and-the-epoch-2-gate)
  for the first-start rule. Variants on the same page: the
  [extracted local-system bundle](install-workstation-local.md#extracted-local-system-bundle),
  [WSL2 (Linux inside Windows)](install-workstation-local.md#wsl2-linux-inside-windows)
  and [optional dependency sets](install-workstation-local.md#optional-dependency-sets).
- As a Linux systemd service: [install-server-private.md](install-server-private.md).
- Prebuilt desktop/mobile app: [app/README.md](../../app/README.md).

## 2. Model backends

Sonder needs at least one generative model. Start with
[Model Requirements & Onboarding](../wiki/19-model-requirements-and-onboarding.md)
(Ollama, the `sonder:latest` alias, embeddings). Other backends, each on its own page:

- OpenAI-compatible servers (llama.cpp server, vLLM, LM Studio):
  [openai-compatible.md](../integration/openai-compatible.md)
- Sonder Inference gateway: [sonder-inference.md](sonder-inference.md)
- OpenRouter: [openrouter.md](../integration/openrouter.md)
- Anthropic: [anthropic.md](../integration/anthropic.md)
- Building a model set from files you already have:
  [assemble-model-collection.md](assemble-model-collection.md)
- The facts model: [use-facts-model.md](use-facts-model.md)

Concept page: [Model Tiers & Gateway](../wiki/08-model-tiers-and-gateway.md).

## 3. Optional: remote Ollama workers

Do this if inference should run on other machines.

1. Prepare the worker OS: [multi-node-ollama.md](multi-node-ollama.md).
2. Put a Caddy TLS proxy in front of the worker and configure the coordinator:
   [remote-ollama-caddy.md](remote-ollama-caddy.md).
3. Pool behaviour and the embedding host: [multi-pc-ollama.md](multi-pc-ollama.md).
4. Two Windows machines on a dedicated cable:
   [multi-node-windows-private-link.md](multi-node-windows-private-link.md).

## 4. Optional: Sonder Inference

If you want the C++ gateway, build and start it from its own repository
([Sonder-Inference README](https://github.com/Krilliac/Sonder-Inference/blob/main/README.md)
and [server guide](https://github.com/Krilliac/Sonder-Inference/blob/main/docs/SERVER.md)),
then connect the runtime with [sonder-inference.md](sonder-inference.md).

## 5. Optional: compute node

For a separate compute host: [compute-fabric.md](compute-fabric.md) and
[deployment-topology.md](deployment-topology.md).

## 6. Clients

Connect chat UIs, the REPL, MCP clients and the app:
[connect-clients.md](connect-clients.md).

## 7. Security hardening

Before exposing anything beyond loopback, in this order:

1. [configure-authentication.md](configure-authentication.md)
2. [security-hardening.md](security-hardening.md)
3. [secure-remote-access.md](secure-remote-access.md): nginx or Caddy in front of
   the API ([Caddy setup](secure-remote-access.md#setup-with-caddy)).
4. [rotate-credentials.md](rotate-credentials.md)

## 8. Optional: training (NVIDIA CUDA only)

[TRAINING.md: set up a training host](../../TRAINING.md#set-up-a-training-host).

## 9. Operations

- Start, stop, drain: [start-stop-drain.md](start-stop-drain.md)
- Backups: [backup-restore.md](backup-restore.md)
- Upgrades and rollback: [upgrade-rollback.md](upgrade-rollback.md)
- Ollama down: [ollama-outage.md](ollama-outage.md)
- Commands and flags: [CLI & Entrypoint](../wiki/04-cli-and-entrypoint.md)

## Where to get help

Run `preflight` (startup checks, opens no listener) and `doctor` (consolidated
health report) as described in [CLI & Entrypoint](../wiki/04-cli-and-entrypoint.md).
Host-header rejections (`421`) are explained in
[HTTP API & Lifecycle](../wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).
