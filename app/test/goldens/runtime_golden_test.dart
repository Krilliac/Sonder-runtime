@Tags(['golden'])
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

import '../runtime_fixtures.dart';
import 'golden_fonts.dart';

void main() {
  setUpAll(loadGoldenFonts);

  const sizes = {
    'phone': Size(390, 844),
    'desk': Size(1440, 900),
  };
  final themes = {'dark': SonderTheme.dark, 'light': SonderTheme.light};

  for (final size in sizes.entries) {
    for (final theme in themes.entries) {
      testWidgets('runtime_overview_${size.key}_${theme.key}', (tester) async {
        setGoldenSurface(tester, size.value);
        await tester.pumpWidget(MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: theme.value,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
            initialInfo: healthySystemInfo(),
            liveUpdates: false,
            now: runtimeNow,
            dataSource: FakeRuntimeData(
              runs: [runningWorkRun()],
              approvalsPage: const ApprovalsPage(supported: true, pending: [
                PendingApproval(
                    callId: '3f9a12c0',
                    tool: 'write_file',
                    preview: 'src/render/pso_cache.cpp'),
              ]),
            ),
          ),
        ));
        await tester.pumpAndSettle();
        await expectLater(
            find.byType(MaterialApp),
            matchesGoldenFile(
                goldenPath('runtime_overview_${size.key}_${theme.key}')));
      });
    }
  }
}
