/// Transcript pieces in Chat's glyph-gutter language: `❯` for what was sent
/// to the agent, `◈` for what it said, `▸` for its tool calls, and quiet
/// markers where its run started, stopped or reported.
library;

import 'package:flutter/material.dart';

import '../agent_lanes.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_row.dart';
import '../ui/status_vocab.dart';
import '../workspace_ui.dart' show ConversationContent;
import 'agent_status.dart';
import 'transcript_model.dart';

/// Width of the glyph gutter and the gap after it, as in Chat.
const transcriptGutter = 24.0;
const transcriptGutterGap = 14.0;

class _Gutter extends StatelessWidget {
  final String glyph;
  final Color color;
  final double size;

  const _Gutter(this.glyph, this.color, {this.size = 14});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return SizedBox(
      width: transcriptGutter,
      child: Padding(
        padding: const EdgeInsets.only(top: 2),
        child: ExcludeSemantics(
          child: Text(glyph,
              textAlign: TextAlign.center,
              style: tokens.mono(size, color: color, weight: FontWeight.w600)),
        ),
      ),
    );
  }
}

/// One message: who sent it, the Markdown, and its delivery state.
class AgentTurnView extends StatelessWidget {
  final MessageItem item;

  /// The lane is paused, so a queued message waits for Resume.
  final bool needsResume;

