import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:sonder_runtime/safety_colors.dart';
import 'package:sonder_runtime/theme.dart';

void main() {
  test('unknown risk stays neutral and explicitly unpublished', () {
    final scheme = ColorScheme.fromSeed(seedColor: Colors.teal);

    expect(riskColor(scheme, 'future-policy'), scheme.outline);
    expect(riskLabel('future-policy'), 'Risk not published');
  });

  test('autonomy colors preserve the safety distinction', () {
    final dark = ColorScheme.fromSeed(
      seedColor: Colors.teal,
      brightness: Brightness.dark,
    );
    final light = ColorScheme.fromSeed(seedColor: Colors.teal);

    // Green is the read-only plan mode's alone; auto never borrows it, and
    // each theme draws the same meaning in its own contrast-safe value.
    expect(permissionModeColor(dark, 'plan'), SonderTokens.dark.ok);
    expect(permissionModeColor(light, 'plan'), SonderTokens.light.ok);
    expect(permissionModeColor(dark, 'auto'), isNot(SonderTokens.dark.ok));
    expect(permissionModeColor(dark, 'auto'), SonderTokens.dark.auto);
    expect(permissionModeColor(light, 'future-mode'), light.outline);
    expect(permissionModeIcon('manual'), Icons.pan_tool_outlined);
    expect(permissionModeIcon('future-mode'), Icons.help_outline);
  });

  test('risk tones come from the same token set as the surfaces', () {
    final dark = ColorScheme.fromSeed(
      seedColor: Colors.teal,
      brightness: Brightness.dark,
    );
    expect(riskColor(dark, 'safe'), SonderTokens.dark.ok);
    expect(riskColor(dark, 'dangerous'), SonderTokens.dark.danger);
    expect(riskColor(dark, 'unclassified'), SonderTokens.dark.muted);
    expect(riskColor(dark, 'safe'), isNot(riskColor(dark, 'ask')));
  });

  test('both themes carry the token extension and the bundled faces', () {
    for (final theme in [SonderTheme.dark, SonderTheme.light]) {
      final tokens = theme.extension<SonderTokens>();
      expect(tokens, isNotNull);
      expect(theme.textTheme.bodyMedium?.fontFamily, SonderTheme.sans);
      expect(tokens!.mono(13).fontFamily, SonderTheme.mono);
      expect(theme.colorScheme.primary, tokens.accent);
      expect(theme.scaffoldBackgroundColor, tokens.canvas);
    }
  });
}
