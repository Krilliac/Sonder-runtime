/// Work runs on Runtime (plan P1-4): "is my long job still going", from the
/// phone. Lists `GET /v1/work-runs` with status, age and budget used, and a
/// Stop per running row behind a confirmation.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'overview.dart';
import 'status_word.dart';

RuntimeStatus workRunStatus(WorkRun run) => switch (run.status) {
      'running' => RuntimeStatus.running,
      'returned' => RuntimeStatus.ok,
      'refused' => RuntimeStatus.refused,
      'cancelled' => RuntimeStatus.skipped,
      'failed' || 'budget_exceeded' || 'interrupted' => RuntimeStatus.fail,
      _ => RuntimeStatus.unknown,
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

class WorkRunsPanel extends StatelessWidget {
  final List<WorkRun>? runs;
  final Object? error;
  final bool loading;
  final DateTime? now;
  final VoidCallback? onRefresh;

  /// Called after the person confirmed. The screen reloads afterwards.
  final Future<void> Function(WorkRun run)? onStop;

  const WorkRunsPanel({
    super.key,
    required this.runs,
    this.error,
    this.loading = false,
    this.now,
    this.onRefresh,
    this.onStop,
  });

  Future<void> _confirmStop(BuildContext context, WorkRun run) async {
    final confirmed = await showDialog<bool>(
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
    if (confirmed == true) await onStop?.call(run);
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final clock = now ?? DateTime.now();
    final failure = error;
    if (failure is SonderException && failure.httpStatus == 403) {
      return const RuntimePanelNote(
        status: RuntimeStatus.skipped,
        word: 'n/a',
        text: 'Work runs need a developer or admin account.',
      );
    }
    final children = <Widget>[];
    if (failure != null) {
      children.add(RuntimePanelNote(
        status: RuntimeStatus.fail,
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
      children.add(RuntimePanelNote(
          status: RuntimeStatus.unknown,
          word: loading ? 'checking' : null,
          text: loading ? 'Loading work runs…' : 'Not loaded yet.'));
    } else if (list != null && list.isEmpty) {
      children.add(const RuntimePanelNote(
          status: RuntimeStatus.note, text: 'No work runs'));
    }
    for (final run in list ?? const <WorkRun>[]) {
      final detail = workRunDetail(run, clock);
      children.add(Semantics(
        container: true,
        label: 'Work run ${run.id}, ${workRunWord(run)}',
        child: Padding(
          key: Key('work-run-${run.id}'),
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(children: [
            RuntimeStatusWord(workRunStatus(run),
                word: workRunWord(run), width: 116),
            Expanded(
              child: Text.rich(
                TextSpan(children: [
                  TextSpan(text: run.shortId, style: tokens.mono(12.5)),
                  if (detail.isNotEmpty)
                    TextSpan(
                        text: '  $detail',
                        style: tokens.mono(12, color: tokens.text2)),
                ]),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            if (run.isRunning && !run.cancelRequested && onStop != null)
              TextButton(
                onPressed: () => _confirmStop(context, run),
                style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
                child: const Text('Stop…'),
              ),
          ]),
        ),
      ));
    }
    return Column(
      key: const Key('work-runs-panel'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: children,
    );
  }
}

/// A one-line `<glyph> <word>  text` note inside a Runtime detail panel.
class RuntimePanelNote extends StatelessWidget {
  final RuntimeStatus status;
  final String? word;
  final String text;
  final Widget? action;
  const RuntimePanelNote(
      {super.key,
      required this.status,
      required this.text,
      this.word,
      this.action});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(children: [
        RuntimeStatusWord(status, word: word, width: 116),
        Expanded(
            child: Text(text,
                style: Theme.of(context)
                    .textTheme
                    .bodyMedium
                    ?.copyWith(color: tokens.text2))),
        if (action != null) action!,
      ]),
    );
  }
}
