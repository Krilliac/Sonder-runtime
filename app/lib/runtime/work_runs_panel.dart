/// Work runs on Runtime (plan P1-4): "is my long job still going", from the
/// phone. Lists `GET /v1/work-runs` with status, age and budget used, and a
/// Stop per running row behind a confirmation. Each row carries its own
/// progress and outcome; nothing else on the page waits for it.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../ui/kit.dart';
import 'overview.dart';
import 'runtime_rows.dart';
import 'status_word.dart';

export 'runtime_rows.dart' show RuntimePanelNote;

StatusKind workRunStatus(WorkRun run) => switch (run.status) {
      'running' => StatusKind.running,
      'returned' => StatusKind.ok,
      'refused' => StatusKind.refused,
      'cancelled' => StatusKind.skipped,
      'failed' || 'budget_exceeded' || 'interrupted' => StatusKind.fail,
      _ => StatusKind.unknown,
    };

String workRunWord(WorkRun run) => switch (run.status) {
      'running' => run.cancelRequested ? 'stopping' : 'working',
      'returned' => 'done',
      'refused' => 'refused',
      'cancelled' => 'cancelled',
      'budget_exceeded' => 'over budget',
      'interrupted' => 'interrupted',
      'failed' => 'error',
      _ => 'unknown',
    };

/// `4m of 30m budget` for a running row, `12m ago` once it settled.
String workRunDetail(WorkRun run, DateTime now) {
  final budget = run.budget;
  if (run.isRunning) {
    final used = compactDuration((run.elapsed(now) ?? Duration.zero));
    return budget == null ? used : '$used of ${compactDuration(budget)} budget';
  }
  final settled = run.updatedAt ?? run.createdAt;
  return settled == null
      ? ''
      : '${compactDuration(now.difference(settled))} ago';
}

/// `1 running · 3 recent`, or null before the first read.
String? workRunsSummary(List<WorkRun>? runs) {
  if (runs == null) return null;
  if (runs.isEmpty) return 'None yet';
  final running = runs.where((run) => run.isRunning).length;
  return [
    if (running > 0) '$running running',
    '${runs.length} recent',
  ].join(' · ');
}

/// The Work runs card: one row per run, Stop behind a confirmation, and the
/// result of a Stop under its own row.
class WorkRunsPanel extends StatelessWidget {
  final List<WorkRun>? runs;
  final Object? error;
  final bool loading;
  final DateTime? now;
  final VoidCallback? onRefresh;

  /// Called after the person confirmed. The screen reloads afterwards.
  final Future<void> Function(WorkRun run)? onStop;

  /// Runs whose Stop request is in flight: their button reads "Stopping…"
  /// and takes no second press.
  final Set<String> stopping;

  /// The outcome of the last Stop per run id.
  final Map<String, ActionOutcome> outcomes;
  final ValueChanged<String>? onDismissOutcome;

  const WorkRunsPanel({
    super.key,
    required this.runs,
    this.error,
    this.loading = false,
    this.now,
    this.onRefresh,
    this.onStop,
    this.stopping = const {},
    this.outcomes = const {},
    this.onDismissOutcome,
  });

  Future<bool?> _confirmStop(BuildContext context, WorkRun run) =>
      showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text('Stop work run ${run.shortId}?'),
          content: const Text(
            'File changes, programs and destructive tools stop at their next '
            'step. A model step already running finishes first.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Keep running'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Stop run'),
            ),
          ],
        ),
      );

  @override
  Widget build(BuildContext context) {
    final clock = now ?? DateTime.now();
    final failure = error;
    final rows = <Widget>[];
    if (failure is SonderException && failure.httpStatus == 403) {
      rows.add(const RuntimePanelNote(
        status: StatusKind.skipped,
        word: 'n/a',
        text: 'Work runs need a developer or admin account.',
      ));
    } else {
      if (failure != null) {
        rows.add(RuntimePanelNote(
          status: StatusKind.fail,
          text: failure is SonderException
              ? failure.message
              : 'Could not load work runs.',
          action: onRefresh == null
              ? null
              : TextButton(onPressed: onRefresh, child: const Text('Retry')),
        ));
      }
      final list = runs;
      if (list == null && failure == null) {
        rows.add(loading
            ? const SkeletonRows(rows: 2, semanticLabel: 'Loading work runs')
            : const RuntimePanelNote(
                status: StatusKind.unknown, text: 'Not loaded yet.'));
      } else if (list != null && list.isEmpty) {
        rows.add(const RuntimeEmptyRow('No work runs',
            icon: Icons.pending_actions_outlined));
      }
      for (final run in list ?? const <WorkRun>[]) {
        rows.add(_row(context, run, clock));
      }
    }
    return SettingsSection(
      key: const Key('work-runs-panel'),
      title: 'Work runs',
      description: workRunsSummary(runs),
      trailing: onRefresh == null
          ? null
          : IconButton(
              tooltip: 'Refresh work runs',
              onPressed: loading ? null : onRefresh,
              icon: const Icon(Icons.refresh, size: 18),
            ),
      children: rows,
    );
  }

  Widget _row(BuildContext context, WorkRun run, DateTime clock) {
    final detail = workRunDetail(run, clock);
    final outcome = outcomes[run.id];
    final canStop = run.isRunning && !run.cancelRequested && onStop != null;
    final busy = stopping.contains(run.id);
    return RuntimeRow(
      key: Key('work-run-${run.id}'),
      kind: workRunStatus(run),
      word: workRunWord(run),
      semanticLabel: 'Work run ${run.id}, ${workRunWord(run)}'
          '${detail.isEmpty ? '' : ', $detail'}',
      title: RuntimeRowTitle(run.shortId, mono: true, maxLines: 1),
      subtitle: detail.isEmpty ? null : RuntimeRowDetail(detail),
      actions: [
        if (canStop || busy)
          AsyncActionButton(
            label: 'Stop…',
            busyLabel: 'Stopping…',
            doneLabel: null,
            style: ActionButtonStyle.text,
            busy: busy,
            confirm: () => _confirmStop(context, run),
            onPressed: () async => onStop?.call(run),
            onError: (_, __) {},
          ),
      ],
      below: outcome == null
          ? null
          : OutcomeView(outcome,
              onDismiss: onDismissOutcome == null
                  ? null
                  : () => onDismissOutcome!(run.id)),
    );
  }
}
