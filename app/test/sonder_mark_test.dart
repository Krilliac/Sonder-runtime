import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/ui/sonder_mark.dart';

void main() {
  test('the S glyph spans exactly the brand SVG path bounds', () {
    final bounds = SonderMarkPainter.glyph.getBounds();
    const expected = SonderMarkPainter.glyphBounds;
    expect(bounds.left, closeTo(expected.left, 0.01));
    expect(bounds.top, closeTo(expected.top, 0.01));
    expect(bounds.right, closeTo(expected.right, 0.01));
    expect(bounds.bottom, closeTo(expected.bottom, 0.01));
  });

  test('the S keeps its counters and the split between its halves open', () {
    final glyph = SonderMarkPainter.glyph;
    // Inside each bowl.
    expect(glyph.contains(const Offset(60, 140)), isTrue);
    expect(glyph.contains(const Offset(360, 280)), isTrue);
    // The two counters (the enclosed notches of the S).
    expect(glyph.contains(const Offset(170, 140)), isFalse);
    expect(glyph.contains(const Offset(250, 280)), isFalse);
    // The diagonal cut between the halves, midway along the slant.
    expect(glyph.contains(const Offset(210, 210)), isFalse);
    // Outside the slanted bar ends.
    expect(glyph.contains(const Offset(375, 100)), isFalse);
    expect(glyph.contains(const Offset(45, 320)), isFalse);
  });

  testWidgets('SonderMark is a sized vector, not an icon glyph',
      (tester) async {
    await tester.pumpWidget(const Directionality(
      textDirection: TextDirection.ltr,
      child: Center(child: SonderMark(size: 40)),
    ));

    expect(tester.getSize(find.byType(SonderMark)), const Size(40, 40));
    expect(
        find.descendant(
            of: find.byType(SonderMark), matching: find.byType(CustomPaint)),
        findsOneWidget);
    expect(find.byType(Icon), findsNothing);
  });
}
