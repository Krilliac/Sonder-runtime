/// The detail pane for background work: a fleet with its agents, or an
/// autopilot run with its plan progress. Everything shown is the server's
/// snapshot; times are measured on the server's clock.
library;

import 'package:flutter/material.dart';

import '../background_work.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import '../ui/status_vocab.dart';
import '../workspace_ui.dart' show WorkspaceNotice, conversationWidth;
import 'agent_status.dart';
import 'list_pane.dart';

class _DetailFrame extends StatelessWidget {
  final String kindLabel;
  final String title;
  final String status;
  final List<String> facts;
  final Widget? action;
  final String? error;
  final List<Widget> sections;
  final bool narrow;

  const _DetailFrame({
    super.key,
    required this.kindLabel,
    required this.title,
    required this.status,
    required this.facts,
    required this.sections,
    required this.narrow,
    this.action,
    this.error,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final s = backgroundStatus(status);
    return ListView(
      padding: EdgeInsets.fromLTRB(
          narrow ? SonderSpace.lg : SonderSpace.xxl,
          narrow ? SonderSpace.lg : SonderSpace.xxl,
          narrow ? SonderSpace.lg : SonderSpace.xxl,
          SonderSpace.x4),
      children: [
        Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: conversationWidth),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(kindLabel,
                    style: text.labelMedium?.copyWith(color: tokens.muted)),
                const SizedBox(height: SonderSpace.xs),
                Semantics(
                  header: true,
                  child: Tooltip(
                    message: title,
                    child: Text(title,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: narrow
                            ? text.titleMedium
                                ?.copyWith(fontWeight: FontWeight.w600)
                            : text.titleLarge),
                  ),
                ),
                const SizedBox(height: SonderSpace.md),
                Wrap(
                  spacing: SonderSpace.md,
                  runSpacing: SonderSpace.sm,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Semantics(
                      liveRegion: true,
                      child: StatusPill(s.kind, word: s.word),
                    ),
                    if (facts.isNotEmpty)
                      Text(facts.join(' · '),
                          style: tokens.mono(12, color: tokens.muted)),
                  ],
                ),
                if (action != null) ...[
                  const SizedBox(height: SonderSpace.md),
                  Align(alignment: Alignment.centerLeft, child: action!),
                ],
                if (error != null) ...[
                  const SizedBox(height: SonderSpace.md),
                  WorkspaceNotice(kind: StatusKind.fail, title: error!),
                ],
                const SizedBox(height: SonderSpace.xxl),
                ...sections,
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _Legend extends StatelessWidget {
  final List<(StatusKind, String)> items;
  const _Legend(this.items);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return Wrap(
      spacing: SonderSpace.lg,
      runSpacing: SonderSpace.xs,
      children: [
        for (final (kind, words) in items)
          Row(mainAxisSize: MainAxisSize.min, children: [
            ExcludeSemantics(
              child: Text(kind.glyph,
                  style: tokens.mono(12,
                      color: kind.color(tokens), weight: FontWeight.w600)),
            ),
            const SizedBox(width: SonderSpace.xs),
            Text(words, style: text.bodySmall?.copyWith(color: tokens.text2)),
          ]),
      ],
    );
  }
}

SegmentedProgress fleetBar(BuildContext context, BackgroundFleet fleet,
    {double height = 4}) {
  final tokens = SonderTokens.of(context);
  final p = fleetProgress(fleet);
  return SegmentedProgress(
    total: p.total,
    height: height,
    semanticLabel: '${p.done} of ${p.total} done, ${p.running} running, '
        '${p.queued} queued${p.failed > 0 ? ', ${p.failed} failed' : ''}',
    parts: [
      ProgressPart(p.done, tokens.ok),
      ProgressPart(p.running, tokens.accent),
      ProgressPart(p.failed, tokens.danger),
      ProgressPart(p.cancelled, tokens.hairlineStrong),
    ],
  );
}

SegmentedProgress? autopilotBar(BuildContext context, BackgroundAutopilot run,
    {double height = 4}) {
  final tokens = SonderTokens.of(context);
  final c = run.taskCounts;
  final total = c['total'] ?? 0;
  if (total <= 0) return null;
  return SegmentedProgress(
    total: total,
    height: height,
    semanticLabel: '${c['done'] ?? 0} of $total tasks done, '
        '${c['running'] ?? 0} running',
    parts: [
      ProgressPart(c['done'] ?? 0, tokens.ok),
      ProgressPart(c['running'] ?? 0, tokens.accent),
      ProgressPart(c['failed'] ?? 0, tokens.danger),
      ProgressPart(c['cancelled'] ?? 0, tokens.hairlineStrong),
    ],
  );
}

/// A fleet: what it was asked, how far it got, and each of its agents.
class FleetDetailView extends StatelessWidget {
  final BackgroundFleet fleet;
  final double capturedAt;
  final bool narrow;
  final Future<bool> Function() confirmCancel;
  final Future<void> Function()? onCancel;
  final void Function(Object error, StackTrace stack)? onCancelError;
  final Future<void> Function(BackgroundChild child)? onCancelChild;
  final bool Function(String laneId) hasLane;
  final ValueChanged<String> onOpenLane;
  final String? error;

  const FleetDetailView({
    super.key,
    required this.fleet,
    required this.capturedAt,
    required this.narrow,
    required this.confirmCancel,
    required this.hasLane,
    required this.onOpenLane,
    this.onCancel,
    this.onCancelError,
    this.onCancelChild,
    this.error,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final p = fleetProgress(fleet);
    final elapsed = elapsedText(fleet.elapsedSeconds);
    final ago = updatedAgo(fleet.updatedTs, capturedAt);
    final children = fleet.children;
    final reserve = children.any((c) =>
        (c.cancelable && onCancelChild != null) ||
        (c.id.isNotEmpty && hasLane(c.id)));
    return _DetailFrame(
      key: ValueKey('fleet-detail-${fleet.id}'),
      kindLabel: 'Fleet',
      title: fleet.displayTask,
      status: fleet.status,
      narrow: narrow,
      error: error,
      facts: [
        '${fleet.requestedAgents} agent${fleet.requestedAgents == 1 ? '' : 's'}',
        if (fleet.workerSlots > 0)
          '${fleet.workerSlots} worker slot${fleet.workerSlots == 1 ? '' : 's'}',
        if (elapsed != null) elapsed,
        if (ago != null) 'updated $ago',
      ],
      action: fleet.cancelable && onCancel != null
          ? AsyncActionButton(
              key: const Key('cancel-fleet'),
              label: 'Cancel fleet',
              icon: Icons.stop_circle_outlined,
              doneLabel: 'Requested',
              confirm: confirmCancel,
              onPressed: onCancel,
              onError: onCancelError,
            )
          : null,
      sections: [
        SettingsSection(
          title: 'Progress',
          dividers: false,
          contentPadding: const EdgeInsets.all(SonderSpace.lg),
          children: [
            fleetBar(context, fleet, height: 8),
            const SizedBox(height: SonderSpace.md),
            _Legend([
              (StatusKind.ok, '${p.done} done'),
              (StatusKind.running, '${p.running} running'),
              (StatusKind.note, '${p.queued} queued'),
              if (p.failed > 0) (StatusKind.fail, '${p.failed} failed'),
              if (p.cancelled > 0)
                (StatusKind.skipped, '${p.cancelled} cancelled'),
            ]),
          ],
        ),
        if (children.isNotEmpty)
          SettingsSection(
            title: 'Agents',
            description: fleet.childrenTruncated
                ? 'Showing ${children.length}; the server holds more.'
                : null,
            children: [
              for (final child in children)
                _ChildRow(
                  key: ValueKey<String>('fleet-child-${child.id}'),
                  child: child,
                  narrow: narrow,
                  reserveAction: reserve,
                  onOpen: child.id.isNotEmpty && hasLane(child.id)
                      ? () => onOpenLane(child.id)
                      : null,
                  onCancel: child.cancelable && onCancelChild != null
                      ? () => onCancelChild!(child)
                      : null,
                ),
            ],
          ),
        if (fleet.preview.isNotEmpty)
          SettingsSection(
            title: 'Latest update',
            dividers: false,
            contentPadding: const EdgeInsets.all(SonderSpace.lg),
            children: [
              SelectionArea(
                child: Text(fleet.preview,
                    style: text.bodyMedium?.copyWith(color: tokens.text2)),
              ),
            ],
          ),
      ],
    );
  }
}

class _ChildRow extends StatelessWidget {
  final BackgroundChild child;
  final bool narrow;

  /// Keep the action slot even when this row has no action, so elapsed
  /// times line up down the list.
  final bool reserveAction;
  final VoidCallback? onOpen;
  final VoidCallback? onCancel;

  const _ChildRow({
    super.key,
    required this.child,
    required this.narrow,
    required this.reserveAction,
    this.onOpen,
    this.onCancel,
  });

  /// Width kept for the trailing action; the 48 dp button overlaps the
  /// row's own right padding.
  static const _slot = SonderSpace.x4;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final s = backgroundStatus(child.status);
    final elapsed = elapsedText(child.elapsedSeconds);
    final note = child.preview.isNotEmpty ? child.preview : child.activity;
    final slot = reserveAction || onCancel != null || onOpen != null;
    final row = ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 48),
      child: Padding(
        padding: EdgeInsets.fromLTRB(SonderSpace.lg, SonderSpace.md,
            slot ? _slot + SonderSpace.xs : SonderSpace.lg, SonderSpace.md),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (!narrow)
            Padding(
              padding: const EdgeInsets.only(top: SonderSpace.xxs),
              child: StatusMark(s.kind, word: s.word, size: 12, width: 104),
            ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (narrow) ...[
                  StatusMark(s.kind, word: s.word, size: 12),
                  const SizedBox(height: SonderSpace.xxs),
                ],
                Text(child.displayTask,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style:
                        text.bodyMedium?.copyWith(fontWeight: FontWeight.w500)),
                if (note.isNotEmpty) ...[
                  const SizedBox(height: SonderSpace.xxs),
                  Text(note,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: text.bodySmall?.copyWith(color: tokens.muted)),
                ],
              ],
            ),
          ),
          if (elapsed != null)
            Padding(
              padding: const EdgeInsets.only(
                  left: SonderSpace.md, top: SonderSpace.xxs),
              child: Text(elapsed, style: tokens.mono(12, color: tokens.muted)),
            ),
        ]),
      ),
    );
    // The action is centred on the task's first line, not the whole row.
    final Widget? action = onCancel != null
        ? IconButton(
            tooltip: 'Cancel child agent',
            icon: const Icon(Icons.stop_circle_outlined, size: 18),
            onPressed: onCancel,
          )
        : onOpen != null
            ? SizedBox(
                width: 48,
                height: 48,
                child: Icon(Icons.chevron_right, size: 18, color: tokens.muted),
              )
            : null;
    final body = Stack(children: [
      row,
      if (action != null)
        Positioned(top: 0, right: SonderSpace.xxs, child: action),
    ]);
    if (onOpen == null) {
      return Semantics(
        label: '${child.displayTask}. ${s.word}'
            '${elapsed == null ? '' : ', $elapsed'}',
        child: body,
      );
    }
    return HoverSurface(
      onTap: onOpen,
      borderRadius: BorderRadius.zero,
      semanticLabel: 'Open ${child.displayTask}. ${s.word}',
      child: body,
    );
  }
}

