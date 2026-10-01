part of '../runtime_screen.dart';

/// A checklist item status as a vocabulary kind.
StatusKind _checklistKind(String status) => switch (status) {
      'done' => StatusKind.ok,
      'in_progress' => StatusKind.running,
      'blocked' => StatusKind.fail,
      _ => StatusKind.note,
    };

/// The latest workbench response: what it did, its checklist, and the exact
/// actions with their evidence.
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
    final text = Theme.of(context).textTheme;
    final recentActions = response.actions.length <= 8
        ? response.actions
        : response.actions.sublist(response.actions.length - 8);
    final completed =
        response.checklist.where((item) => item.status == 'done').length;
    final status = response.status.isEmpty ? 'unknown' : response.status;
    final kind = switch (status) {
      'error' || 'failed' => StatusKind.fail,
      'running' => StatusKind.running,
      'unknown' => StatusKind.unknown,
      _ => StatusKind.ok,
    };
    return SettingsSection(
      key: const Key('workbench-activity'),
      title: 'Workbench activity',
      description: response.label.isEmpty ? null : response.label,
      trailing: StatusPill(kind, word: status),
      children: [
        RuntimeStatStrip([
          RuntimeStat('Actions', '${response.toolCalls}'),
          RuntimeStat('Model calls', '${response.modelCalls}'),
          RuntimeStat('All actions', '$totalToolCalls'),
          RuntimeStat('Time', '${response.elapsedMs} ms'),
        ]),
        if (response.resultSummary.isNotEmpty)
          RuntimeCardBody(
            child: Text(response.resultSummary,
                style: text.bodyMedium?.copyWith(fontWeight: FontWeight.w600)),
          ),
        if (response.checklist.isNotEmpty) ...[
          RuntimeCardBody(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.lg, SonderSpace.md, SonderSpace.lg, 0),
            child: Row(children: [
              Expanded(
                child: Text(
                    response.checklistTitle.isEmpty
                        ? 'Checklist'
                        : response.checklistTitle,
                    style: text.titleSmall),
              ),
              Text('$completed/${response.checklist.length}',
                  style: text.bodySmall),
            ]),
          ),
          for (final item in response.checklist)
            RuntimeRow(
              dense: true,
              kind: _checklistKind(item.status),
              word: _humanStatus(item.status),
              title: RuntimeRowTitle(item.title),
            ),
        ],
        RuntimeCardBody(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.lg, SonderSpace.md, SonderSpace.lg, 0),
          child: Text('Exact actions', style: text.titleSmall),
        ),
        if (recentActions.isEmpty)
          const RuntimeEmptyRow('No tool actions recorded yet.',
              icon: Icons.build_outlined)
        else
          for (final action in recentActions)
            RuntimeRow(
              kind: action.ok ? StatusKind.ok : StatusKind.fail,
              title: RuntimeRowTitle(action.title),
              subtitle: action.evidence.isEmpty
                  ? null
                  : RuntimeRowDetail(action.evidence, mono: true, maxLines: 4),
              actions: [
                Text('+${action.elapsedMs}ms',
                    style: SonderTokens.of(context)
                        .mono(12, color: SonderTokens.of(context).muted)),
              ],
            ),
      ],
    );
  }
}

/// The runtime's bounded live execution feed, newest first: one row per
/// event (time, status, what happened, its details and preview). The feed's
/// own bookkeeping (window, drops, gaps) is one quiet line.
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
    final limit = maxVisible.clamp(1, 20).toInt();
    final feed = this.feed;
    final events = feed?.events ?? const <ExecutionFeedEvent>[];
    final visible =
        events.length <= limit ? events : events.sublist(events.length - limit);
    final Widget? placeholder;
    if (offline) {
      placeholder = const _FeedPlaceholder(
        icon: Icons.cloud_off_outlined,
        title: 'Offline',
        message: 'Live execution events are unavailable while disconnected.',
      );
    } else if (feed == null || !feed.known) {
      placeholder = const _FeedPlaceholder(
        icon: Icons.help_outline,
        title: 'Unavailable',
        message: 'This runtime does not publish an execution feed.',
      );
    } else if (feed.error.isNotEmpty) {
      placeholder = const _FeedPlaceholder(
        icon: Icons.error_outline,
        title: 'Unavailable',
        message: 'The runtime reported an execution feed error.',
      );
    } else if (visible.isEmpty) {
      placeholder = const _FeedPlaceholder(
        icon: Icons.hourglass_empty,
        title: 'Unknown',
        message: 'No execution events are available yet.',
      );
    } else {
      placeholder = null;
    }
    final meta = placeholder != null || feed == null
        ? const <String>[]
        : [
            'schema ${feed.schemaVersion}',
            feed.activeResponses == null
                ? 'active unknown'
                : '${feed.activeResponses} active',
            if (feed.oldestSeq != null && feed.nextSeq != null)
              'window ${feed.oldestSeq} → ${feed.nextSeq}',
            if ((feed.droppedEvents ?? 0) > 0) '${feed.droppedEvents} dropped',
            if (feed.detailsDisabled) 'Details disabled',
            if (feed.hasGap) 'Sequence gap',
            if (feed.truncated) 'History truncated',
            if (feed.redactionApplied) 'Redaction applied',
          ];
    return SettingsSection(
      key: const Key('live-execution'),
      title: 'Live execution',
      description: placeholder == null
          ? '${visible.length}/${events.length} events'
          : null,
      children: [
        if (placeholder != null) placeholder,
        if (meta.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.lg, vertical: SonderSpace.sm),
            child: Wrap(
              spacing: SonderSpace.xs,
              runSpacing: SonderSpace.xs,
              children: [for (final tag in meta) _FeedTag(tag)],
            ),
          ),
        if (placeholder == null)
          for (final event in visible.reversed)
            _ExecutionEventRow(event: event),
      ],
    );
  }
}

