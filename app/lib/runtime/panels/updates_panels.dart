part of '../runtime_screen.dart';

/// Installed extensions and where their registry persists.
class _ExtensionRegistrySection extends StatelessWidget {
  final ExtensionRegistryStatus status;

  const _ExtensionRegistrySection({required this.status});

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      key: const Key('extensions-section'),
      title: 'Extensions',
      children: [
        StatusValueRow(
          label: 'Persistence',
          value: status.persistence,
          kind:
              status.persistence == 'durable' ? StatusKind.ok : StatusKind.warn,
        ),
        if (status.records.isEmpty)
          const RuntimeEmptyRow('No extension installations are registered.',
              icon: Icons.extension_outlined)
        else
          for (final record in status.records)
            RuntimeRow(
              kind: record.enabled && record.healthState == 'healthy'
                  ? StatusKind.ok
                  : record.enabled
                      ? StatusKind.warn
                      : StatusKind.skipped,
              word: record.enabled ? record.healthState : 'off',
              title: RuntimeRowTitle(record.extensionId, mono: true),
              subtitle: RuntimeRowDetail([
                '${record.scope} · v${record.version}',
                record.enabled ? 'enabled' : 'disabled',
                if (record.memoryLimitBytes != null)
                  'memory ${record.memoryLimitBytes} B',
              ].join(' · ')),
            ),
      ],
    );
  }
}

/// Releases (SPEC-4 section 14): the installed version, active and previous
/// releases, verified updates and rollback. Install and rollback are
/// administrator operations done at the console with an explicit nonce, so
/// this page shows the exact command to copy instead of pretending to run
/// them.
class _UpdateSection extends StatelessWidget {
  final UpdateStatus status;

  const _UpdateSection({required this.status});

  @override
  Widget build(BuildContext context) {
    final active = status.activeRelease;
    final previous = status.previousRelease;
    final available = status.plans.where((p) => p.isAvailable).toList();
    final inFlight =
        status.plans.where((p) => !p.isAvailable && !p.isTerminal).toList();
    final rollbackNonce = previous == null
        ? ''
        : previous.releaseId.length >= 8
            ? previous.releaseId.substring(previous.releaseId.length - 8)
            : previous.releaseId;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(
          key: const Key('updates-section'),
          title: 'This runtime',
          children: [
            ValueRow(
              label: 'Running',
              value: '${status.runningVersion} '
                  '(${status.platform}/${status.architecture})',
            ),
            if (status.runningCommit.isNotEmpty)
              ValueRow(
                label: 'Commit',
                value: status.runningCommit.length > 12
                    ? status.runningCommit.substring(0, 12)
                    : status.runningCommit,
                mono: true,
              ),
            ValueRow(
              label: 'Active release',
              value: active != null
                  ? '${active.version} (${active.releaseId})'
                  : 'source checkout',
            ),
            if (previous != null)
              ValueRow(
                label: 'Previous',
                value: '${previous.version} (${previous.releaseId})',
              ),
          ],
        ),
        SettingsSection(
          title: 'Updates',
          description: 'Installing drains the runtime, takes a verified '
              'backup, health-checks the new release and switches '
              'atomically.',
          children: [
            if (available.isEmpty && inFlight.isEmpty)
              const RuntimeEmptyRow(
                  'No pending updates. Import a signed bundle to check.',
                  icon: Icons.system_update_alt_outlined),
            for (final plan in available)
              SettingRow(
                label: 'Install ${plan.targetVersion}',
                description: '${plan.channel} · verified. Run on the '
                    'runtime host:',
                below: _CommandLine(
                  'sonder update install ${plan.updateId} '
                  '--confirm ${plan.confirmNonce ?? '<nonce>'}',
                  label: 'install command',
                ),
              ),
            for (final plan in inFlight)
              RuntimeRow(
                kind: StatusKind.running,
                word: plan.status,
                title: RuntimeRowTitle(plan.targetVersion, mono: true),
                subtitle: RuntimeRowDetail(plan.channel),
              ),
            if (status.canRollback && previous != null)
              SettingRow(
                label: 'Roll back to ${previous.version}',
                description: 'Restores the previous release; the failed '
                    'release and its evidence are kept. Run on the runtime '
                    'host:',
                below: _CommandLine(
                  'sonder update rollback --confirm $rollbackNonce',
                  label: 'rollback command',
                ),
              ),
          ],
        ),
      ],
    );
  }
}
