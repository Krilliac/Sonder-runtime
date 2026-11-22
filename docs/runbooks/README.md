# Sonder runtime runbooks (SPEC-2 WP10)

Contractor-executable operational procedures. Each runbook assumes the
server-private reference deployment (systemd, loopback bind, reverse
proxy for remote access) unless it says otherwise.

- [setup-start-here.md](setup-start-here.md) — ordered path through every setup runbook
- [configure-authentication.md](configure-authentication.md) — first-time API key, accounts and first admin
- [connect-clients.md](connect-clients.md) — connect MCP agents, console, app, web app, HTTP and the SDK to a Sonder Runtime
- [install-server-private.md](install-server-private.md)
- [install-workstation-local.md](install-workstation-local.md)
- [use-facts-model.md](use-facts-model.md) — import a portable GGUF (facts. USB)
- [assemble-model-collection.md](assemble-model-collection.md) — a routed collection of specialist models
- [secure-remote-access.md](secure-remote-access.md)
- [start-stop-drain.md](start-stop-drain.md)
- [rotate-credentials.md](rotate-credentials.md)
- [backup-restore.md](backup-restore.md)
- [memory-replication.md](memory-replication.md) — explicit fixed-peer, fact-only copies
- [control-state-provider.md](control-state-provider.md) — authenticated replication, quorum, and fencing transport
- [upgrade-rollback.md](upgrade-rollback.md)
- [publish-release.md](publish-release.md) — TUF signing ceremony
- [release-version-policy.md](release-version-policy.md) — version/tag compatibility gate
- [ollama-outage.md](ollama-outage.md)
- [sonder-inference.md](sonder-inference.md) — run tiers on `sonder-infer serve`, doctor, fallback
- [database-lock-or-corruption.md](database-lock-or-corruption.md)
- [fault-injection-testing.md](fault-injection-testing.md) — deterministic offline reliability fixtures and contracts
- [autopilot-interruption.md](autopilot-interruption.md)
- [fleet-retry-recovery.md](fleet-retry-recovery.md) — replaying interrupted masters, transient worker retries
- [selfmod-continuous-loop.md](selfmod-continuous-loop.md) — unattended nightly self-improvement loop
- [merged-branch-cleanup.md](merged-branch-cleanup.md) — dry-run-first merged worktree cleanup
- [disk-exhaustion.md](disk-exhaustion.md)
- [training-failure.md](training-failure.md)
- [suspected-secret-exposure.md](suspected-secret-exposure.md)
- [unsafe-lab.md](unsafe-lab.md) — disposable isolated-host testing with model tool policy deliberately removed
- [multi-pc-ollama.md](multi-pc-ollama.md) — pool independent Ollama hosts over HTTPS
- [remote-ollama-caddy.md](remote-ollama-caddy.md) — TLS-fronted remote Ollama behind Caddy, coordinator settings
- [security-hardening.md](security-hardening.md) — setup-time security hardening checklist
- [multi-node-windows-private-link.md](multi-node-windows-private-link.md) — prepare and validate Windows workers over a dedicated private link
- [rpc-sharding.md](rpc-sharding.md) — opt-in: split one model across two machines with llama.cpp RPC
- [mesh-generation.md](mesh-generation.md) — model-driven 3D asset generation in headless Blender (`mesh_generate`)
- [multi-node-ollama.md](multi-node-ollama.md) — worker host OS setup and external membership
- [compute-fabric.md](compute-fabric.md) — remote compute workers and controller
- [deployment-topology.md](deployment-topology.md) — deployment topology and capability status
- [client-surface-contracts.md](client-surface-contracts.md) — client surface contracts
- [release-smoke-check.md](release-smoke-check.md) — pre-tag release smoke check
- [nightly-selfmod-windows.md](nightly-selfmod-windows.md) — Windows nightly selfmod deployment
- [artifact-cache-retention.md](artifact-cache-retention.md) — artifact cache retention
- [compute-indexed-inventory.md](compute-indexed-inventory.md) — reference, not setup: indexed compute inventory and refresh
- [takeover-readiness.md](takeover-readiness.md) — reference, not setup: takeover readiness
- [formal-reasoning.md](formal-reasoning.md) — formal reasoning setup
- [inference-membership-acceptance.md](inference-membership-acceptance.md) — disposable private inference membership acceptance
- [partition-routing.md](partition-routing.md) — partition routing and synthetic cluster acceptance
- [performance.md](performance.md) — performance measurement and safe acceleration
- [reconnect-reconciliation.md](reconnect-reconciliation.md) — client reconnect and worker receipt reconciliation
- [replication-acknowledgement.md](replication-acknowledgement.md) — replication acknowledgement
- [worker-epoch-fence.md](worker-epoch-fence.md) — worker owner epoch effect fence

For conceptual, in-depth documentation of each subsystem, see the
[Wiki](../wiki/README.md).
