import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../theme.dart';

/// A small circular meter for a fill level (context used, VRAM, quota).
///
/// The arc is accent below [warnAt], warn from [warnAt] and danger from
/// [dangerAt]. A ring cannot show a number, so [semanticLabel] must state
/// it ("Context: 2,100 of 8,192 tokens, 26%"); pair it with a tooltip or a
/// visible value for sighted readers. The arc animates to a new value
/// (instantly under reduced motion).
class RingMeter extends StatelessWidget {
  /// Fill from 0 to 1; values outside are clamped, NaN draws empty.
  final double value;
  final String semanticLabel;
  final double size;
  final double strokeWidth;
  final double warnAt;
  final double dangerAt;

  const RingMeter({
    super.key,
    required this.value,
    required this.semanticLabel,
    this.size = 18,
    this.strokeWidth = 2.5,
    this.warnAt = 0.75,
    this.dangerAt = 0.9,
  });

  /// The arc colour for [value] in [tokens].
  static Color colorFor(SonderTokens tokens, double value,
          {double warnAt = 0.75, double dangerAt = 0.9}) =>
      value >= dangerAt
          ? tokens.danger
          : value >= warnAt
              ? tokens.warn
              : tokens.accentText;

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final v = value.isNaN ? 0.0 : value.clamp(0.0, 1.0);
    final color = colorFor(tokens, v, warnAt: warnAt, dangerAt: dangerAt);
    return Semantics(
      label: semanticLabel,
      child: ExcludeSemantics(
        child: SizedBox.square(
          dimension: size,
          child: TweenAnimationBuilder<double>(
            tween: Tween(end: v),
            duration: SonderMotion.of(context, SonderMotion.slow),
            curve: SonderMotion.standard,
            builder: (context, fill, _) => CustomPaint(
              painter: _RingPainter(
                fill: fill,
                color: color,
                track: tokens.hairlineStrong.withValues(alpha: 0.45),
                strokeWidth: strokeWidth,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _RingPainter extends CustomPainter {
  final double fill;
  final Color color;
  final Color track;
  final double strokeWidth;

  const _RingPainter({
    required this.fill,
    required this.color,
    required this.track,
    required this.strokeWidth,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final rect = (Offset.zero & size).deflate(strokeWidth / 2);
    final base = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth
      ..color = track;
    canvas.drawArc(rect, 0, math.pi * 2, false, base);
    if (fill <= 0) return;
    final arc = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth
      ..strokeCap = StrokeCap.round
      ..color = color;
    // A sliver of fill still shows as a dot, so "almost empty" is visible.
    final sweep = math.max(fill, 0.02) * math.pi * 2;
    canvas.drawArc(rect, -math.pi / 2, sweep, false, arc);
  }

  @override
  bool shouldRepaint(_RingPainter old) =>
      old.fill != fill ||
      old.color != color ||
      old.track != track ||
      old.strokeWidth != strokeWidth;
}
