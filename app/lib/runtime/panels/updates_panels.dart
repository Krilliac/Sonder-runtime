part of '../runtime_screen.dart';

class _ExtensionRegistrySection extends StatelessWidget {
  final ExtensionRegistryStatus status;

  const _ExtensionRegistrySection({required this.status});

  @override
  Widget build(BuildContext context) {
    return _Section(
      title: 'Extensions',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusRow(
            label: 'Persistence',
            value: status.persistence,
            ok: status.persistence == 'durable',
          ),
          if (status.records.isEmpty)
            const Text('No extension installations are registered.')
          else
            for (final record in status.records)
              _StatusRow(
                label: record.extensionId,
                value: '${record.scope} v${record.version} — '
                    '${record.healthState}${record.enabled ? ' — enabled' : ' — disabled'}'
                    '${record.memoryLimitBytes == null ? '' : ' — memory ${record.memoryLimitBytes} B'}',
                ok: record.enabled && record.healthState == 'healthy',
              ),
        ],
      ),
    );
  }
}

/// Update section for the System page (SPEC-4 section 14): installed
/// version/channel, active/previous releases, available updates with
/// verification state, and a rollback affordance. Install/rollback are
/// admin operations on the CLI surface; the UI surfaces durable state and
/// the confirmation nonce so an operator can complete them deliberately.
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

    return _Section(
      title: 'Updates',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusRow(
            label: 'Running',
            value: '${status.runningVersion} '
                '(${status.platform}/${status.architecture})',
            ok: true,
          ),
          if (status.runningCommit.isNotEmpty)
            _StatusRow(
              label: 'Commit',
              value: status.runningCommit.length > 12
                  ? status.runningCommit.substring(0, 12)
                  : status.runningCommit,
              ok: true,
            ),
          _StatusRow(
            label: 'Active release',
            value: active != null
                ? '${active.version} (${active.releaseId})'
                : 'source checkout',
            ok: active != null,
            off: true,
          ),
          if (previous != null)
            _StatusRow(
              label: 'Previous',
              value: '${previous.version} (${previous.releaseId})',
              ok: true,
            ),
          const Divider(height: 20),
          if (available.isEmpty && inFlight.isEmpty)
            Text('No pending updates. Import a signed bundle to check.',
                style: Theme.of(context).textTheme.bodyMedium)
          else ...[
            for (final plan in available)
              _StatusRow(
                label: 'Available',
                value: '${plan.targetVersion} (${plan.channel}) — verified; '
                    'confirm nonce ${plan.confirmNonce ?? "?"}',
                ok: true,
              ),
            for (final plan in inFlight)
              _StatusRow(
                label: plan.status,
                value: '${plan.targetVersion} (${plan.channel})',
                ok: true,
              ),
          ],
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (available.isNotEmpty)
                FilledButton.tonalIcon(
                  onPressed: () => _showInstallHint(context, available.first),
                  icon: const Icon(Icons.download_done, size: 18),
                  label: const Text('Install…'),
                ),
              if (status.canRollback)
                OutlinedButton.icon(
                  onPressed: () => _showRollbackHint(context, previous!),
                  icon: const Icon(Icons.history, size: 18),
                  label: const Text('Roll back…'),
                ),
            ],
          ),
        ],
      ),
    );
  }

  void _showInstallHint(BuildContext context, UpdatePlan plan) {
    _showDialog(
      context,
      'Install ${plan.targetVersion}',
      'Installation is an administrator operation performed with an '
          'explicit confirmation nonce so it never runs silently:\n\n'
          'sonder update install ${plan.updateId} '
          '--confirm ${plan.confirmNonce ?? "<nonce>"}\n\n'
          'The runtime drains, takes a verified backup, migrates, health-'
          'checks the new release, and switches atomically. This page shows '
          'the durable progress and survives the service restart.',
    );
  }

  void _showRollbackHint(BuildContext context, UpdateRelease previous) {
    final nonce = previous.releaseId.length >= 8
        ? previous.releaseId.substring(previous.releaseId.length - 8)
        : previous.releaseId;
    _showDialog(
      context,
      'Roll back to ${previous.version}',
      'Rollback restores the previous release and is impact-aware — code '
          'rollback when compatible, otherwise a state restore from the '
          'pre-update backup:\n\n'
          'sonder update rollback --confirm $nonce\n\n'
          'The failed release and its evidence are retained.',
    );
  }

  void _showDialog(BuildContext context, String title, String body) {
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: SingleChildScrollView(child: SelectableText(body)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }
}