  const AgentTurnView(
      {super.key, required this.item, this.needsResume = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final fromAgent = item.author == 'model' || item.author == 'child';
    final glyph = fromAgent ? StatusKind.running.glyph : '❯';
    final color = item.author == 'parent' ? tokens.text2 : tokens.accent;
    final (String? delivery, StatusKind? deliveryKind) =
        switch (item.deliveryState) {
      'queued' => needsResume
          ? ('Queued · choose Resume to continue', StatusKind.warn)
          : ('Queued for the next turn', StatusKind.note),
      'accepted' => ('Received by agent', StatusKind.ok),
      _ => (null, null),
    };
    return Semantics(
      label: item.authorLabel,
      child: Padding(
        padding: const EdgeInsets.symmetric(
            vertical: SonderSpace.sm + SonderSpace.xxs),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _Gutter(glyph, color),
            const SizedBox(width: transcriptGutterGap),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(item.authorLabel,
                      style: text.labelMedium?.copyWith(color: tokens.text2)),
                  const SizedBox(height: SonderSpace.xs),
                  ConversationContent(
                      content: item.content, fullWidthCode: true),
                  if (delivery != null) ...[
                    const SizedBox(height: SonderSpace.sm),
                    Row(children: [
                      ExcludeSemantics(
                        child: Text(deliveryKind!.glyph,
                            style: tokens.mono(11.5,
                                color: deliveryKind.color(tokens),
                                weight: FontWeight.w600)),
                      ),
                      const SizedBox(width: SonderSpace.sm),
                      Flexible(
                        child: Text(delivery,
                            style: text.bodySmall?.copyWith(
                                color: deliveryKind == StatusKind.warn
                                    ? tokens.warn
                                    : tokens.muted)),
                      ),
                    ]),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

({StatusKind kind, String word}) toolStatus(ToolState state) => switch (state) {
      ToolState.requested => (kind: StatusKind.running, word: 'requested'),
      ToolState.done => (kind: StatusKind.ok, word: 'done'),
      ToolState.failed => (kind: StatusKind.fail, word: 'failed'),
      ToolState.received => (kind: StatusKind.note, word: 'result received'),
      ToolState.noResult => (kind: StatusKind.unknown, word: 'no result'),
      ToolState.rejected => (kind: StatusKind.refused, word: 'rejected'),
    };

String _durationText(Duration d) {
  if (d.inMilliseconds < 1000) return '${d.inMilliseconds} ms';
  if (d.inSeconds < 60) {
    return '${(d.inMilliseconds / 1000).toStringAsFixed(1)}s';
  }
  return compactSpan(d);
}

/// The text a tool output shows as: strings as sent, structures as JSON.
String toolOutputText(Object? output) {
  if (output == null) return '';
  if (output is String) return output;
  if (output is Map || output is List) {
    return StructuredFields.prettyJson(output);
  }
  return '$output';
}

/// A tool call as a collapsible card: the summary line names the tool, its
/// salient argument, its status and duration (when the server measured
/// one); the body shows the arguments as fields and the output as raw text.
class ToolCallCard extends StatelessWidget {
  final ToolItem item;
  final bool expanded;
  final VoidCallback onToggle;

  const ToolCallCard({
    super.key,
    required this.item,
    required this.expanded,
    required this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final status = toolStatus(item.state);
    final salient = item.salientArgument;
    final duration =
        item.duration == null ? null : _durationText(item.duration!);
    final radius = BorderRadius.circular(SonderRadius.row);

    Widget summary(double width) {
      final stacked = width < 380;
      final name = Text(item.name,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style:
              tokens.mono(12.5, color: tokens.text, weight: FontWeight.w600));
      final salientText = Text(salient,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: tokens.mono(12, color: tokens.muted));
      // Bounded, so a long status word at large text sizes ellipsizes
      // instead of overflowing.
      final trailing = ConstrainedBox(
        constraints: BoxConstraints(maxWidth: width * 0.6),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Flexible(
              child: StatusMark(status.kind, word: status.word, size: 11.5)),
          if (duration != null) ...[
            const SizedBox(width: SonderSpace.sm),
            Text(duration, style: tokens.mono(11.5, color: tokens.muted)),
          ],
          const SizedBox(width: SonderSpace.xs),
          AnimatedRotation(
            turns: expanded ? 0.5 : 0,
            duration: SonderMotion.of(context, SonderMotion.fast),
            curve: SonderMotion.standard,
            child: Icon(Icons.expand_more, size: 18, color: tokens.text2),
          ),
        ]),
      );
      if (stacked) {
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(child: name),
              const SizedBox(width: SonderSpace.sm),
              trailing,
            ]),
            if (salient.isNotEmpty) ...[
              const SizedBox(height: SonderSpace.xxs),
              salientText,
            ],
          ],
        );
      }
      // The name keeps its natural width up to two fifths of the line; the
      // salient argument takes the rest, so status always sits at the end.
      return Row(children: [
        ConstrainedBox(
          constraints: BoxConstraints(maxWidth: width * 0.4),
          child: name,
        ),
        const SizedBox(width: SonderSpace.md),
        Expanded(child: salientText),
        const SizedBox(width: SonderSpace.md),
        trailing,
      ]);
    }

    final output = toolOutputText(item.output);
    final body = Padding(
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.md, 0, SonderSpace.md, SonderSpace.md),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Divider(height: 1, color: tokens.hairline),
          const SizedBox(height: SonderSpace.xs),
          if (item.arguments != null)
            StructuredFields(item.arguments, label: 'Arguments'),
          if (item.errorCode.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.md),
            Row(children: [
              const StatusMark(StatusKind.fail, word: 'error', size: 12),
              const SizedBox(width: SonderSpace.md),
              Flexible(
                child: SelectionArea(
                  child: Text(item.errorCode,
                      style: tokens.mono(12, color: tokens.text2)),
                ),
              ),
            ]),
          ],
          const SizedBox(height: SonderSpace.md),
          if (output.isNotEmpty)
            RawOutput(output, label: 'Output', collapsedLines: 12)
          else
            Padding(
              padding: const EdgeInsets.only(bottom: SonderSpace.xs),
              child: Text(
                  switch (item.state) {
                    ToolState.requested => 'No result received yet.',
                    ToolState.noResult =>
                      'No result was recorded before the agent stopped.',
                    ToolState.rejected =>
                      'The lane refused this request before it ran.',
                    _ => 'No text output.',
                  },
                  style: text.bodySmall?.copyWith(color: tokens.muted)),
            ),
        ],
      ),
    );

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: SonderSpace.xs),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: SonderSpace.md),
            child: _Gutter(statusGlyphs['tool']!, tokens.muted, size: 13),
          ),
          const SizedBox(width: transcriptGutterGap),
          Expanded(
            child: Container(
              decoration: BoxDecoration(
                color: tokens.panel,
                borderRadius: radius,
                border: Border.all(
                    color: expanded
                        ? tokens.hairlineStrong.withValues(alpha: 0.6)
                        : tokens.hairline),
              ),
              clipBehavior: Clip.antiAlias,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  HoverSurface(
                    onTap: onToggle,
                    borderRadius: BorderRadius.zero,
                    semanticLabel: '${item.name}, ${status.word}'
                        '${salient.isEmpty ? '' : ', $salient'}'
                        '${duration == null ? '' : ', $duration'}. '
                        '${expanded ? 'Hide' : 'Show'} details',
                    child: ExcludeSemantics(
                      child: ConstrainedBox(
                        constraints: const BoxConstraints(minHeight: 48),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                              horizontal: SonderSpace.md,
                              vertical: SonderSpace.sm + SonderSpace.xxs),
                          child: LayoutBuilder(
                            builder: (context, c) => Align(
                              alignment: Alignment.centerLeft,
                              child: summary(c.maxWidth),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  SonderReveal(visible: expanded, child: body),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// A quiet marker where the run changed state.
class LifecycleLine extends StatelessWidget {
  final LifecycleItem item;

  /// Whether to repeat the server's reason after the words. Off for the
  /// failure the header notice already explains.
  final bool showReason;

  const LifecycleLine({super.key, required this.item, this.showReason = true});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final detail = item.detail.isEmpty ? null : laneErrorSummary(item.detail);
    final (StatusKind kind, String words) = switch (item.kind) {
      LifecycleKind.resumed => (StatusKind.running, 'Resumed'),
      LifecycleKind.interrupted => (StatusKind.warn, 'Interrupted'),
      LifecycleKind.cancelled => (StatusKind.skipped, 'Cancelled'),
      LifecycleKind.failed => (StatusKind.fail, 'Failed'),
      LifecycleKind.stalled => (StatusKind.warn, 'Stopped, needs input'),
      LifecycleKind.completed => (StatusKind.ok, 'Completed'),
      LifecycleKind.exited => (StatusKind.warn, 'The worker process exited'),
      LifecycleKind.reported => (StatusKind.note, 'Reported to the parent'),
    };
    final showDetail = showReason &&
        detail != null &&
        (item.kind == LifecycleKind.failed ||
            item.kind == LifecycleKind.stalled);
    return Semantics(
      label: showDetail ? '$words. $detail' : words,
      excludeSemantics: true,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: SonderSpace.sm),
        child: Row(children: [
          SizedBox(
            width: transcriptGutter,
            child: Text(kind.glyph,
                textAlign: TextAlign.center,
                style: tokens.mono(12,
                    color: kind.color(tokens), weight: FontWeight.w600)),
          ),
          const SizedBox(width: transcriptGutterGap),
          Flexible(
            child: Text.rich(
              TextSpan(children: [
                TextSpan(
                    text: words,
                    style: TextStyle(
                        color: kind == StatusKind.note
                            ? tokens.muted
                            : kind.color(tokens),
                        fontWeight: FontWeight.w500)),
                if (showDetail) TextSpan(text: ' · $detail'),
              ]),
              style: text.bodySmall?.copyWith(color: tokens.muted),
            ),
          ),
        ]),
      ),
    );
  }
}

/// A small tinted pill: glyph and word in one colour, like [StatusPill].
class _Pill extends StatelessWidget {
  final String glyph;
  final String word;
  final Color color;

  const _Pill(this.glyph, this.word, this.color);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Semantics(
      label: word,
      child: ExcludeSemantics(
        child: Container(
          padding: const EdgeInsets.symmetric(
              horizontal: SonderSpace.sm, vertical: SonderSpace.xxs),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(SonderRadius.pill),
          ),
          child: Text('$glyph $word',
              style: tokens.mono(11.5, color: color, weight: FontWeight.w600)),
        ),
      ),
    );
  }
}

/// A report to the parent. Unread reports open with an accent edge and a
/// Mark read action; read ones collapse to their header, content kept.
class ReportCard extends StatelessWidget {
  final AgentReport report;
  final bool expanded;
  final VoidCallback onToggle;
  final Future<void> Function()? onMarkRead;
  final void Function(Object error, StackTrace stack)? onMarkReadError;

  const ReportCard({
    super.key,
    required this.report,
    required this.expanded,
    required this.onToggle,
    this.onMarkRead,
    this.onMarkReadError,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final unread = !report.acknowledged;
    final body = Padding(
      padding: const EdgeInsets.fromLTRB(
          SonderSpace.lg, 0, SonderSpace.lg, SonderSpace.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          ConversationContent(content: report.summary, fullWidthCode: true),
          if (report.artifacts.isNotEmpty) ...[
            const SizedBox(height: SonderSpace.md),
            Text('Artifacts', style: text.labelMedium),
            const SizedBox(height: SonderSpace.xs),
            SelectionArea(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final artifact in report.artifacts)
                    Padding(
                      padding:
                          const EdgeInsets.symmetric(vertical: SonderSpace.xxs),
                      child: Row(children: [
                        Icon(Icons.description_outlined,
                            size: 14, color: tokens.muted),
                        const SizedBox(width: SonderSpace.sm),
                        Expanded(
                          child: Text(artifact,
                              style: tokens.mono(12, color: tokens.text2)),
                        ),
                      ]),
                    ),
                ],
              ),
            ),
          ],
          if (unread && onMarkRead != null) ...[
            const SizedBox(height: SonderSpace.md),
            Align(
              alignment: Alignment.centerLeft,
              child: AsyncActionButton(
                label: 'Mark read',
                icon: Icons.done_all,
                doneLabel: null,
                onPressed: onMarkRead,
                onError: onMarkReadError,
              ),
            ),
          ],
        ],
      ),
    );
    return Container(
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.row),
        border: Border.all(
            color: unread
                ? tokens.accent.withValues(alpha: 0.45)
                : tokens.hairline),
      ),
      clipBehavior: Clip.antiAlias,
      child: Stack(children: [
        Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            HoverSurface(
              onTap: onToggle,
              borderRadius: BorderRadius.zero,
              semanticLabel: 'Report to parent, ${unread ? 'unread' : 'read'}. '
                  '${expanded ? 'Hide' : 'Show'} report',
              child: ExcludeSemantics(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: 48),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                        horizontal: SonderSpace.lg, vertical: SonderSpace.md),
                    child: Row(children: [
                      unread
                          ? _Pill('●', 'Unread', tokens.accentText)
                          : _Pill(StatusKind.ok.glyph, 'Read', tokens.muted),
                      const SizedBox(width: SonderSpace.md),
                      Expanded(
                        child: Text('Report to parent',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: text.titleSmall
                                ?.copyWith(fontWeight: FontWeight.w600)),
                      ),
                      AnimatedRotation(
                        turns: expanded ? 0.5 : 0,
                        duration: SonderMotion.of(context, SonderMotion.fast),
                        child: Icon(Icons.expand_more,
                            size: 18, color: tokens.text2),
                      ),
                    ]),
                  ),
                ),
              ),
            ),
            SonderReveal(visible: expanded, child: body),
          ],
        ),
        if (unread)
          Positioned(
            left: 0,
            top: 0,
            bottom: 0,
            child: Container(width: 3, color: tokens.accent),
          ),
      ]),
    );
  }
}

