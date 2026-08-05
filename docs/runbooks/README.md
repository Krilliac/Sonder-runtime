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
- [ollama-outage.md](ollama-outage.md)
- [database-lock-or-corruption.md](database-lock-or-corruption.md)
- [autopilot-interruption.md](autopilot-interruption.md)
- [disk-exhaustion.md](disk-exhaustion.md)
- [training-failure.md](training-failure.md)
- [suspected-secret-exposure.md](suspected-secret-exposure.md)

For conceptual, in-depth documentation of each subsystem, see the
[Wiki](../wiki/README.md).