/// A small quiet tag for the feed's bookkeeping ("window 0 → 16").
class _FeedTag extends StatelessWidget {
  final String text;
  const _FeedTag(this.text);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.sm, vertical: SonderSpace.xxs),
      decoration: BoxDecoration(
        color: tokens.raised,
        borderRadius: BorderRadius.circular(SonderRadius.pill),
      ),
      child: Text(text, style: tokens.mono(11.5, color: tokens.text2)),
    );
  }
}

class _FeedPlaceholder extends StatelessWidget {
  final IconData icon;
  final String title;
  final String message;

  const _FeedPlaceholder({
    required this.icon,
    required this.title,
    required this.message,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Container(
      constraints: const BoxConstraints(minHeight: 56),
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.lg, vertical: SonderSpace.md),
      child: Row(children: [
        Icon(icon, size: 18, color: tokens.muted),
        const SizedBox(width: SonderSpace.md),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title,
                  style:
                      text.bodyMedium?.copyWith(fontWeight: FontWeight.w500)),
              const SizedBox(height: SonderSpace.xxs),
              Text(message, style: text.bodySmall),
            ],
          ),
        ),
      ]),
    );
  }
}

class _ExecutionEventRow extends StatelessWidget {
  final ExecutionFeedEvent event;

  const _ExecutionEventRow({required this.event});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final status = executionEventStatus(event);
    final title = event.summary.isNotEmpty
        ? event.summary
        : event.title.isNotEmpty
            ? event.title
            : 'No summary';
    final details = <String>[
      event.kind,
      if (event.seq > 0) '#${event.seq}',
      if (event.elapsedMs > 0) '${event.elapsedMs}ms',
      if (event.model.isNotEmpty) 'model ${event.model}',
      if (event.promptChars > 0) 'prompt ${event.promptChars} chars',
      if (event.historyMessages > 0) '${event.historyMessages} history',
      if (event.tokensIn != 0 || event.tokensOut != 0)
        'tokens ${event.tokensIn}/${event.tokensOut}',
      if (event.tool.isNotEmpty) 'tool ${event.tool}',
      if (event.fileOperation.isNotEmpty) 'file ${event.fileOperation}',
      if (event.path.isNotEmpty && !title.contains(event.path)) event.path,
      if (event.bytes > 0) '${event.bytes} bytes',
      if (event.dryRun) 'dry run',
    ];
    // A summary-only event previews its own summary: say it once.
    final preview = event.preview == title ? '' : event.preview;
    // An event without a preview is the common case; only say so when the
    // runtime turned previews off, or bounded what it shows.
    final shown = event.displayPreview;
    final String? previewNote;
    if (event.previewState == 'disabled') {
      previewNote = 'preview: disabled';
    } else if (event.previewState == 'available' &&
        (shown.redacted || shown.truncated)) {
      previewNote = 'preview: ${shown.redacted ? 'redacted' : 'bounded'}'
          '${shown.truncated ? ', truncated' : ''}';
    } else {
      previewNote = null;
    }
    return RuntimeRow(
      markWidth: 172,
      leading: Row(mainAxisSize: MainAxisSize.min, children: [
        SizedBox(
          width: 68,
          child: Text(_timestampLabel(event.timestamp),
              style: tokens.mono(12, color: tokens.muted)),
        ),
        Flexible(
          child: StatusMark(status, word: executionEventWord(status), size: 12),
        ),
      ]),
      title: RuntimeRowTitle(title),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          RuntimeRowDetail(details.join(' · '), mono: true, maxLines: 2),
          if (event.deltaLabel.isNotEmpty)
            Text(event.deltaLabel,
                style: tokens.mono(12,
                    color: tokens.accentText, weight: FontWeight.w500)),
          if (preview.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.xs),
            SelectableText(
              preview,
              minLines: 1,
              maxLines: 4,
              style: tokens.mono(12, color: tokens.text2),
            ),
          ],
          if (previewNote != null)
            Text(previewNote, style: Theme.of(context).textTheme.labelSmall),
        ],
      ),
    );
  }

  static String _timestampLabel(DateTime? timestamp) {
    if (timestamp == null) return '--:--:--';
    String two(int value) => value.toString().padLeft(2, '0');
    final local = timestamp.toLocal();
    return '${two(local.hour)}:${two(local.minute)}:${two(local.second)}';
  }
}