/// "Task, workspace and run details": collapsed by default, so the
/// conversation keeps visual priority.
class RunDetails extends StatelessWidget {
  final AgentLane lane;
  final bool expanded;
  final VoidCallback onToggle;
  final VoidCallback? onOpenRuntime;

  const RunDetails({
    super.key,
    required this.lane,
    required this.expanded,
    required this.onToggle,
    this.onOpenRuntime,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    Widget label(String value) => Padding(
          padding: const EdgeInsets.only(bottom: SonderSpace.xs),
          child: Text(value, style: text.labelMedium),
        );
    final sections = <Widget>[
      if (lane.task.isNotEmpty)
        Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          label('Task'),
          ConversationContent(content: lane.task, fullWidthCode: true),
        ]),
      if (lane.workspaceRoot.isNotEmpty)
        Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          label('Assigned workspace'),
          SelectionArea(
            child: Text(lane.workspaceRoot,
                style: tokens.mono(12, color: tokens.text2)),
          ),
        ]),
      if (lane.tier.isNotEmpty)
        Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          label('Execution'),
          SelectionArea(
            child: Text(lane.executionSummary,
                style: tokens.mono(12, color: tokens.text2)),
          ),
          const SizedBox(height: SonderSpace.sm),
          Text(
              'Per-lane capacity counters are not reported by this server. '
              'Use Runtime for cluster-level capacity and resource health.',
              style: text.bodySmall),
          if (onOpenRuntime != null)
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                onPressed: onOpenRuntime,
                icon: const Icon(Icons.dashboard_customize_outlined, size: 16),
                label: const Text('Open Runtime'),
              ),
            ),
        ]),
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        HoverSurface(
          onTap: onToggle,
          semanticLabel: 'Task, workspace and run details, '
              '${expanded ? 'expanded' : 'collapsed'}',
          child: ExcludeSemantics(
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 48),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: SonderSpace.xs),
                child: Row(children: [
                  SizedBox(
                    width: transcriptGutter - SonderSpace.xs,
                    child: AnimatedRotation(
                      turns: expanded ? 0.25 : 0,
                      duration: SonderMotion.of(context, SonderMotion.fast),
                      child: Icon(Icons.chevron_right,
                          size: 18, color: tokens.text2),
                    ),
                  ),
                  const SizedBox(width: transcriptGutterGap),
                  Expanded(
                    child: Text('Task, workspace and run details',
                        style: text.bodyMedium?.copyWith(
                            color: tokens.text2, fontWeight: FontWeight.w500)),
                  ),
                ]),
              ),
            ),
          ),
        ),
        SonderReveal(
          visible: expanded,
          child: Padding(
            padding: const EdgeInsets.only(
                left: transcriptGutter + transcriptGutterGap,
                top: SonderSpace.xs,
                bottom: SonderSpace.md),
            child: Container(
              padding: const EdgeInsets.all(SonderSpace.lg),
              decoration: BoxDecoration(
                color: tokens.panel,
                borderRadius: BorderRadius.circular(SonderRadius.row),
                border: Border.all(color: tokens.hairline),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (var i = 0; i < sections.length; i++) ...[
                    if (i > 0) const SizedBox(height: SonderSpace.lg),
                    sections[i],
                  ],
                ],
              ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Message-shaped placeholders while a conversation's first page loads.
class TranscriptSkeleton extends StatelessWidget {
  const TranscriptSkeleton({super.key});

  @override
  Widget build(BuildContext context) {
    Widget turn(double a, double b, {bool tool = false}) => Padding(
          padding: const EdgeInsets.symmetric(vertical: SonderSpace.md),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const SizedBox(
              width: transcriptGutter,
              child: Center(
                child:
                    Skeleton(width: 12, height: 12, radius: SonderRadius.pill),
              ),
            ),
            const SizedBox(width: transcriptGutterGap),
            Expanded(
              child: LayoutBuilder(
                builder: (context, c) => tool
                    ? Skeleton(
                        width: c.maxWidth, height: 44, radius: SonderRadius.row)
                    : Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Skeleton(width: 72, height: 10),
                          const SizedBox(height: SonderSpace.md),
                          Skeleton(width: c.maxWidth * a, height: 12),
                          const SizedBox(height: SonderSpace.sm),
                          Skeleton(width: c.maxWidth * b, height: 12),
                        ],
                      ),
              ),
            ),
          ]),
        );
    return Semantics(
      label: 'Loading conversation',
      liveRegion: true,
      child: Column(children: [
        turn(0.92, 0.64),
        turn(0.8, 0.45),
        turn(1, 1, tool: true),
        turn(0.86, 0.58),
      ]),
    );
  }
}
