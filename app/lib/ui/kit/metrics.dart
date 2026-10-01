import 'package:flutter/material.dart';

import '../../theme.dart';
import '../status_vocab.dart';
import 'motion.dart';

/// A status as a soft pill: glyph and word on a faint tint of its colour.
/// For headers, rail badges and tile corners where a bare `StatusMark` would
/// get lost. The word is always shown; colour never carries it alone.
class StatusPill extends StatelessWidget {
  final StatusKind kind;
  final String? word;
  final bool dense;

  const StatusPill(this.kind, {super.key, this.word, this.dense = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final color = kind.color(tokens);
    final shown = word ?? kind.word;
    return Semantics(
      label: shown,
      child: ExcludeSemantics(
        child: AnimatedContainer(
          duration: SonderMotion.of(context, SonderMotion.fast),
          padding: EdgeInsets.symmetric(
              horizontal: dense ? 6 : SonderSpace.sm, vertical: dense ? 1 : 2),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(SonderRadius.pill),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Text(kind.glyph,
                style: tokens.mono(dense ? 10.5 : 11.5,
                    color: color, weight: FontWeight.w600)),
            SizedBox(width: dense ? 3 : SonderSpace.xs),
            Text(shown,
                style: tokens.mono(dense ? 10.5 : 11.5,
                    color: color, weight: FontWeight.w600)),
          ]),
        ),
      ),
    );
  }
}

/// A small count, e.g. "3" running agents beside a rail entry. [semantic]
/// says what is counted ("3 running"), so the number is never bare.
class CountBadge extends StatelessWidget {
  final int count;
  final String semantic;
  final StatusKind kind;

  const CountBadge(
    this.count, {
    super.key,
    required this.semantic,
    this.kind = StatusKind.running,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final color = kind.color(tokens);
    return Semantics(
      label: semantic,
      child: ExcludeSemantics(
        child: Container(
          constraints: const BoxConstraints(minWidth: 20),
          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.14),
            borderRadius: BorderRadius.circular(SonderRadius.pill),
          ),
          child: Text(
            count > 99 ? '99+' : '$count',
            textAlign: TextAlign.center,
            style: tokens.mono(11, color: color, weight: FontWeight.w600),
          ),
        ),
      ),
    );
  }
}

/// A labelled horizontal meter. The fill animates to [value] (0..1); the
/// colour steps to warn and danger at [warnAt] and [dangerAt], and the
/// [valueLabel] text always states the number, so colour is never the only
/// signal.
class Meter extends StatelessWidget {
  final double value;
  final String label;
  final String valueLabel;
  final double warnAt;
  final double dangerAt;

  /// Higher is better (a hit rate) instead of worse (memory used).
  final bool higherIsBetter;

  const Meter({
    super.key,
    required this.value,
    required this.label,
    required this.valueLabel,
    this.warnAt = 0.75,
    this.dangerAt = 0.92,
    this.higherIsBetter = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final v = value.isNaN ? 0.0 : value.clamp(0.0, 1.0);
    final pressure = higherIsBetter ? 1 - v : v;
    final color = pressure >= dangerAt
        ? tokens.danger
        : pressure >= warnAt
            ? tokens.warn
            : tokens.accent;
    return Semantics(
      label: '$label: $valueLabel',
      child: ExcludeSemantics(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(children: [
              Expanded(child: Text(label, style: text.bodySmall)),
              Text(valueLabel,
                  style: tokens.mono(11.5, color: tokens.text2)),
            ]),
            const SizedBox(height: SonderSpace.xs + 2),
            ClipRRect(
              borderRadius: BorderRadius.circular(SonderRadius.pill),
              child: SizedBox(
                height: 6,
                child: Stack(children: [
                  Positioned.fill(child: ColoredBox(color: tokens.hairline)),
                  TweenAnimationBuilder<double>(
                    tween: Tween(end: v),
                    duration: SonderMotion.of(context, SonderMotion.slow),
                    curve: SonderMotion.standard,
                    builder: (context, fill, _) => FractionallySizedBox(
                      widthFactor: fill,
                      heightFactor: 1,
                      alignment: Alignment.centerLeft,
                      child: AnimatedContainer(
                        duration: SonderMotion.of(context, SonderMotion.medium),
                        decoration: BoxDecoration(
                          color: color,
                          borderRadius:
                              BorderRadius.circular(SonderRadius.pill),
                        ),
                      ),
                    ),
                  ),
                ]),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// An overview tile: what it is, its status, the headline value and a line
/// of detail, optionally a meter. Tapping opens the page that owns it.
class StatTile extends StatelessWidget {
  final String label;
  final IconData icon;
  final String value;
  final String? detail;
  final StatusKind? kind;
  final String? word;
  final Widget? meter;
  final VoidCallback? onTap;

  const StatTile({
    super.key,
    required this.label,
    required this.icon,
    required this.value,
    this.detail,
    this.kind,
    this.word,
    this.meter,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final body = Padding(
      padding: const EdgeInsets.all(SonderSpace.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(children: [
            Icon(icon, size: 16, color: tokens.text2),
            const SizedBox(width: SonderSpace.sm),
            Expanded(
              child: Text(label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: text.labelMedium),
            ),
            if (kind != null) StatusPill(kind!, word: word, dense: true),
          ]),
          const SizedBox(height: SonderSpace.md),
          SonderSwitcher(
            child: Text(
              value,
              key: ValueKey(value),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: text.titleLarge,
            ),
          ),
          if (detail != null) ...[
            const SizedBox(height: SonderSpace.xs),
            Text(detail!,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: text.bodySmall),
          ],
          if (meter != null) ...[
            const SizedBox(height: SonderSpace.md),
            meter!,
          ],
        ],
      ),
    );
    return Container(
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.card),
        border: Border.all(color: tokens.hairline),
      ),
      clipBehavior: Clip.antiAlias,
      child: onTap == null
          ? body
          : HoverSurface(
              onTap: onTap,
              borderRadius: BorderRadius.circular(SonderRadius.card),
              semanticLabel: '$label: ${word ?? kind?.word ?? ''} $value',
              child: body,
            ),
    );
  }
}

/// Lays tiles out in as many equal columns as fit ([minTileWidth] each,
/// at most [maxColumns]), with even gaps.
class StatGrid extends StatelessWidget {
  final List<Widget> children;
  final double minTileWidth;
  final int maxColumns;
  final double gap;

  const StatGrid({
    super.key,
    required this.children,
    this.minTileWidth = 220,
    this.maxColumns = 4,
    this.gap = SonderSpace.md,
  });

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final width = constraints.maxWidth;
      var columns = ((width + gap) / (minTileWidth + gap)).floor();
      columns = columns.clamp(1, maxColumns);
      // Tiles in one row share its height, so bottoms line up even when
      // only some tiles carry a meter.
      final rows = <Widget>[];
      for (var start = 0; start < children.length; start += columns) {
        final cells = <Widget>[];
        for (var i = 0; i < columns; i++) {
          if (i > 0) cells.add(SizedBox(width: gap));
          final index = start + i;
          cells.add(Expanded(
            child: index < children.length
                ? children[index]
                : const SizedBox.shrink(),
          ));
        }
        if (rows.isNotEmpty) rows.add(SizedBox(height: gap));
        rows.add(IntrinsicHeight(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: cells,
          ),
        ));
      }
      return Padding(
        padding: const EdgeInsets.only(bottom: SonderSpace.lg),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: rows,
        ),
      );
    });
  }
}
