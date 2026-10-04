part of '../runtime_screen.dart';

/// Safe self-improvement: whether it is on, its mode, active runs and
/// rollback points, the recent runs, and a status read on demand.
class _SelfmodPanel extends StatelessWidget {
  final _RuntimeScreenState s;
  final SelfmodInfo info;

  const _SelfmodPanel({required this.s, required this.info});

  @override
  Widget build(BuildContext context) {
    return SettingsSection(
      key: const Key('selfmod-panel'),
      title: 'Safe self-improvement',
      description: 'Changes the runtime proposes to its own code, tested '
          'and reversible.',
      trailing: StatusPill(info.enabled ? StatusKind.ok : StatusKind.skipped,
          word: info.enabled ? 'on' : 'off', dense: true),
      children: [
        RuntimeStatStrip([
          RuntimeStat('Mode', info.mode),
          RuntimeStat('Active', '${info.active}'),
          RuntimeStat('Deployed', '${info.deployed}'),
          RuntimeStat('Rollback points', '${info.rollbackPoints}'),
        ]),
        if (info.backupRoot.isNotEmpty)
          ValueRow(
            label: 'Backups',
            value: info.backupRoot,
            mono: true,
            copyable: true,
          ),
        for (final run in info.runs.take(5))
          RuntimeRow(
            kind: autopilotStatusKind('${run['phase'] ?? ''}'),
            word: '${run['phase'] ?? 'unknown'}',
            title: RuntimeRowTitle('${run['objective'] ?? run['id'] ?? ''}'),
            subtitle: RuntimeRowDetail(
                [
                  '${run['id'] ?? ''}',
                  if ('${run['risk'] ?? ''}'.isNotEmpty) 'risk ${run['risk']}',
                ].where((part) => part.isNotEmpty).join(' · '),
                mono: true),
          ),
        SettingRow(
          label: 'Status report',
          description: 'Diffs, tests and rollbacks stay in the console: '
              '/selfmod diff, /selfmod tests, /selfmod rollback.',
          trailing: AsyncActionButton(
            label: 'Read status',
            busyLabel: 'Reading…',
            doneLabel: null,
            busy: s._busy('selfmod'),
            onPressed: () => s._trackCommand('selfmod', '/selfmod status',
                title: 'Self-improvement status'),
            onError: (_, __) {},
          ),
          below: _trackedView(s, 'selfmod'),
        ),
      ],
    );
  }
}
