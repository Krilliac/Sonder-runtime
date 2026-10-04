/// The Agents list: conversation rows in Chat's glyph-gutter language, the
/// parent/child tree, status filters and background work rows.
library;

import 'package:flutter/material.dart';

import '../agent_lanes.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/status_vocab.dart';
import 'agent_status.dart';

/// Row geometry shared by lane rows, background rows and the tree painter.
abstract final class AgentRowMetrics {
  /// Inner left padding of a row, before the glyph gutter.
  static const inset = SonderSpace.md;

  /// Indent per tree level.
  static const indent = SonderSpace.xl;

  /// The glyph gutter.
  static const gutter = 18.0;

  /// Gap between the gutter and the text.
  static const gap = SonderSpace.md;

  /// Top padding of a row; the title's first line is centred 11 below it.
  static const top = SonderSpace.md;

  static double glyphCenterX(int level) => inset + level * indent + gutter / 2;
  static const glyphCenterY = top + 11;
}

/// A sentence-case section label with an optional count and actions.
class AgentSectionHeader extends StatelessWidget {
  final String title;
  final int? count;
  final String? countSemantics;
  final List<Widget> actions;

  const AgentSectionHeader({
    super.key,
    required this.title,
    this.count,
    this.countSemantics,
    this.actions = const [],
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 40),
      child: Padding(
        // Starts at the rows' glyph column.
        padding: const EdgeInsets.fromLTRB(
            AgentRowMetrics.inset, SonderSpace.sm, SonderSpace.xs, 0),
        child: Row(children: [
          Expanded(
            child: Semantics(
              header: true,
              label: count == null
                  ? title
                  : '$title, ${countSemantics ?? '$count'}',
              excludeSemantics: true,
              child: Row(children: [
                Flexible(
                  child: Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: text.labelLarge?.copyWith(
                        color: tokens.text2, fontWeight: FontWeight.w600),
                  ),
                ),
                if (count != null) ...[
                  const SizedBox(width: SonderSpace.sm),
                  Text('$count', style: tokens.mono(11.5, color: tokens.muted)),
                ],
              ]),
            ),
          ),
          ...actions,
        ]),
      ),
    );
  }
}

/// The status filters as one compact segmented control. Every segment keeps
/// a 48 dp target around a smaller visible segment. Only the filters that
/// ask for attention (Needs you, Unread) show their count; every count is
/// in the segment's semantics. When text is too large for one line the
/// control scrolls sideways instead of wrapping.
class AgentFilterBar extends StatelessWidget {
  final AgentFilter value;
  final Map<AgentFilter, int> counts;
  final ValueChanged<AgentFilter> onChanged;

  const AgentFilterBar({
    super.key,
    required this.value,
    required this.counts,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    const filters = AgentFilter.values;
    final segments = <Widget>[];
    for (var i = 0; i < filters.length; i++) {
      final filter = filters[i];
      if (i > 0) {
        // A divider disappears beside the selected segment.
        final quiet = filter == value || filters[i - 1] == value;
        segments.add(AnimatedOpacity(
          duration: SonderMotion.of(context, SonderMotion.fast),
          opacity: quiet ? 0 : 1,
          child: Container(width: 1, height: 14, color: tokens.hairline),
        ));
      }
      final count = counts[filter] ?? 0;
      segments.add(_Segment(
        key: Key('agent-filter-${filter.name}'),
        label: filter.label,
        semanticCount: filter == AgentFilter.all ? null : count,
        shownCount: switch (filter) {
          AgentFilter.attention ||
          AgentFilter.unread =>
            count > 0 ? count : null,
          _ => null,
        },
        countColor:
            filter == AgentFilter.attention ? tokens.warn : tokens.accentText,
        selected: filter == value,
        onTap: () => onChanged(filter),
      ));
    }
    return Semantics(
      container: true,
      label: 'Filter conversations',
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: SizedBox(
          height: 48,
          child: Stack(alignment: Alignment.centerLeft, children: [
            Positioned.fill(
              top: SonderSpace.sm,
              bottom: SonderSpace.sm,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  color: tokens.canvas,
                  borderRadius: BorderRadius.circular(SonderRadius.row),
                  border: Border.all(
                      color: tokens.hairlineStrong.withValues(alpha: 0.7)),
                ),
              ),
            ),
            Row(mainAxisSize: MainAxisSize.min, children: segments),
          ]),
        ),
      ),
    );
  }
}

class _Segment extends StatefulWidget {
  final String label;
  final int? semanticCount;
  final int? shownCount;
  final Color countColor;
  final bool selected;
  final VoidCallback onTap;

