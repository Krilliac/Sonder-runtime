part of '../runtime_screen.dart';

class _ContextHealthPanel extends StatelessWidget {
  final ContextHealth health;

  const _ContextHealthPanel({required this.health});

  @override
  Widget build(BuildContext context) {
    final status = health.status.isEmpty ? 'unknown' : health.status;
    final sessionTitle = health.title.isEmpty ? health.session : health.title;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Chip(
              avatar: Icon(
                _statusIcon(status),
                size: 18,
                color: _statusColor(context, status),
              ),
              label: Text('Context $status'),
            ),
            Chip(
              avatar: const Icon(Icons.forum_outlined, size: 18),
              label: Text('Session: $sessionTitle'),
            ),
            Chip(
              avatar: const Icon(Icons.folder_copy_outlined, size: 18),
              label: Text('Project: ${health.project}'),
            ),
            Chip(
              avatar: const Icon(Icons.view_week_outlined, size: 18),
              label: Text(
                  '${health.contextMode}: native ${health.nativeContextLimit}'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _MeterBar(
          label: 'Context',
          percent: health.contextPercent,
          detail:
              '~${health.estimatedTokens}/${health.contextLimit} virtual tokens',
          color: _statusColor(context, status),
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Live turns',
          percent: health.turnPercent,
          detail:
              '${health.liveTurns}/${health.maxLiveTurns} kept live, ${health.totalTurns} total',
        ),
        const SizedBox(height: 10),
        _MeterBar(
          label: 'Memory',
          percent: health.memoryPercent,
          detail:
              '${health.lessons} lessons, ${health.facts} facts, ${health.interactions} interactions',
        ),
        const SizedBox(height: 12),
        _OutputCard(text: health.consoleText()),
        if (health.updatedTs.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            'Last updated ${health.updatedTs}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ],
    );
  }

  IconData _statusIcon(String status) {
    if (status == 'hot') return Icons.warning_amber_outlined;
    if (status == 'warm') return Icons.thermostat_outlined;
    if (status == 'healthy') return Icons.check_circle_outline;
    return Icons.info_outline;
  }

  Color _statusColor(BuildContext context, String status) {
    final cs = Theme.of(context).colorScheme;
    if (status == 'hot') return cs.error;
    if (status == 'warm') return Colors.amber.shade800;
    if (status == 'healthy') return cs.primary;
    return cs.outline;
  }
}
