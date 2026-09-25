/// "What is running now": the one health summary at the top of Runtime
/// (plan P1-5, §2.4). Every row is `<glyph> <word>  <label>  <value>`, so
/// colour never carries status alone; off-by-design reads `– off`, not red.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'runtime_data.dart';
import 'status_word.dart';

/// One overview row, computed from the snapshots the screen already holds.
class OverviewRow {
  final RuntimeStatus status;
  final String? word;
  final String label;
  final String value;
  final String? actionLabel;
  final VoidCallback? onAction;

  const OverviewRow({
    required this.status,
    required this.label,
    required this.value,
    this.word,
    this.actionLabel,
    this.onAction,
  });
}

/// One line of the recent-activity list.
class ActivityLine {
  final String time;
  final RuntimeStatus status;
  final String word;
  final String text;
  const ActivityLine(this.time, this.status, this.word, this.text);
}

String _two(int value) => value.toString().padLeft(2, '0');

String clockLabel(DateTime time) => '${_two(time.hour)}:${_two(time.minute)}';

/// `4m`, `1h 12m`, `38s`: the REPL's compact duration words.
String compactDuration(Duration duration) {
  final seconds = duration.inSeconds;
  if (seconds < 60) return '${seconds < 0 ? 0 : seconds}s';
  final minutes = duration.inMinutes;
  if (minutes < 60) return '${minutes}m';
  final hours = duration.inHours;
  final rest = minutes - hours * 60;
  return rest == 0 ? '${hours}h' : '${hours}h ${rest}m';
}

String serverLabel(String serverUrl) {
  final uri = Uri.tryParse(serverUrl.trim());
  if (uri == null || uri.host.isEmpty) return serverUrl.trim();
  return uri.hasPort ? '${uri.host}:${uri.port}' : uri.host;
}

RuntimeStatus _eventStatus(ExecutionFeedEvent event) {
  if (event.ok == true) return RuntimeStatus.ok;
  if (event.ok == false) return RuntimeStatus.fail;
  final state = '${event.phase} ${event.responseStatus}'.toLowerCase();
  if (state.contains('refus')) return RuntimeStatus.refused;
  if (state.contains('fail') || state.contains('error')) {
    return RuntimeStatus.fail;
  }
  if (state.contains('cancel')) return RuntimeStatus.skipped;
  if (state.contains('run') ||
      state.contains('start') ||
      state.contains('active')) {
    return RuntimeStatus.running;
  }
  if (state.contains('complet') ||
      state.contains('done') ||
      state.contains('finish') ||
      state.contains('succe')) {
    return RuntimeStatus.ok;
  }
  return RuntimeStatus.note;
}

String _eventWord(RuntimeStatus status) => switch (status) {
      RuntimeStatus.ok => 'done',
      RuntimeStatus.skipped => 'cancelled',
      _ => status.word,
    };

/// The feed's newest [limit] events, newest first.
List<ActivityLine> recentActivity(ExecutionFeed? feed, {int limit = 5}) {
  if (feed == null) return const [];
  final events = [...feed.events]..sort((a, b) => b.seq.compareTo(a.seq));
  return [
    for (final event in events.take(limit))
      () {
        final status = _eventStatus(event);
        final parts = <String>[
          event.summary.isNotEmpty ? event.summary : event.kind,
          if (event.elapsedMs > 0)
            '${(event.elapsedMs / 1000).toStringAsFixed(1)}s',
        ];
        return ActivityLine(
          event.timestamp == null ? '--:--' : clockLabel(event.timestamp!),
          status,
          _eventWord(status),
          parts.join(' · '),
        );
      }(),
  ];
}

