part of '../runtime_screen.dart';

class WorkbenchActivityPanel extends StatelessWidget {
  final ActivityResponse response;
  final int totalToolCalls;

  const WorkbenchActivityPanel({
    super.key,
    required this.response,
    required this.totalToolCalls,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final recentActions = response.actions.length <= 8
        ? response.actions
        : response.actions.sublist(response.actions.length - 8);
    final completed =
        response.checklist.where((item) => item.status == 'done').length;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            Chip(
              avatar: Icon(
                response.status == 'error'
                    ? Icons.error_outline
                    : response.status == 'running'
                        ? Icons.sync
                        : Icons.check_circle_outline,
                size: 18,
              ),
              label:
                  Text(response.status.isEmpty ? 'Unknown' : response.status),
            ),
            Chip(
              avatar: const Icon(Icons.build_outlined, size: 18),
              label: Text('${response.toolCalls} actions'),
            ),
            Chip(
              avatar: const Icon(Icons.history, size: 18),
              label: Text('$totalToolCalls total'),
            ),
            Chip(
              avatar: const Icon(Icons.timer_outlined, size: 18),
              label: Text('${response.elapsedMs} ms'),
            ),
          ],
        ),
        if (response.resultSummary.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(
            response.resultSummary,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
          ),
        ],
        if (response.checklist.isNotEmpty) ...[
          const SizedBox(height: 14),
          Row(
            children: [
              Icon(Icons.checklist_rounded, size: 19, color: cs.primary),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  response.checklistTitle.isEmpty
                      ? 'Checklist'
                      : response.checklistTitle,
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
              Text('$completed/${response.checklist.length}'),
            ],
          ),
          const SizedBox(height: 8),
          ...response.checklist.map((item) => Padding(
                padding: const EdgeInsets.only(bottom: 7),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(
                      _checklistIcon(item.status),
                      size: 18,
                      color: _checklistColor(cs, item.status),
                    ),
                    const SizedBox(width: 8),
                    Expanded(child: Text(item.title)),
                    const SizedBox(width: 8),
                    Text(
                      item.status.replaceAll('_', ' '),
                      style: Theme.of(context).textTheme.labelSmall?.copyWith(
                            color: _checklistColor(cs, item.status),
                          ),
                    ),
                  ],
                ),
              )),
        ],
        const SizedBox(height: 10),
        Row(
          children: [
            Icon(Icons.receipt_long_outlined, size: 19, color: cs.primary),
            const SizedBox(width: 7),
            Text('Exact actions',
                style: Theme.of(context).textTheme.titleSmall),
          ],
        ),
        const SizedBox(height: 8),
        if (recentActions.isEmpty)
          const _OutputText('No tool actions recorded yet.')
        else
          ...recentActions.map((action) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(11),
                  decoration: BoxDecoration(
                    color: cs.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      color: action.ok
                          ? cs.outlineVariant
                          : cs.error.withValues(alpha: 0.55),
                    ),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(
                            action.ok
                                ? Icons.check_circle_outline
                                : Icons.error_outline,
                            size: 17,
                            color: action.ok ? cs.primary : cs.error,
                          ),
                          const SizedBox(width: 7),
                          Expanded(
                            child: Text(
                              action.title,
                              style: Theme.of(context)
                                  .textTheme
                                  .labelLarge
                                  ?.copyWith(fontWeight: FontWeight.w700),
                            ),
                          ),
                          Text('+${action.elapsedMs}ms'),
                        ],
                      ),
                      if (action.evidence.isNotEmpty) ...[
                        const SizedBox(height: 7),
                        SelectableText(
                          action.evidence,
                          style:
                              Theme.of(context).textTheme.bodySmall?.copyWith(
                                    fontFamily: SonderTheme.mono,
                                    height: 1.3,
                                  ),
                        ),
                      ],
                    ],
                  ),
                ),
              )),
      ],
    );
  }

  IconData _checklistIcon(String status) {
    if (status == 'done') return Icons.check_circle;
    if (status == 'in_progress') return Icons.pending;
    if (status == 'blocked') return Icons.error;
    return Icons.radio_button_unchecked;
  }

  Color _checklistColor(ColorScheme colors, String status) {
    if (status == 'done') return colors.primary;
    if (status == 'blocked') return colors.error;
    if (status == 'in_progress') return Colors.amber.shade800;
    return colors.outline;
  }
}

class LiveExecutionFeed extends StatelessWidget {
  final ExecutionFeed? feed;
  final bool offline;
  final int maxVisible;

