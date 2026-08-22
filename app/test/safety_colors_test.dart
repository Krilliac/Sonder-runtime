import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:sonder_runtime/safety_colors.dart';

void main() {
  test('unknown risk stays neutral and explicitly unpublished', () {
    final scheme = ColorScheme.fromSeed(seedColor: Colors.teal);

    expect(riskColor(scheme, 'future-policy'), scheme.outline);
    expect(riskLabel('future-policy'), 'Risk not published');
  });

  test('autonomy colors preserve the safety distinction', () {
    final scheme = ColorScheme.fromSeed(seedColor: Colors.teal);

    expect(permissionModeColor(scheme, 'plan'), const Color(0xFF1B5E20));
    expect(permissionModeColor(scheme, 'auto'), isNot(const Color(0xFF1B5E20)));
    expect(permissionModeIcon('manual'), Icons.pan_tool_outlined);
    expect(permissionModeIcon('future-mode'), Icons.help_outline);
  });
}
