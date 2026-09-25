import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// Loads the bundled faces (IBM Plex Sans/Mono, Material Icons and the
/// SonderSymbols subset) through [FontLoader], so goldens are hermetic: no
/// Ahem boxes, no platform fonts and no network. Call once per test, inside
/// the test body.
Future<void> loadGoldenFonts(WidgetTester tester) async {
  await tester.runAsync(() async {
    final icons = FontLoader('MaterialIcons')
      ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
    await icons.load();
    for (final family in ['Sans', 'Mono']) {
      final loader = FontLoader('IBM Plex $family');
      for (final weight in ['Regular', 'Medium', 'SemiBold']) {
        loader.addFont(rootBundle.load('fonts/IBMPlex$family-$weight.ttf'));
      }
      await loader.load();
    }
    final symbols = FontLoader('SonderSymbols')
      ..addFont(rootBundle.load('assets/fonts/SonderSymbols.ttf'));
    await symbols.load();
  });
}

/// Goldens are generated and compared on Linux only: other platforms
/// rasterise the same fonts slightly differently.
final goldenSkip = Platform.isLinux ? null : 'goldens are compared on Linux';
