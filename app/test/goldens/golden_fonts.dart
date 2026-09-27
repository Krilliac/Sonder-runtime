import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

bool _loaded = false;

/// Loads the bundled faces (IBM Plex Sans/Mono, Material Icons and the
/// SonderSymbols subset) through [FontLoader], so goldens are hermetic: no
/// Ahem boxes, no platform fonts and no network. Safe to call more than once.
///
/// Inside a `testWidgets` body pass the [tester] so the asset loads run in
/// real async; from `setUpAll` call it without one.
Future<void> loadGoldenFonts([WidgetTester? tester]) async {
  if (_loaded) return;
  Future<void> load() async {
    Future<void> family(String name, List<String> assets) async {
      final loader = FontLoader(name);
      for (final asset in assets) {
        loader.addFont(rootBundle.load(asset));
      }
      await loader.load();
    }

    await family('MaterialIcons', ['fonts/MaterialIcons-Regular.otf']);
    for (final face in ['Sans', 'Mono']) {
      await family('IBM Plex $face', [
        for (final weight in ['Regular', 'Medium', 'SemiBold'])
          'fonts/IBMPlex$face-$weight.ttf',
      ]);
    }
    await family('SonderSymbols', ['assets/fonts/SonderSymbols.ttf']);
    _loaded = true;
  }

  if (tester != null) {
    await tester.runAsync(load);
  } else {
    TestWidgetsFlutterBinding.ensureInitialized();
    await load();
  }
}

/// Goldens are generated and compared on Linux only: other platforms
/// rasterise the same fonts slightly differently.
final goldenSkip = Platform.isLinux ? null : 'goldens are compared on Linux';

/// Pins the surface to [size] at DPR 1 for one test.
void setGoldenSurface(WidgetTester tester, Size size) {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

/// Golden image path for a lane-owned golden.
String goldenPath(String name) => 'files/$name.png';
