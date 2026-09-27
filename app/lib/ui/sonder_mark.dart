import 'package:flutter/material.dart';

/// The product mark: Sonder's split S on a navy tile, the same composition
/// as the launcher icons (`app/assets/brand/`, rendered by
/// `scripts/generate_app_icons.py`). Drawn as a vector so it stays crisp at
/// every size without an image asset or an SVG dependency.
///
/// The brand purple-to-blue lives only here; teal remains the functional
/// accent everywhere else in the UI.
class SonderMark extends StatelessWidget {
  final double size;
  const SonderMark({super.key, this.size = 22});

  @override
  Widget build(BuildContext context) => SizedBox.square(
        dimension: size,
        child: const CustomPaint(painter: SonderMarkPainter()),
      );
}

/// Paints [SonderMark]. Exposed so tests can check the geometry directly.
class SonderMarkPainter extends CustomPainter {
  const SonderMarkPainter();

  /// Brand gradient along the mark's diagonal (matches the banner SVGs).
  static const gradient = [Color(0xFFB56CF7), Color(0xFF8A85FB), Color(0xFF4FA6FF)];
  static const tileTop = Color(0xFF16173D);
  static const tileBottom = Color(0xFF0B0C22);

  /// Bounds of [glyph] in its 420-unit source grid.
  static const glyphBounds = Rect.fromLTRB(38, 32, 382, 388);

  /// Glyph height as a fraction of the tile (the launcher icons' ratio).
  static const glyphScale = 0.62;
  static const cornerRadius = 0.22;

  /// The S, transcribed from the brand SVG path data (420-unit grid):
  /// two interlocking halves, each a half-ellipse bowl with a slanted cut.
  static final Path glyph = _buildGlyph();

  static Path _buildGlyph() {
    Path half({
      required Offset start,
      required double barEnd,
      required double bowlEnd,
      required double joinX,
      required Offset slant,
      required double counterEnd,
      required double tipX,
    }) {
      const outer = Radius.elliptical(113.95, 107.5);
      const inner = Radius.elliptical(39.95, 33.5);
      // SVG sweep-flag 0 (outer bowl) is counterclockwise, 1 (counter) clockwise.
      return Path()
        ..moveTo(start.dx, start.dy)
        ..lineTo(barEnd, start.dy)
        ..arcToPoint(Offset(barEnd, bowlEnd),
            radius: outer, clockwise: false)
        ..lineTo(joinX, bowlEnd)
        ..lineTo(slant.dx, slant.dy)
        ..lineTo(barEnd, slant.dy)
        ..arcToPoint(Offset(barEnd, counterEnd),
            radius: inner, clockwise: true)
        ..lineTo(tipX, counterEnd)
        ..close();
    }

    // M382 32H151.95A113.95 107.5 0 0 0 151.95 247H164.65L222.37 173
    // H151.95A39.95 33.5 0 0 1 151.95 106H324.28Z
    final top = half(
      start: const Offset(382, 32),
      barEnd: 151.95,
      bowlEnd: 247,
      joinX: 164.65,
      slant: const Offset(222.37, 173),
      counterEnd: 106,
      tipX: 324.28,
    );
    // M38 388H268.05A113.95 107.5 0 0 0 268.05 173H255.35L197.63 247
    // H268.05A39.95 33.5 0 0 1 268.05 314H95.72Z
    final bottom = half(
      start: const Offset(38, 388),
      barEnd: 268.05,
      bowlEnd: 173,
      joinX: 255.35,
      slant: const Offset(197.63, 247),
      counterEnd: 314,
      tipX: 95.72,
    );
    return Path()
      ..addPath(top, Offset.zero)
      ..addPath(bottom, Offset.zero);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final side = size.shortestSide;
    final tile = Offset.zero & Size.square(side);
    canvas.drawRRect(
      RRect.fromRectAndRadius(tile, Radius.circular(side * cornerRadius)),
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [tileTop, tileBottom],
        ).createShader(tile),
    );

    final scale = side * glyphScale / glyphBounds.height;
    canvas.save();
    canvas.translate(side / 2 - glyphBounds.center.dx * scale,
        side / 2 - glyphBounds.center.dy * scale);
    canvas.scale(scale);
    canvas.drawPath(
      glyph,
      Paint()
        ..isAntiAlias = true
        ..shader = const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: gradient,
          stops: [0, 0.5, 1],
        ).createShader(glyphBounds),
    );
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant SonderMarkPainter oldDelegate) => false;
}