/// Builds the overview rows. Pure, so the table is testable without pumping.
List<OverviewRow> overviewRows({
  required String serverUrl,
  required SystemInfo? info,
  required bool offline,
  bool loading = false,
  List<WorkRun>? workRuns,
  Object? workRunsError,
  ApprovalsPage? approvals,
  DateTime? now,
  VoidCallback? onOpenWorkRuns,
  VoidCallback? onReviewApprovals,
}) {
  final host = serverLabel(serverUrl);
  final clock = now ?? DateTime.now();
  final rows = <OverviewRow>[];

  if (offline) {
    rows.add(OverviewRow(
        status: RuntimeStatus.fail,
        label: 'Server',
        value: "Can't reach $host"));
  } else if (info == null) {
    rows.add(OverviewRow(
        status: RuntimeStatus.unknown,
        word: loading ? 'checking' : null,
        label: 'Server',
        value: loading ? 'Connecting to $host…' : 'No status from $host yet'));
  } else {
    final summary = info.status.split('\n').first.trim();
    rows.add(OverviewRow(
        status: RuntimeStatus.ok,
        label: 'Server',
        value: [host, if (summary.isNotEmpty) summary].join(' · ')));
  }

  if (info != null) {
    final models = info.models.map((m) => m.id).where((id) => id.isNotEmpty);
    final caps = info.operationalCapabilities;
    final pool = caps != null && caps.workerCount > 0
        ? 'pool ${caps.healthyWorkerCount} of ${caps.workerCount} workers'
        : '';
    final modelText = models.isEmpty
        ? 'none reported'
        : models.length <= 2
            ? models.join(', ')
            : '${models.take(2).join(', ')} +${models.length - 2}';
    final degraded = caps != null &&
        caps.workerCount > 0 &&
        caps.healthyWorkerCount < caps.workerCount;
    rows.add(OverviewRow(
        status: models.isEmpty
            ? RuntimeStatus.warn
            : degraded
                ? RuntimeStatus.warn
                : RuntimeStatus.ok,
        label: 'Models',
        value: [modelText, if (pool.isNotEmpty) pool].join(' · ')));
  }

  if (approvals != null) {
    if (!approvals.supported) {
      rows.add(const OverviewRow(
          status: RuntimeStatus.skipped,
          word: 'n/a',
          label: 'Approvals',
          value: 'approve from the console (/approvals)'));
    } else if (approvals.pending.isNotEmpty) {
      final n = approvals.pending.length;
      rows.add(OverviewRow(
          status: RuntimeStatus.warn,
          label: 'Approvals',
          value: '$n call${n == 1 ? '' : 's'} waiting',
          actionLabel: 'Review',
          onAction: onReviewApprovals));
    } else {
      rows.add(OverviewRow(
          status: RuntimeStatus.ok,
          label: 'Approvals',
          value: approvals.open.isEmpty
              ? 'none waiting'
              : 'none waiting · ${approvals.open.length} open',
          actionLabel: approvals.open.isEmpty ? null : 'Review',
          onAction: approvals.open.isEmpty ? null : onReviewApprovals));
    }
  }

  if (workRunsError is SonderException && workRunsError.httpStatus == 403) {
    rows.add(const OverviewRow(
        status: RuntimeStatus.skipped,
        word: 'n/a',
        label: 'Work runs',
        value: 'need a developer or admin account'));
  } else if (workRuns != null) {
    final running = workRuns.where((run) => run.running).toList();
    if (running.isEmpty) {
      rows.add(OverviewRow(
          status: RuntimeStatus.note,
          label: 'Work runs',
          value: workRuns.isEmpty
              ? 'No work runs'
              : 'none running · ${workRuns.length} recent',
          actionLabel: workRuns.isEmpty ? null : 'Open',
          onAction: workRuns.isEmpty ? null : onOpenWorkRuns));
    } else {
      final first = running.first;
      rows.add(OverviewRow(
          status: RuntimeStatus.running,
          label: 'Work runs',
          value: '${running.length} running · ${first.shortId} '
              '${compactDuration(first.age(clock))}',
          actionLabel: 'Open',
          onAction: onOpenWorkRuns));
    }
  } else if (workRunsError != null) {
    rows.add(const OverviewRow(
        status: RuntimeStatus.unknown,
        label: 'Work runs',
        value: 'could not load'));
  }

  if (info != null) {
    final autopilot = info.autopilot;
    final active = autopilot?.activeRuns ?? 0;
    final resumable = autopilot?.resumableRuns ?? 0;
    rows.add(OverviewRow(
        status: active > 0
            ? RuntimeStatus.running
            : resumable > 0
                ? RuntimeStatus.warn
                : RuntimeStatus.skipped,
        label: 'Autopilot',
        value: active > 0
            ? '$active running'
            : resumable > 0
                ? '$resumable paused, resumable'
                : 'off'));

    final agents = info.agents;
    final activeAgents = agents?.activeAgents ?? 0;
    final interrupted = agents?.interruptedAgents ?? 0;
    rows.add(OverviewRow(
        status: activeAgents > 0
            ? RuntimeStatus.running
            : interrupted > 0
                ? RuntimeStatus.warn
                : RuntimeStatus.note,
        label: 'Agents',
        value: activeAgents > 0
            ? '$activeAgents running'
            : interrupted > 0
                ? '$interrupted interrupted'
                : 'none running'));
  }
  return rows;
}

