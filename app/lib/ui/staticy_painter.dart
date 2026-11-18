import 'dart:math' as math;

import 'package:flutter/material.dart';

class StaticyPainter extends CustomPainter {
  final double animation;
  final math.Random _random;

  StaticyPainter(this.animation) : _random = math.Random();

  @override
  void paint(Canvas canvas, Size size) {
    if (animation <= 0 || animation >= 1) return;

    final paint = Paint()
      ..color = Colors.white.withValues(alpha: 0.5 * (1 - animation))
      ..style = PaintingStyle.fill;

    for (int i = 0; i < 15; i++) {
      final x = _random.nextDouble() * size.width;
      final y = _random.nextDouble() * size.height;
      final sizeFactor = _random.nextDouble() * 3 + 1;
      
      // Particles expand outwards
      final offsetX = (x - size.width / 2) * animation * 2;
      final offsetY = (y - size.height / 2) * animation * 2;

      canvas.drawRect(
        Rect.fromLTWH(x + offsetX, y + offsetY, sizeFactor, sizeFactor),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(StaticyPainter oldDelegate) => true;
}