  const _Segment({
    super.key,
    required this.label,
    required this.semanticCount,
    required this.shownCount,
    required this.countColor,
    required this.selected,
    required this.onTap,
  });

  @override
  State<_Segment> createState() => _SegmentState();
}

class _SegmentState extends State<_Segment> {
  bool _hover = false, _pressed = false, _focused = false;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final selected = widget.selected;
    final shown = widget.shownCount;
    final background = selected
        ? tokens.accentDim
        : _pressed
            ? tokens.raised
            : _hover
                ? tokens.raised.withValues(alpha: 0.7)
                : Colors.transparent;
    return Semantics(
      button: true,
      selected: selected,
      inMutuallyExclusiveGroup: true,
      label: widget.semanticCount == null
          ? widget.label
          : '${widget.label}, ${widget.semanticCount}',
      excludeSemantics: true,
      child: InkWell(
        onTap: widget.onTap,
        onHover: (value) => setState(() => _hover = value),
        onHighlightChanged: (value) => setState(() => _pressed = value),
        onFocusChange: (value) => setState(() => _focused = value),
        splashFactory: NoSplash.splashFactory,
        overlayColor: const WidgetStatePropertyAll(Colors.transparent),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48, minWidth: 48),
          // A 28 px segment inset 2 px inside the 32 px track.
          child: Padding(
            padding: const EdgeInsets.symmetric(
                horizontal: SonderSpace.xxs,
                vertical: SonderSpace.sm + SonderSpace.xxs),
            child: AnimatedContainer(
              duration: SonderMotion.of(context, SonderMotion.fast),
              curve: SonderMotion.standard,
              alignment: Alignment.center,
              padding: const EdgeInsets.symmetric(
                  horizontal: SonderSpace.xs + SonderSpace.xxs),
              decoration: BoxDecoration(
                color: background,
                borderRadius: BorderRadius.circular(SonderRadius.control),
                border: _focused
                    ? Border.all(color: tokens.accentText)
                    : Border.all(color: Colors.transparent),
              ),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                Text(widget.label,
                    style: text.labelMedium?.copyWith(
                      color: selected ? tokens.accentText : tokens.text2,
                      fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
                    )),
                if (shown != null) ...[
                  const SizedBox(width: SonderSpace.xs),
                  Text('$shown',
                      style: tokens.mono(11,
                          color: widget.countColor, weight: FontWeight.w600)),
                ],
              ]),
            ),
          ),
        ),
      ),
    );
  }
}

/// Where a row sits in the parent/child tree.
class TreePosition {
  /// 0 for a root row.
  final int depth;

  /// For each ancestor level above the parent's: whether a sibling further
  /// down keeps that level's line going through this row.
  final List<bool> rails;

  /// Whether this row is the last child of its parent.
  final bool last;

  /// Whether rows below are this row's children.
  final bool hasChildren;

  const TreePosition({
    this.depth = 0,
    this.rails = const [],
    this.last = true,
    this.hasChildren = false,
  });

  static const root = TreePosition();
}

class _TreePainter extends CustomPainter {
  final TreePosition tree;
  final Color color;

  _TreePainter(this.tree, this.color);

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 1
      ..style = PaintingStyle.stroke;
    const y = AgentRowMetrics.glyphCenterY;
    final d = tree.depth;
    for (var level = 0; level < d - 1; level++) {
      if (level < tree.rails.length && tree.rails[level]) {
        final x = AgentRowMetrics.glyphCenterX(level).roundToDouble() + 0.5;
        canvas.drawLine(Offset(x, 0), Offset(x, size.height), paint);
      }
    }
    if (d > 0) {
      final x = AgentRowMetrics.glyphCenterX(d - 1).roundToDouble() + 0.5;
      // Up to the child's glyph box, stopping just short of the glyph.
      final end = AgentRowMetrics.inset + d * AgentRowMetrics.indent + 1;
      const r = 5.0;
      final path = Path()
        ..moveTo(x, 0)
        ..lineTo(x, y - r)
        ..quadraticBezierTo(x, y, x + r, y)
        ..lineTo(end, y);
      canvas.drawPath(path, paint);
      if (!tree.last) {
        canvas.drawLine(Offset(x, y - r), Offset(x, size.height), paint);
      }
    }
    if (tree.hasChildren) {
      final x = AgentRowMetrics.glyphCenterX(d).roundToDouble() + 0.5;
      canvas.drawLine(Offset(x, y + 10), Offset(x, size.height), paint);
    }
  }

  @override
  bool shouldRepaint(_TreePainter old) =>
      old.tree.depth != tree.depth ||
      old.tree.last != tree.last ||
      old.tree.hasChildren != tree.hasChildren ||
      old.color != color ||
      old.tree.rails.join() != tree.rails.join();
}

