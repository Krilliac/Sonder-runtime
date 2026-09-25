part of '../runtime_screen.dart';

class _AutopilotPanel extends StatelessWidget {
  final AutopilotStatus status;
  final ValueChanged<AutopilotRun> onResume;
  final ValueChanged<AutopilotRun> onPause;
  final ValueChanged<AutopilotRun> onCancel;

  const _AutopilotPanel({
    required this.status,
    required this.onResume,
    required this.onPause,
    required this.onCancel,
  });

  @override
  Widget build(BuildContext context) {
    final run = status.latest;
    final colors = Theme.of(context).colorScheme;
    if (run == null) {
      return const _OutputText(
        'No autonomous goals yet. Planning creates a restart-persistent run.',
      );
    }
    final passed = run.tasks.where((task) => task.status == 'passed').length;
    final superseded =
        run.tasks.where((task) => task.status == 'superseded').length;
    final complete = passed + superseded;
    final progress = run.tasks.isEmpty ? 0.0 : complete / run.tasks.length;
    final color = _runColor(colors, run.status);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(_runIcon(run.status), size: 18, color: color),
              label: Text(run.status.replaceAll('_', ' ')),
            ),
            Chip(
              avatar: const Icon(Icons.layers_outlined, size: 18),
              label: Text('${status.activeRuns} active'),
            ),
            Chip(
              avatar: const Icon(Icons.pause_circle_outline, size: 18),
              label: Text('${status.resumableRuns} resumable'),
            ),
            Chip(
              avatar: Icon(
                run.policy == 'observe'
                    ? Icons.visibility_outlined
                    : Icons.edit_note_outlined,
                size: 18,
              ),
              label: Text(run.policy),
            ),
            Chip(
              avatar: const Icon(Icons.memory_outlined, size: 18),
              label: Text(run.tier.isEmpty ? 'local' : 'local ${run.tier}'),
            ),
            Chip(
              avatar: Icon(
                run.allowWeb
                    ? Icons.public_outlined
                    : Icons.public_off_outlined,
                size: 18,
              ),
              label: Text(run.allowWeb ? 'web on' : 'web off'),
            ),
            Chip(
              avatar: Icon(
                run.adaptive ? Icons.route_outlined : Icons.linear_scale,
                size: 18,
              ),
              label: Text(run.adaptive ? 'adaptive' : 'static plan'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          run.objective,
          style: Theme.of(context).textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w700,
              ),
        ),
        const SizedBox(height: 4),
        SelectableText(
          '${run.id} • ${run.phase} • ${run.project.isEmpty ? 'default project' : run.project}',
          style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: colors.onSurfaceVariant,
                fontFamily: SonderTheme.mono,
              ),
        ),
        const SizedBox(height: 10),
        LinearProgressIndicator(
          value: progress.clamp(0.0, 1.0),
          minHeight: 7,
          borderRadius: BorderRadius.circular(99),
          color: color,
        ),
        const SizedBox(height: 6),
        Text(
          '$complete/${run.tasks.length} tasks settled • '
          '${run.cycles} cycles • ${run.failures}/${run.maxFailures} failures • '
          '${run.checkpoints} checkpoint${run.checkpoints == 1 ? '' : 's'} • '
          '${run.replans}/${run.maxReplans} replans',
          style: Theme.of(context).textTheme.bodySmall,
        ),
        if (run.summary.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(run.summary),
        ],
        if (run.lastError.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            run.lastError,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: colors.error,
                  fontWeight: FontWeight.w600,
                ),
          ),
        ],
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            if (run.isResumable)
              FilledButton.tonalIcon(
                onPressed: () => onResume(run),
                icon: const Icon(Icons.play_arrow_outlined),
                label: const Text('Resume'),
              ),
            if (run.isActive)
              OutlinedButton.icon(
                onPressed: () => onPause(run),
                icon: const Icon(Icons.pause_outlined),
                label: const Text('Pause'),
              ),
            if (!run.isTerminal)
              TextButton.icon(
                onPressed: () => onCancel(run),
                icon: const Icon(Icons.close_outlined),
                label: const Text('Cancel'),
              ),
          ],
        ),
        if (run.criteria.isNotEmpty) ...[
          const SizedBox(height: 14),
          Text('Success gates', style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 6),
          ...run.criteria.map(
            (criterion) => Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.flag_outlined, size: 16, color: colors.primary),
                  const SizedBox(width: 7),
                  Expanded(child: Text(criterion)),
                ],
              ),
            ),
          ),
        ],
        if (run.tasks.isNotEmpty) ...[
          const SizedBox(height: 14),
          Text('Persistent checklist',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 7),
          ...run.tasks.map(
            (task) => Container(
              width: double.infinity,
              margin: const EdgeInsets.only(bottom: 7),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: colors.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: colors.outlineVariant),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    _taskIcon(task.status),
                    size: 19,
                    color: _runColor(colors, task.status),
                  ),
                  const SizedBox(width: 9),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${task.id} • ${task.kind} • ${task.title}',
                          style: Theme.of(context).textTheme.labelLarge,
                        ),
                        const SizedBox(height: 3),
                        Text(
                          task.error.isNotEmpty ? task.error : task.instruction,
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    task.status.replaceAll('_', ' '),
                    style: Theme.of(context).textTheme.labelSmall?.copyWith(
                          color: _runColor(colors, task.status),
                        ),
                  ),
                ],
              ),
            ),
          ),
        ],
        if (status.events.isNotEmpty) ...[
          const SizedBox(height: 8),
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            childrenPadding: const EdgeInsets.only(bottom: 8),
            title: const Text('Run events'),
            subtitle: Text('${status.events.length} persisted checkpoints'),
            children: [
              _OutputCard(
                text: status.events
                    .map((event) => '${event.kind}: ${event.message}')
                    .join('\n'),
              ),
            ],
          ),
        ],
        if (run.finalReport.isNotEmpty) ...[
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            childrenPadding: const EdgeInsets.only(bottom: 8),
            title: const Text('End report'),
            subtitle: const Text('Evidence-backed task ledger'),
            children: [_OutputCard(text: run.finalReport)],
          ),
        ],
      ],
    );
  }

  static IconData _runIcon(String value) {
    if (value == 'completed' || value == 'passed') {
      return Icons.check_circle_outline;
    }
    if (value == 'running' || value == 'planning' || value == 'in_progress') {
      return Icons.sync;
    }
    if (value == 'failed' || value == 'blocked') return Icons.error_outline;
    if (value == 'cancelled' || value == 'superseded') {
      return Icons.remove_circle_outline;
    }
    return Icons.pause_circle_outline;
  }

  static IconData _taskIcon(String value) => _runIcon(value);

  static Color _runColor(ColorScheme colors, String value) {
    if (value == 'completed' || value == 'passed') return colors.primary;
    if (value == 'failed' || value == 'blocked') return colors.error;
    if (value == 'running' || value == 'planning' || value == 'in_progress') {
      return Colors.amber.shade800;
    }
    return colors.outline;
  }
}