/// An autopilot run: its objective, plan progress and current task.
class AutopilotDetailView extends StatelessWidget {
  final BackgroundAutopilot run;
  final double capturedAt;
  final bool narrow;
  final Future<bool> Function() confirmCancel;
  final Future<void> Function()? onCancel;
  final void Function(Object error, StackTrace stack)? onCancelError;
  final String? error;

  const AutopilotDetailView({
    super.key,
    required this.run,
    required this.capturedAt,
    required this.narrow,
    required this.confirmCancel,
    this.onCancel,
    this.onCancelError,
    this.error,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final c = run.taskCounts;
    final total = c['total'] ?? 0;
    final elapsed = elapsedText(run.elapsedSeconds);
    final ago = updatedAgo(run.updatedTs, capturedAt);
    final bar = autopilotBar(context, run, height: 8);
    return _DetailFrame(
      key: ValueKey('autopilot-detail-${run.id}'),
      kindLabel: 'Autopilot',
      title: run.displayObjective,
      status: run.status,
      narrow: narrow,
      error: error,
      facts: [
        if (run.phase.isNotEmpty) run.phase,
        if (elapsed != null) elapsed,
        if (ago != null) 'updated $ago',
      ],
      action: run.cancelable && onCancel != null
          ? AsyncActionButton(
              key: const Key('cancel-autopilot'),
              label: 'Cancel autopilot',
              icon: Icons.stop_circle_outlined,
              doneLabel: 'Requested',
              confirm: confirmCancel,
              onPressed: onCancel,
              onError: onCancelError,
            )
          : null,
      sections: [
        if (bar != null)
          SettingsSection(
            title: 'Plan',
            dividers: false,
            contentPadding: const EdgeInsets.all(SonderSpace.lg),
            children: [
              bar,
              const SizedBox(height: SonderSpace.md),
              _Legend([
                (StatusKind.ok, '${c['done'] ?? 0} of $total done'),
                (StatusKind.running, '${c['running'] ?? 0} running'),
                (StatusKind.note, '${c['queued'] ?? 0} queued'),
                if ((c['failed'] ?? 0) > 0)
                  (StatusKind.fail, '${c['failed']} failed'),
              ]),
            ],
          ),
        if (run.currentTask.isNotEmpty)
          SettingsSection(
            title: 'Current task',
            dividers: false,
            contentPadding: const EdgeInsets.all(SonderSpace.lg),
            children: [
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                ExcludeSemantics(
                  child: Text(StatusKind.running.glyph,
                      style: tokens.mono(13,
                          color: tokens.accentText, weight: FontWeight.w600)),
                ),
                const SizedBox(width: SonderSpace.sm),
                Expanded(
                  child: SelectionArea(
                      child: Text(run.currentTask, style: text.bodyMedium)),
                ),
              ]),
            ],
          ),
        if (run.preview.isNotEmpty)
          SettingsSection(
            title: 'Latest update',
            dividers: false,
            contentPadding: const EdgeInsets.all(SonderSpace.lg),
            children: [
              SelectionArea(
                child: Text(run.preview,
                    style: text.bodyMedium?.copyWith(color: tokens.text2)),
              ),
            ],
          ),
      ],
    );
  }
}
