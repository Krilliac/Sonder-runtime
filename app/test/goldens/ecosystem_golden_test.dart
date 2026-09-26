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
/// (desk width) and down with an Ollama fallback (phone width).
void main() {
  setUpAll(loadGoldenFonts);

  final cases = {
    'ready_desk': (const Size(760, 1000), ecosystemReadySynthetic()),
    'fallback_phone': (const Size(390, 1150), ecosystemFallback()),
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
                  message: 'Opened the Observatory.',
                ),
              ),
            ),
          ),
        ));
        await tester.pumpAndSettle();
        await expectLater(find.byType(MaterialApp),
            matchesGoldenFile(goldenPath(name)));
      }, skip: goldenSkip != null);
    }
  }
}
