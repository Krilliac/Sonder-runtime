// The Runtime "Host developer tools" section: lazy load, grouping, the
// admin-only/busy/too-large states and Rediscover, fed with the server's own
// inventory bodies.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/host_tools_panel.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/runtime/status_word.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

import 'fixtures/server_fixtures.dart';
import 'runtime_fixtures.dart';

ToolInventory inventoryFixture(String name) => ToolInventory.fromJson(
    jsonDecode(serverFixture(name)) as Map<String, dynamic>);

ToolInventory _byCategory(String? category) => category == 'compiler'
    ? inventoryFixture('tool_inventory_compiler_200.json')
    : inventoryFixture('tool_inventory_200.json');

Future<void> _pumpPanel(WidgetTester tester, FakeRuntimeData data,
    {bool expanded = true}) async {
  tester.view.physicalSize = const Size(900, 1400);
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(MaterialApp(
    theme: SonderTheme.dark,
    home: Scaffold(
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: HostToolsPanel(source: data, initiallyExpanded: expanded),
      ),
    ),
  ));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('loads only when opened, then groups tools by category',
      (tester) async {
    final data = FakeRuntimeData(toolInventoryFor: _byCategory);
    await _pumpPanel(tester, data, expanded: false);
    expect(data.toolInventoryReads, isEmpty);
    await tester.tap(find.text('Host tools · Details'));
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, [null]);

    expect(find.text('Linux · x86_64 · 8 tools · checked 12m ago'),
        findsOneWidget);
    for (final heading in [
      'Compilers · 2',
      'Build systems · 2',
      'Test runners · 1',
      'Debuggers & profilers · 1',
      'Runtimes · 1',
      'Version control · 1',
    ]) {
      expect(find.text(heading), findsOneWidget, reason: heading);
    }
    expect(
        find.text('gcc 13.2.0 · on PATH · +1 other install'), findsOneWidget);
    expect(
        find.text('clang · version probe timed out · on PATH'), findsOneWidget);
    expect(
        find.text('pytest · project-local, not probed · known install folder'),
        findsOneWidget);
    expect(find.text('~/project/.venv/bin/pytest'), findsOneWidget);
    expect(find.text('skipped 1 relative or invalid PATH entries'),
        findsOneWidget);
    // Stale warning only when the server says so.
    expect(find.textContaining('older than its refresh window'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('status marks: version known ok, probe failure warn, else note',
      (tester) async {
    final inventory = inventoryFixture('tool_inventory_200.json');
    StatusKind of(String name) =>
        hostToolStatus(inventory.tools.firstWhere((t) => t.name == name));
    expect(of('gcc'), StatusKind.ok);
    expect(of('clang'), StatusKind.warn);
    expect(of('gdb'), StatusKind.warn);
    expect(of('ninja'), StatusKind.note);
    expect(of('pytest'), StatusKind.note);
  });

  testWidgets('a stale snapshot says so', (tester) async {
    final data = FakeRuntimeData(
        toolInventoryFor: (_) =>
            inventoryFixture('tool_inventory_stale_200.json'));
    await _pumpPanel(tester, data);
    expect(
        find.textContaining('older than its refresh window'), findsOneWidget);
  });

  testWidgets('403 reads as n/a with no Rediscover', (tester) async {
    final data = FakeRuntimeData(
        toolInventoryError: SonderException(
            'Only an administrator can read the host tool inventory.',
            httpStatus: 403,
            code: 'FORBIDDEN'));
    await _pumpPanel(tester, data);
    expect(find.text('Needs an administrator account.'), findsOneWidget);
    expect(find.byKey(const Key('host-tools-rediscover')), findsNothing);
  });

  testWidgets('404 reads as not available on this server', (tester) async {
    final data = FakeRuntimeData(
        toolInventoryError: SonderException('gone', httpStatus: 404));
    await _pumpPanel(tester, data);
    expect(find.text('Not available on this server.'), findsOneWidget);
  });

  testWidgets('busy (429) is a warning with Retry', (tester) async {
    final data = FakeRuntimeData(
        toolInventoryError: SonderException(
            'The server is already reading its tool inventory. '
            'Try again in a moment.',
            httpStatus: 429,
            code: 'TOOL_INVENTORY_BUSY'));
    await _pumpPanel(tester, data);
    expect(find.textContaining('already reading its tool inventory'),
        findsOneWidget);
    expect(find.text('warn'), findsOneWidget);
    data.toolInventoryError = null;
    data.toolInventoryFor = _byCategory;
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, [null, null]);
    expect(find.text('Compilers · 2'), findsOneWidget);
  });

  testWidgets('too large (413) asks for a category, then reads filtered',
      (tester) async {
    final data = FakeRuntimeData(
        toolInventoryError: SonderException(
            'Too many tools to list at once. Pick a category.',
            httpStatus: 413,
            code: 'TOOL_INVENTORY_TOO_LARGE'));
    await _pumpPanel(tester, data);
    expect(find.text('Too many tools to list at once. Pick a category.'),
        findsOneWidget);
    data.toolInventoryError = null;
    data.toolInventoryFor = _byCategory;
    await tester.tap(find.byKey(const Key('host-tools-category')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Compilers').last);
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, [null, 'compiler']);
    expect(find.text('Compilers · 2'), findsOneWidget);
    expect(find.text('Version control · 1'), findsNothing);
  });

  testWidgets('Rediscover refreshes, then re-reads the chosen category',
      (tester) async {
    final data = FakeRuntimeData(
      toolInventoryFor: _byCategory,
      rediscovered: inventoryFixture('tool_inventory_200.json'),
    );
    await _pumpPanel(tester, data);
    await tester.tap(find.byKey(const Key('host-tools-category')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Compilers (2)').last);
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, [null, 'compiler']);
    await tester.tap(find.text('Rediscover'));
    await tester.pumpAndSettle();
    expect(data.rediscoveries, 1);
    expect(data.toolInventoryReads, [null, 'compiler', 'compiler']);
    expect(find.text('Version control · 1'), findsNothing);
  });

  testWidgets('a failed Rediscover shows the error and keeps Retry',
      (tester) async {
    final data = FakeRuntimeData(
      toolInventoryFor: _byCategory,
      rediscoverError: SonderException(
          'This server has no host tool inventory right now.',
          httpStatus: 503,
          code: 'TOOL_INVENTORY_UNAVAILABLE'),
    );
    await _pumpPanel(tester, data);
    await tester.tap(find.text('Rediscover'));
    await tester.pumpAndSettle();
    expect(find.text('This server has no host tool inventory right now.'),
        findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
  });

  testWidgets('Runtime screen: rail items reach unbuilt sections both ways',
      (tester) async {
    final data = FakeRuntimeData(toolInventoryFor: _byCategory);
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
        initialInfo: healthySystemInfo(),
        liveUpdates: false,
        dataSource: data,
        now: runtimeNow,
      ),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Host tools').first);
    await tester.pumpAndSettle();
    expect(find.text('Host developer tools'), findsOneWidget);
    await tester.ensureVisible(find.text('Host tools · Details'));
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, isEmpty);
    await tester.tap(find.text('Host tools · Details'));
    await tester.pumpAndSettle();
    expect(data.toolInventoryReads, [null]);
    await tester.ensureVisible(find.text('Compilers · 2'));
    expect(find.text('Compilers · 2'), findsOneWidget);
    // Jumping back up reaches sections the lazy list has since dropped.
    await tester.tap(find.text('Work runs').first);
    await tester.pumpAndSettle();
    expect(find.text('Work runs (0)').hitTestable(), findsOneWidget);
    await tester.tap(find.text('Jobs').first);
    await tester.pumpAndSettle();
    expect(find.text('Jobs, fanout & compute').hitTestable(), findsOneWidget);
  });
}
