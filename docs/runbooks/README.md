# Sonder runtime runbooks (SPEC-2 WP10)

Contractor-executable operational procedures. Each runbook assumes the
server-private reference deployment (systemd, loopback bind, reverse
proxy for remote access) unless it says otherwise.

- [install-server-private.md](install-server-private.md)
- [install-workstation-local.md](install-workstation-local.md)
- [use-facts-model.md](use-facts-model.md) — import a portable GGUF (facts. USB)
- [assemble-model-collection.md](assemble-model-collection.md) — a routed collection of specialist models
- [secure-remote-access.md](secure-remote-access.md)
- [start-stop-drain.md](start-stop-drain.md)
- [rotate-credentials.md](rotate-credentials.md)
- [backup-restore.md](backup-restore.md)
- [upgrade-rollback.md](upgrade-rollback.md)
- [publish-release.md](publish-release.md) — TUF signing ceremony
- [release-version-policy.md](release-version-policy.md) — version/tag compatibility gate
- [ollama-outage.md](ollama-outage.md)
- [database-lock-or-corruption.md](database-lock-or-corruption.md)
- [fault-injection-testing.md](fault-injection-testing.md) — deterministic offline reliability fixtures and contracts
- [autopilot-interruption.md](autopilot-interruption.md)
- [fleet-retry-recovery.md](fleet-retry-recovery.md) — replaying interrupted masters, transient worker retries
- [merged-branch-cleanup.md](merged-branch-cleanup.md) — dry-run-first merged worktree cleanup
- [disk-exhaustion.md](disk-exhaustion.md)
- [training-failure.md](training-failure.md)
- [suspected-secret-exposure.md](suspected-secret-exposure.md)
- [unsafe-lab.md](unsafe-lab.md) — disposable isolated-host testing with model tool policy deliberately removed
- [multi-pc-ollama.md](multi-pc-ollama.md) — pool independent Ollama hosts over HTTPS

For conceptual, in-depth documentation of each subsystem, see the
[Wiki](../wiki/README.md).