  const LiveExecutionFeed({
    super.key,
    required this.feed,
    this.offline = false,
    this.maxVisible = 12,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final limit = maxVisible.clamp(1, 20).toInt();
    final events = feed?.events ?? const <ExecutionFeedEvent>[];
    final visible =
        events.length <= limit ? events : events.sublist(events.length - limit);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(Icons.dynamic_feed_outlined, size: 19, color: cs.primary),
            const SizedBox(width: 7),
            Text('Live execution',
                style: Theme.of(context).textTheme.titleSmall),
            const Spacer(),
            Text('${visible.length}/${events.length} events',
                style: Theme.of(context).textTheme.labelSmall),
          ],
        ),
        const SizedBox(height: 8),
        if (offline)
          const _ExecutionFeedPlaceholder(
            icon: Icons.cloud_off_outlined,
            title: 'Offline',
            message:
                'Live execution events are unavailable while disconnected.',
          )
        else if (feed == null || !feed!.known)
          const _ExecutionFeedPlaceholder(
            icon: Icons.help_outline,
            title: 'Unavailable',
            message: 'This runtime does not publish an execution feed.',
          )
        else if (feed!.error.isNotEmpty)
          const _ExecutionFeedPlaceholder(
            icon: Icons.error_outline,
            title: 'Unavailable',
            message: 'The runtime reported an execution feed error.',
          )
        else if (visible.isEmpty)
          const _ExecutionFeedPlaceholder(
            icon: Icons.help_outline,
            title: 'Unknown',
            message: 'No execution events are available yet.',
          )
        else ...[
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              Chip(label: Text('schema ${feed!.schemaVersion}')),
              Chip(
                label: Text(feed!.activeResponses == null
                    ? 'active unknown'
                    : '${feed!.activeResponses} active'),
              ),
              if (feed!.oldestSeq != null && feed!.nextSeq != null)
                Chip(
                  label: Text('window ${feed!.oldestSeq} → ${feed!.nextSeq}'),
                ),
              if ((feed!.droppedEvents ?? 0) > 0)
                Chip(label: Text('${feed!.droppedEvents} dropped')),
              if (feed!.detailsDisabled)
                const Chip(label: Text('Details disabled')),
              if (feed!.hasGap) const Chip(label: Text('Sequence gap')),
              if (feed!.truncated) const Chip(label: Text('History truncated')),
              if (feed!.redactionApplied)
                const Chip(label: Text('Redaction applied')),
            ],
          ),
          const SizedBox(height: 8),
          ...visible.map((event) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _ExecutionEventCard(event: event),
              )),
        ],
      ],
    );
  }
}

class _ExecutionFeedPlaceholder extends StatelessWidget {
  final IconData icon;
  final String title;
  final String message;

  const _ExecutionFeedPlaceholder({
    required this.icon,
    required this.title,
    required this.message,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Icon(icon, color: cs.outline),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: Theme.of(context).textTheme.labelLarge),
                const SizedBox(height: 2),
                Text(message, style: Theme.of(context).textTheme.bodySmall),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ExecutionEventCard extends StatelessWidget {
  final ExecutionFeedEvent event;

  const _ExecutionEventCard({required this.event});

  @override
  Widget build(BuildContext context) {
    final status = event.status;
    final error = status == 'error' || status == 'failed';
    final details = <String>[
      if (event.seq > 0) '#${event.seq}',
      if (event.elapsedMs > 0) '${event.elapsedMs}ms',
      if (event.model.isNotEmpty) 'model ${event.model}',
      if (event.promptChars > 0) 'prompt ${event.promptChars} chars',
      if (event.historyMessages > 0) '${event.historyMessages} history',
      if (event.tokensIn != 0 || event.tokensOut != 0)
        'tokens ${event.tokensIn}/${event.tokensOut}',
      if (event.tool.isNotEmpty) 'tool ${event.tool}',
      if (event.fileOperation.isNotEmpty) 'file ${event.fileOperation}',
      if (event.path.isNotEmpty) event.path,
      if (event.bytes > 0) '${event.bytes} bytes',
      if (event.dryRun) 'dry run',
    ];
    final tokens = SonderTokens.of(context);
    final tone = error ? tokens.danger : tokens.ok;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(12, 9, 12, 9),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(color: error ? tokens.danger : tokens.hairline),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(_timestampLabel(event.timestamp),
                  style: tokens.mono(11, color: tokens.muted)),
              const SizedBox(width: 10),
              Icon(_eventIcon(event.kind), size: 14, color: tone),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  event.kind,
                  style: tokens.mono(12, weight: FontWeight.w500),
                ),
              ),
              Text(status, style: tokens.mono(11, color: tone)),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            event.summary.isNotEmpty
                ? event.summary
                : event.title.isNotEmpty
                    ? event.title
                    : 'No summary',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: tokens.text,
                ),
          ),
          if (details.isNotEmpty) ...[
            const SizedBox(height: 3),
            Text(details.join('  ·  '),
                style: tokens.mono(11, color: tokens.muted)),
          ],
          if (event.deltaLabel.isNotEmpty) ...[
            const SizedBox(height: 3),
            Text(event.deltaLabel,
                style: tokens.mono(11,
                    color: tokens.accent, weight: FontWeight.w500)),
          ],
          if (event.preview.isNotEmpty) ...[
            const SizedBox(height: 7),
            SelectableText(
              event.preview,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    fontFamily: SonderTheme.mono,
                    height: 1.3,
                  ),
            ),
          ],
          if (event.previewState != 'available') ...[
            const SizedBox(height: 4),
            Text('preview: ${event.previewState}',
                style: Theme.of(context).textTheme.labelSmall),
          ] else if (event.displayPreview.redacted ||
              event.displayPreview.truncated) ...[
            const SizedBox(height: 4),
            Text(
                'preview: ${event.displayPreview.redacted ? 'redacted' : 'bounded'}'
                '${event.displayPreview.truncated ? ', truncated' : ''}',
                style: Theme.of(context).textTheme.labelSmall),
          ],
        ],
      ),
    );
  }

  static IconData _eventIcon(String category) {
    switch (category.toLowerCase()) {
      case 'model':
      case 'model_call':
        return Icons.psychology_outlined;
      case 'tool':
      case 'tool_call':
        return Icons.build_outlined;
      case 'file':
      case 'file_op':
      case 'file_change':
        return Icons.description_outlined;
      default:
        return Icons.bolt_outlined;
    }
  }

  static String _timestampLabel(DateTime? timestamp) {
    if (timestamp == null) return '--:--:--';
    String two(int value) => value.toString().padLeft(2, '0');
    final local = timestamp.toLocal();
    return '${two(local.hour)}:${two(local.minute)}:${two(local.second)}';
  }
}
