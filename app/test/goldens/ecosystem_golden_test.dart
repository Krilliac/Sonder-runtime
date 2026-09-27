@Tags(['golden'])
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/theme.dart';

import '../runtime_fixtures.dart';
import 'golden_fonts.dart';

/// The Inference & Observatory panel: ready on the synthetic mock backend
/// after Open Observatory (desk width, launch result shown) and down with an
/// Ollama fallback (phone width). Both surfaces are tall enough to include
/// the action row. Checked times render in UTC, so these do not depend on
/// the host time zone.
void main() {
  setUpAll(loadGoldenFonts);

  final cases = {
    'ready_desk': (const Size(760, 1180), ecosystemReadySynthetic(), true),
    'fallback_phone': (const Size(390, 1240), ecosystemFallback(), false),
  };
  final themes = {'dark': SonderTheme.dark, 'light': SonderTheme.light};

  for (final entry in cases.entries) {
    for (final theme in themes.entries) {
      final name = 'ecosystem_${entry.key}_${theme.key}';
      testWidgets(name, (tester) async {
        setGoldenSurface(tester, entry.value.$1);
        await tester.pumpWidget(MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: theme.value,
          home: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(16),
              child: EcosystemPanel(
                reading: EcosystemReading.parse(
                    jsonDecode(jsonEncode(entry.value.$2))),
                runtimeUrl: 'http://127.0.0.1:11435',
                onLaunch: (_) async => const ObservatoryLaunchResult(
                  ok: true,
                  mode: ObservatoryLaunchMode.executable,
                  message: 'Opened the Observatory with 2 producers.',
                ),
              ),
            ),
          ),
        ));
        await tester.pumpAndSettle();
        if (entry.value.$3) {
          await tester.tap(find.byKey(const Key('ecosystem-open-observatory')));
          await tester.pumpAndSettle();
        }
        await expectLater(
            find.byType(MaterialApp), matchesGoldenFile(goldenPath(name)));
      }, skip: goldenSkip != null);
    }
  }
}
