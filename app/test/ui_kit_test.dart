import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';

Widget _app(Widget home) => MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: SonderTheme.dark,
      home: home,
    );

List<SonderCategory> _categories() => [
      SonderCategory(
        id: 'overview',
        label: 'Overview',
        icon: Icons.space_dashboard_outlined,
        description: 'Health at a glance.',
        builder: (_) => const Text('overview body'),
      ),
      SonderCategory(
        id: 'models',
        label: 'Models',
        icon: Icons.memory_outlined,
        group: 'Inference',
        keywords: const ['context size'],
        badge: const CountBadge(3, semantic: '3 routes'),
        builder: (_) => const Text('models body'),
      ),
      SonderCategory(
        id: 'server',
        label: 'Server',
        icon: Icons.dns_outlined,
        group: 'Host',
        builder: (context) => TextButton(
          onPressed: () => CategoryNavigator.maybeOf(context)!.select('models'),
          child: const Text('open models'),
        ),
      ),
    ];

void _surface(WidgetTester tester, Size size) {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void main() {
  group('CategoryScaffold', () {
    testWidgets('wide: rail selects one page at a time', (tester) async {
      _surface(tester, const Size(1280, 800));
      final selected = <String>[];
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Runtime',
        categories: _categories(),
        onSelected: selected.add,
      )));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('category-page-overview')), findsOneWidget);
      expect(find.text('overview body'), findsOneWidget);
      expect(find.text('models body'), findsNothing);
      // Group headings are drawn once per run of categories.
      expect(find.text('INFERENCE'), findsOneWidget);
      expect(find.bySemanticsLabel(RegExp('Models.*3 routes', dotAll: true)),
          findsOneWidget);

      await tester.tap(find.byKey(const Key('category-models')));
      await tester.pumpAndSettle();
      expect(find.text('models body'), findsOneWidget);
      expect(find.text('overview body'), findsNothing);
      expect(selected, ['models']);
    });

    testWidgets('search filters the rail by label and keyword',
        (tester) async {
      _surface(tester, const Size(1280, 800));
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Settings',
        categories: _categories(),
      )));
      await tester.pumpAndSettle();
      await tester.enterText(
          find.byKey(const Key('category-search')), 'context');
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('category-models')), findsOneWidget);
      expect(find.byKey(const Key('category-server')), findsNothing);

      await tester.enterText(find.byKey(const Key('category-search')), 'zzz');
      await tester.pumpAndSettle();
      expect(find.text('No matches'), findsOneWidget);
    });

    testWidgets('a page can open another category', (tester) async {
      _surface(tester, const Size(1280, 800));
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Runtime',
        categories: _categories(),
        initialId: 'server',
      )));
      await tester.pumpAndSettle();
      await tester.tap(find.text('open models'));
      await tester.pumpAndSettle();
      expect(find.text('models body'), findsOneWidget);
    });

    testWidgets('narrow: list first, page with back to the list',
        (tester) async {
      _surface(tester, const Size(390, 844));
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Runtime',
        categories: _categories(),
      )));
      await tester.pumpAndSettle();
      expect(find.text('overview body'), findsNothing);
      expect(find.text('Health at a glance.'), findsOneWidget);

      await tester.tap(find.byKey(const Key('category-models')));
      await tester.pumpAndSettle();
      expect(find.text('models body'), findsOneWidget);

      await tester.tap(find.byTooltip('All runtime sections'));
      await tester.pumpAndSettle();
      expect(find.text('models body'), findsNothing);
      expect(find.byKey(const Key('category-server')), findsOneWidget);
    });

    testWidgets('narrow: a deep link opens the page directly', (tester) async {
      _surface(tester, const Size(390, 844));
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Runtime',
        categories: _categories(),
        initialId: 'models',
      )));
      await tester.pumpAndSettle();
      expect(find.text('models body'), findsOneWidget);
    });

    testWidgets('rail entries meet the tap-target guideline', (tester) async {
      _surface(tester, const Size(1280, 800));
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_app(CategoryScaffold(
        title: 'Runtime',
        categories: _categories(),
      )));
      await tester.pumpAndSettle();
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      handle.dispose();
    });
  });

  group('AsyncActionButton', () {
    testWidgets('shows progress, then done, then its label again',
        (tester) async {
      final gate = Completer<void>();
      await tester.pumpWidget(_app(Scaffold(
        body: Center(
          child: AsyncActionButton(
            label: 'Start server',
            busyLabel: 'Starting…',
            doneLabel: 'Started',
            onPressed: () => gate.future,
          ),
        ),
      )));
      await tester.tap(find.text('Start server'));
      await tester.pump();
      expect(find.text('Starting…'), findsOneWidget);
      // The spinner waits a beat so instant actions never flicker.
      expect(find.byType(CircularProgressIndicator), findsNothing);
      await tester.pump(const Duration(milliseconds: 200));
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      // A second tap while running does nothing.
      await tester.tap(find.text('Starting…'));
      gate.complete();
      await tester.pump();
      await tester.pump();
      expect(find.text('Started'), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      expect(find.text('Start server'), findsOneWidget);
    });

    testWidgets('a declined confirmation runs nothing and never looks busy',
        (tester) async {
      var answer = false;
      var runs = 0;
      await tester.pumpWidget(_app(Scaffold(
        body: Center(
          child: AsyncActionButton(
            label: 'Cancel work',
            doneLabel: 'Requested',
            confirm: () async => answer,
            onPressed: () async => runs++,
          ),
        ),
      )));
      await tester.tap(find.text('Cancel work'));
      await tester.pump(const Duration(milliseconds: 300));
      expect(runs, 0);
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.text('Cancel work'), findsOneWidget);
      answer = true;
      await tester.tap(find.text('Cancel work'));
      await tester.pump();
      await tester.pump();
      expect(runs, 1);
      expect(find.text('Requested'), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
    });

    testWidgets('a failure shows failed and reaches onError', (tester) async {
      Object? seen;
      await tester.pumpWidget(_app(Scaffold(
        body: Center(
          child: AsyncActionButton(
            label: 'Restart',
            onPressed: () async => throw StateError('launcher refused'),
            onError: (error, _) => seen = error,
          ),
        ),
      )));
      await tester.tap(find.text('Restart'));
      await tester.pump();
      await tester.pump();
      expect(find.text('Failed'), findsOneWidget);
      expect(seen, isA<StateError>());
      await tester.pump(const Duration(seconds: 3));
      expect(find.text('Restart'), findsOneWidget);
    });
  });

  group('rows', () {
    testWidgets('SwitchRow toggles from anywhere on the row', (tester) async {
      var value = false;
      await tester.pumpWidget(_app(Scaffold(
        body: StatefulBuilder(
          builder: (context, setState) => SettingsSection(
            title: 'Privacy',
            children: [
              SwitchRow(
                label: 'Allow approximate location',
                description: 'Only for location-dependent questions.',
                value: value,
                onChanged: (v) => setState(() => value = v),
              ),
            ],
          ),
        ),
      )));
      await tester.tap(find.text('Allow approximate location'));
      await tester.pump();
      expect(value, isTrue);
    });

    testWidgets('SettingRow stacks its control below narrow widths',
        (tester) async {
      Future<double> controlTop(double width) async {
        await tester.pumpWidget(_app(Scaffold(
          body: Center(
            child: SizedBox(
              width: width,
              child: const SettingRow(
                label: 'Context size',
                description: 'Requested conversation capacity.',
                trailing: SizedBox(
                    key: Key('control'), width: 120, height: 36),
              ),
            ),
          ),
        )));
        final label = tester.getTopLeft(find.text('Context size')).dy;
        return tester.getTopLeft(find.byKey(const Key('control'))).dy - label;
      }

      expect(await controlTop(700), lessThan(24));
      expect(await controlTop(360), greaterThan(30));
    });

    testWidgets('Meter states its value in words', (tester) async {
      await tester.pumpWidget(_app(const Scaffold(
        body: Meter(value: 0.5, label: 'Context', valueLabel: '4.1k of 8.2k'),
      )));
      await tester.pumpAndSettle();
      expect(find.bySemanticsLabel('Context: 4.1k of 8.2k'), findsOneWidget);
    });

    testWidgets('StatusPill and CountBadge always carry words',
        (tester) async {
      await tester.pumpWidget(_app(const Scaffold(
        body: Row(children: [
          StatusPill(StatusKind.warn, word: 'needs you'),
          CountBadge(2, semantic: '2 approvals waiting'),
        ]),
      )));
      expect(find.bySemanticsLabel('needs you'), findsOneWidget);
      expect(find.bySemanticsLabel('2 approvals waiting'), findsOneWidget);
    });
  });

  group('feedback', () {
    testWidgets('a toast leads with the status word', (tester) async {
      await tester.pumpWidget(_app(Scaffold(
        body: Builder(
          builder: (context) => TextButton(
            onPressed: () => showSonderToast(context, 'Link copied'),
            child: const Text('copy'),
          ),
        ),
      )));
      await tester.tap(find.text('copy'));
      await tester.pump();
      // Let the entrance finish: a fade at opacity 0 hides semantics.
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.text('Link copied'), findsOneWidget);
      expect(find.bySemanticsLabel(RegExp('ok: Link copied')), findsOneWidget);
    });

    testWidgets('StructuredFields reads as fields, clips, and keeps raw JSON',
        (tester) async {
      final value = {
        'path': 'src/render/pso_cache.cpp',
        'old_text': List.generate(6, (i) => 'line $i').join('\n'),
        'limit': 50,
        'dry_run': false,
        'note': null,
      };
      await tester.pumpWidget(_app(Scaffold(
        body: SingleChildScrollView(
          child: SizedBox(
            width: 640,
            child: StructuredFields(value, label: 'Arguments'),
          ),
        ),
      )));
      // One row per key; no JSON punctuation in the readable view.
      expect(find.text('path'), findsOneWidget);
      expect(find.text('src/render/pso_cache.cpp'), findsOneWidget);
      expect(find.text('50'), findsOneWidget);
      expect(find.text('false'), findsOneWidget);
      expect(find.textContaining('"path"'), findsNothing);
      // Long values are clipped behind an explicit control.
      expect(find.textContaining('line 5'), findsNothing);
      await tester.tap(find.text('Show all 6 lines'));
      await tester.pump();
      expect(find.textContaining('line 5'), findsOneWidget);
      expect(find.text('Show less'), findsOneWidget);
      // The exact value stays one tap away.
      await tester.tap(find.text('Raw JSON'));
      await tester.pump();
      expect(find.textContaining('"path": "src/render/pso_cache.cpp"'),
          findsOneWidget);
      expect(find.byTooltip('Copy arguments'), findsOneWidget);
    });

    testWidgets('RawOutput collapses long output behind Show all',
        (tester) async {
      final long = List.generate(30, (i) => 'line $i').join('\n');
      await tester.pumpWidget(_app(Scaffold(
        body: SingleChildScrollView(child: RawOutput(long)),
      )));
      expect(find.text('Show all 30 lines'), findsOneWidget);
      await tester.tap(find.text('Show all 30 lines'));
      await tester.pumpAndSettle();
      expect(find.text('Show less'), findsOneWidget);
    });
  });
}