/// The frame every list row shares: hover and selected surfaces, the
/// selected accent edge, the glyph gutter and the tree connector.
class _AgentRowFrame extends StatelessWidget {
  final StatusKind kind;
  final bool selected;
  final TreePosition tree;
  final VoidCallback onTap;
  final String semanticLabel;
  final Widget body;
  final Widget? trailing;

  const _AgentRowFrame({
    required this.kind,
    required this.selected,
    required this.tree,
    required this.onTap,
    required this.semanticLabel,
    required this.body,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final indent = tree.depth * AgentRowMetrics.indent;
    return HoverSurface(
      selected: selected,
      onTap: onTap,
      semanticLabel: semanticLabel,
      child: ExcludeSemantics(
        child: Stack(children: [
          if (tree.depth > 0 || tree.hasChildren)
            Positioned.fill(
              child: CustomPaint(
                painter: _TreePainter(
                    tree, tokens.hairlineStrong.withValues(alpha: 0.55)),
              ),
            ),
          ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 48),
            child: Padding(
              padding: EdgeInsets.fromLTRB(AgentRowMetrics.inset + indent,
                  AgentRowMetrics.top, SonderSpace.md, AgentRowMetrics.top),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                    width: AgentRowMetrics.gutter,
                    height: 22,
                    child: Center(
                      // The note dot is tiny at text size; queued rows
                      // need it visible in the gutter.
                      child: Text(kind.glyph,
                          style: tokens.mono(kind == StatusKind.note ? 20 : 13,
                              color: kind.color(tokens),
                              weight: FontWeight.w600)),
                    ),
                  ),
                  const SizedBox(width: AgentRowMetrics.gap),
                  Expanded(child: body),
                  if (trailing != null) ...[
                    const SizedBox(width: SonderSpace.sm),
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: trailing!,
                    ),
                  ],
                ],
              ),
            ),
          ),
          Positioned(
            left: 0,
            top: SonderSpace.md,
            bottom: SonderSpace.md,
            child: AnimatedContainer(
              duration: SonderMotion.of(context, SonderMotion.fast),
              curve: SonderMotion.standard,
              width: selected ? 3 : 0,
              decoration: BoxDecoration(
                color: tokens.accent,
                borderRadius: BorderRadius.circular(SonderRadius.pill),
              ),
            ),
          ),
        ]),
      ),
    );
  }
}

/// The muted words after a lane's status: its task when the title does not
/// already say it, otherwise its model tier.
String laneSnippet(AgentLane lane) {
  final task = lane.task.trim().split('\n').first.trim();
  final title = lane.displayTitle.trim();
  final failing = lane.status == 'failed' || lane.status == 'awaiting_input';
  if (failing && lane.error.isNotEmpty) {
    return laneErrorSummary(lane.error).replaceAll(RegExp(r'\.$'), '');
  }
  if (task.isNotEmpty &&
      task != title &&
      !task.startsWith(title.replaceAll('…', ''))) {
    return task;
  }
  return lane.tier.isEmpty ? '' : 'tier ${lane.tier}';
}

/// One agent conversation in the list.
class LaneRow extends StatelessWidget {
  final AgentLane lane;
  final bool selected;
  final TreePosition tree;
  final VoidCallback onTap;

  /// "4m ago", only when the server supplied a timestamp.
  final String? age;

  const LaneRow({
    super.key,
    required this.lane,
    required this.selected,
    required this.onTap,
    this.tree = TreePosition.root,
    this.age,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final kind = lane.statusKind;
    final snippet = laneSnippet(lane);
    final unread = lane.unreadReports;
    final title = lane.displayTitle;
    final semantic = StringBuffer('$title. ${lane.statusLabel}');
    if (unread > 0) {
      semantic.write(', $unread unread report${unread == 1 ? '' : 's'}');
    }
    if (age != null) semantic.write(', updated $age');
    return _AgentRowFrame(
      kind: kind,
      selected: selected,
      tree: tree,
      onTap: onTap,
      semanticLabel: semantic.toString(),
      trailing: unread > 0
          ? CountBadge(unread,
              semantic: '$unread unread report${unread == 1 ? '' : 's'}')
          : null,
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Tooltip(
            message: title,
            child: Text(
              title,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: text.bodyMedium?.copyWith(
                fontWeight: FontWeight.w500,
                color:
                    lane.isFinished && !selected ? tokens.text2 : tokens.text,
              ),
            ),
          ),
          const SizedBox(height: SonderSpace.xxs),
          Text.rich(
            TextSpan(children: [
              TextSpan(
                text: lane.statusLabel,
                style: TextStyle(
                    color: kind.color(tokens), fontWeight: FontWeight.w500),
              ),
              if (snippet.isNotEmpty) TextSpan(text: ' · $snippet'),
              if (age != null) TextSpan(text: ' · $age'),
            ]),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: text.bodySmall?.copyWith(color: tokens.muted),
          ),
        ],
      ),
    );
  }
}

