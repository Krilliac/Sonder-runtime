@Tags(['golden'])
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/host_tools_panel.dart';
import 'package:sonder_runtime/theme.dart';

import '../fixtures/server_fixtures.dart';
import '../runtime_fixtures.dart';
import 'golden_fonts.dart';

void main() {
  setUpAll(loadGoldenFonts);

  const sizes = {
    'phone': Size(390, 980),
    'desk': Size(900, 820),
  };
  final themes = {'dark': SonderTheme.dark, 'light': SonderTheme.light};
  final inventory = ToolInventory.fromJson(
      jsonDecode(serverFixture('tool_inventory_200.json'))
          as Map<String, dynamic>);

  for (final size in sizes.entries) {
    for (final theme in themes.entries) {
      final name = 'host_tools_${size.key}_${theme.key}';
      testWidgets(name, (tester) async {
        setGoldenSurface(tester, size.value);
        await tester.pumpWidget(MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: theme.value,
          home: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(16),
              child: HostToolsPanel(
                source: FakeRuntimeData(toolInventoryFor: (_) => inventory),
                initiallyExpanded: true,
              ),
            ),
          ),
        ));
        await tester.pumpAndSettle();
        await expectLater(
            find.byType(MaterialApp), matchesGoldenFile(goldenPath(name)));
      }, skip: goldenSkip != null);
    }
  }
}
