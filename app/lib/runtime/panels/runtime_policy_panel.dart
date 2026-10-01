part of '../runtime_screen.dart';

String _capitalized(String value) =>
    value.isEmpty ? value : '${value[0].toUpperCase()}${value.substring(1)}';

/// The shared local runtime policy: which local model each alias names, and
/// which alias each automatic lane uses. Read-only here; guarded edits go
/// through `/runtime set`, shown as a command to copy.
class _RuntimePolicyPanel extends StatelessWidget {
  final RuntimePolicyInfo policy;

  const _RuntimePolicyPanel({required this.policy});

  static const _tiers = ['fast', 'code', 'general'];
  static const _lanes = ['router', 'workbench', 'autopilot', 'fleet', 'review'];

  @override
  Widget build(BuildContext context) {
    final warnings = <String>[
      if (policy.error.isNotEmpty) '${policy.error} (safe defaults are active)',
      if (policy.inventoryError.isNotEmpty)
        'Model inventory unavailable: ${policy.inventoryError}',
      if (policy.missingModels.isNotEmpty)
        'Missing local models: ${policy.missingModels.join(', ')}',
    ];
    return Column(
      key: const Key('runtime-policy-panel'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(
          title: 'Local model aliases',
          description: [
            'Shared policy r${policy.revision}',
            if (policy.source.isNotEmpty) policy.source,
          ].join(' · '),
          trailing: StatusPill(
              policy.hasWarning ? StatusKind.warn : StatusKind.ok,
              word: policy.hasWarning ? 'warn' : 'ok',
              dense: true),
          children: [
            if (warnings.isNotEmpty)
              Padding(
                padding: const EdgeInsets.all(SonderSpace.lg),
                child: WorkspaceNotice(
                  kind: StatusKind.warn,
                  title: warnings.first,
                  detail:
                      warnings.length > 1 ? warnings.skip(1).join('\n') : null,
                  liveRegion: false,
                ),
              ),
            for (final tier in _tiers)
              ValueRow(
                key: Key('policy-alias-$tier'),
                label: _capitalized(tier),
                value: policy.localModels[tier] ?? 'unassigned',
                mono: true,
              ),
          ],
        ),
        SettingsSection(
          title: 'Execution lanes',
          description: 'Which alias each automatic lane runs on.',
          children: [
            for (final lane in _lanes)
              ValueRow(
                key: Key('policy-lane-$lane'),
                label: _capitalized(lane),
                value: [
                  policy.routing[lane] ?? 'unassigned',
                  if (policy.modelForLane(lane).isNotEmpty)
                    policy.modelForLane(lane),
                ].join(' · '),
                mono: true,
              ),
            const SettingRow(
              label: 'Change a lane',
              description: 'Guarded edits go through the runtime; run this '
                  'in Chat or the Developer console.',
              trailing: _CommandLine('/runtime set workbench=general',
                  label: 'lane command'),
            ),
            if (policy.path.isNotEmpty)
              ValueRow(
                label: 'Policy file',
                value: policy.path,
                mono: true,
                copyable: true,
              ),
          ],
        ),
      ],
    );
  }
}