/// The Overview block: rows, then the last five feed events.
class RuntimeOverview extends StatelessWidget {
  final List<OverviewRow> rows;
  final List<ActivityLine> activity;

  /// When offline, the last loaded values stay, dimmed, "as of 12:40".
  final DateTime? staleSince;
  final VoidCallback? onRetry;
  final VoidCallback? onAllActivity;

  const RuntimeOverview({
    super.key,
    required this.rows,
    this.activity = const [],
    this.staleSince,
    this.onRetry,
    this.onAllActivity,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(builder: (context, constraints) {
      final narrow = constraints.maxWidth < 560;
      final body = Column(
        key: const Key('runtime-overview'),
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final row in rows) _OverviewRowView(row: row, narrow: narrow),
          if (staleSince != null)
            Padding(
              padding: const EdgeInsets.only(top: 4, bottom: 4),
              child: Wrap(
                crossAxisAlignment: WrapCrossAlignment.center,
                spacing: 8,
                children: [
                  Text('as of ${clockLabel(staleSince!)}',
                      style: tokens.mono(12, color: tokens.muted)),
                  if (onRetry != null)
                    TextButton(onPressed: onRetry, child: const Text('Retry')),
                ],
              ),
            ),
          const SizedBox(height: 16),
          Row(children: [
            Expanded(child: Text('Recent activity', style: text.labelSmall)),
            if (onAllActivity != null && activity.isNotEmpty)
              TextButton(onPressed: onAllActivity, child: const Text('All')),
          ]),
          const SizedBox(height: 4),
          if (activity.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Row(children: [
                const RuntimeStatusWord(RuntimeStatus.note),
                Expanded(
                    child: Text('No recent activity',
                        style: text.bodyMedium?.copyWith(color: tokens.text2))),
              ]),
            ),
          for (final line in activity)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                      width: 48,
                      child: Text(line.time,
                          style: tokens.mono(12, color: tokens.muted))),
                  RuntimeStatusWord(line.status,
                      word: line.word, width: narrow ? 92 : 110),
                  Expanded(
                    child: Text(line.text,
                        maxLines: narrow ? 2 : 1,
                        overflow: TextOverflow.ellipsis,
                        style: tokens.mono(12, color: tokens.text2)),
                  ),
                ],
              ),
            ),
        ],
      );
      if (staleSince == null) return body;
      return Opacity(opacity: 0.62, child: body);
    });
  }
}

class _OverviewRowView extends StatelessWidget {
  final OverviewRow row;
  final bool narrow;
  const _OverviewRowView({required this.row, required this.narrow});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final labelStyle = Theme.of(context).textTheme.labelLarge;
    final value = Text(row.value,
        style: tokens.mono(12.5, color: tokens.text),
        maxLines: narrow ? 3 : 2,
        overflow: TextOverflow.ellipsis);
    final action = row.actionLabel == null
        ? null
        : TextButton(
            onPressed: row.onAction,
            style: TextButton.styleFrom(
                minimumSize: const Size(48, 48),
                tapTargetSize: MaterialTapTargetSize.padded),
            child: Text(row.actionLabel!),
          );
    final status = RuntimeStatusWord(row.status, word: row.word);
    return Semantics(
      container: true,
      label: '${row.word ?? row.status.word}, ${row.label}',
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 36),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: narrow
              ? Row(
                  crossAxisAlignment: CrossAxisAlignment.center,
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(children: [
                            status,
                            Flexible(child: Text(row.label, style: labelStyle)),
                          ]),
                          Padding(
                              padding: const EdgeInsets.only(left: 92, top: 2),
                              child: value),
                        ],
                      ),
                    ),
                    if (action != null) action,
                  ],
                )
              : Row(
                  crossAxisAlignment: CrossAxisAlignment.center,
                  children: [
                    status,
                    SizedBox(
                        width: 110, child: Text(row.label, style: labelStyle)),
                    Expanded(child: value),
                    if (action != null) action,
                  ],
                ),
        ),
      ),
    );
  }
}
