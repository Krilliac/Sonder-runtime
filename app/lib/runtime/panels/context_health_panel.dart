part of '../runtime_screen.dart';

/// Context health of the current session: how full the context is, how many
/// turns stay live, and how much memory backs it, as meters (the console's
/// ASCII bars said the same thing twice and are gone).
class _ContextHealthPanel extends StatelessWidget {
  final ContextHealth health;

  const _ContextHealthPanel({required this.health});

  @override
  Widget build(BuildContext context) {
    final status = health.status.isEmpty ? 'unknown' : health.status;
    final kind = switch (status) {
      'healthy' => StatusKind.ok,
      'warm' => StatusKind.warn,
      'hot' => StatusKind.warn,
      _ => StatusKind.unknown,
    };
    final session = health.title.isEmpty ? health.session : health.title;
    final ratio = (health.contextPercent / 100).clamp(0.0, 1.0);
    return SettingsSection(
      key: const Key('context-health-panel'),
      title: 'Context',
      description: [
        if (session.isNotEmpty) session,
        if (health.project.isNotEmpty) 'project ${health.project}',
      ].join(' · '),
      trailing: StatusPill(kind,
          word: status == 'hot' ? 'needs you' : kind.runtimeWord, dense: true),
      children: [
        RuntimeCardBody(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Meter(
                value: ratio,
                label: 'Context',
                valueLabel: '~${compactCount(health.estimatedTokens)} / '
                    '${compactCount(health.contextLimit)}',
              ),
              const _MeterCaption('Tokens of context this session holds'),
              const SizedBox(height: SonderSpace.lg),
              Meter(
                value: (health.turnPercent / 100).clamp(0.0, 1.0),
                label: 'Live turns',
                valueLabel: '${health.liveTurns} / ${health.maxLiveTurns}',
              ),
              _MeterCaption('Turns kept in full, of ${health.totalTurns} in '
                  'total; older turns are summarized'),
              const SizedBox(height: SonderSpace.lg),
              Meter(
                value: (health.memoryPercent / 100).clamp(0.0, 1.0),
                label: 'Memory',
                valueLabel: '${health.memoryPercent.round()}%',
                warnAt: 1.1,
                dangerAt: 1.1,
              ),
              _MeterCaption('${health.lessons} lessons · ${health.facts} '
                  'facts · ${health.interactions} interactions'),
            ],
          ),
        ),
        ValueRow(
          label: 'Mode',
          value: '${health.contextMode} · native '
              '${compactCount(health.nativeContextLimit)}',
        ),
        if (health.updatedTs.isNotEmpty)
          ValueRow(label: 'Last updated', value: health.updatedTs),
      ],
    );
  }
}