/// One coloured part of a [SegmentedProgress] bar.
class ProgressPart {
  final int count;
  final Color color;
  const ProgressPart(this.count, this.color);
}

/// A slim bar of coloured parts out of [total] (done, running, failed…).
/// Its [semanticLabel] states the numbers, so colour is never the signal.
class SegmentedProgress extends StatelessWidget {
  final List<ProgressPart> parts;
  final int total;
  final String semanticLabel;
  final double height;

  const SegmentedProgress({
    super.key,
    required this.parts,
    required this.total,
    required this.semanticLabel,
    this.height = 4,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final used = parts.fold<int>(0, (sum, part) => sum + part.count);
    final rest = (total - used).clamp(0, total);
    return Semantics(
      label: semanticLabel,
      child: ExcludeSemantics(
        child: ClipRRect(
          borderRadius: BorderRadius.circular(SonderRadius.pill),
          child: Container(
            height: height,
            color: tokens.hairline,
            child: total <= 0
                ? null
                : Row(children: [
                    for (final part in parts)
                      if (part.count > 0)
                        Expanded(
                          flex: part.count,
                          child: Container(
                            margin: const EdgeInsets.only(right: 1),
                            color: part.color,
                          ),
                        ),
                    if (rest > 0) Spacer(flex: rest),
                  ]),
          ),
        ),
      ),
    );
  }
}

/// A fleet or autopilot run in the list.
class BackgroundRow extends StatelessWidget {
  final String kindLabel;
  final String title;
  final String status;
  final String detail;
  final SegmentedProgress? progress;
  final bool selected;
  final VoidCallback onTap;

  const BackgroundRow({
    super.key,
    required this.kindLabel,
    required this.title,
    required this.status,
    required this.detail,
    required this.selected,
    required this.onTap,
    this.progress,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final s = backgroundStatus(status);
    return _AgentRowFrame(
      kind: s.kind,
      selected: selected,
      tree: TreePosition.root,
      onTap: onTap,
      semanticLabel: '$kindLabel: $title. ${s.word}'
          '${progress != null ? ', ${progress!.semanticLabel}' : detail.isEmpty ? '' : ', $detail'}',
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Tooltip(
            message: title,
            child: Text(title,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: text.bodyMedium?.copyWith(
                  fontWeight: FontWeight.w500,
                  color: s.kind == StatusKind.ok && !selected
                      ? tokens.text2
                      : tokens.text,
                )),
          ),
          const SizedBox(height: SonderSpace.xxs),
          Text.rich(
            TextSpan(children: [
              TextSpan(
                text: s.word,
                style: TextStyle(
                    color: s.kind.color(tokens), fontWeight: FontWeight.w500),
              ),
              TextSpan(text: ' · $kindLabel'),
              if (detail.isNotEmpty) TextSpan(text: ' · $detail'),
            ]),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: text.bodySmall?.copyWith(color: tokens.muted),
          ),
          if (progress != null) ...[
            const SizedBox(height: SonderSpace.sm),
            progress!,
            const SizedBox(height: SonderSpace.xxs),
          ],
        ],
      ),
    );
  }
}

/// The short reference of a parent session, which opens its full ID.
class ParentGroupLabel extends StatelessWidget {
  final String id;
  final String label;
  final VoidCallback onTap;

  const ParentGroupLabel({
    super.key,
    required this.id,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(SonderSpace.xs, SonderSpace.xs, 0, 0),
      child: Align(
        alignment: Alignment.centerLeft,
        child: Semantics(
          label: 'Parent conversation $id',
          child: TextButton.icon(
            style: TextButton.styleFrom(
              foregroundColor: tokens.muted,
              padding: const EdgeInsets.symmetric(horizontal: SonderSpace.sm),
            ),
            onPressed: onTap,
            icon: const Icon(Icons.call_split, size: 14),
            label: Text(label, style: tokens.mono(11.5, color: tokens.muted)),
          ),
        ),
      ),
    );
  }
}
