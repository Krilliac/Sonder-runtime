import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/ui/status_vocab.dart';
import 'package:sonder_runtime/workspace_ui.dart' show WorkspaceDestination;

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

    testWidgets('a long label wraps under large text instead of overflowing',
        (tester) async {
      await tester.pumpWidget(MediaQuery(
        data: const MediaQueryData(textScaler: TextScaler.linear(2)),
        child: _app(Scaffold(
          body: Center(
            child: SizedBox(
              width: 200,
              child: AsyncActionButton(
                label: 'Test host control',
                icon: Icons.power_settings_new_outlined,
                onPressed: () async {},
              ),
            ),
          ),
        )),
      ));
      expect(tester.takeException(), isNull);
      expect(find.text('Test host control'), findsOneWidget);
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

  group('ShellScope', () {
    ShellScope scope({
      required Widget child,
      ShellLeaveGuards? guards,
      bool sidebarVisible = true,
      VoidCallback? openNavigation,
      ShellSection? section,
    }) =>
        ShellScope(
          current: WorkspaceDestination.settings,
          sidebarVisible: sidebarVisible,
          navigate: (_) {},
          openNavigation: openNavigation ?? () {},
          section: section,
          leaveGuards: guards,
          child: child,
        );

    testWidgets('a leave guard registers while mounted and can keep the page',
        (tester) async {
      final guards = ShellLeaveGuards();
      var dirty = true;
      var asked = 0;
      Widget page(bool mounted) => _app(scope(
            guards: guards,
            child: mounted
                ? ShellLeaveGuard(
                    canLeave: () async {
                      asked++;
                      return !dirty;
                    },
                    child: const Text('settings'),
                  )
                : const Text('gone'),
          ));

      await tester.pumpWidget(page(true));
      expect(guards.isEmpty, isFalse);
      expect(await guards.canLeave(), isFalse);
      dirty = false;
      expect(await guards.canLeave(), isTrue);
      expect(asked, 2);

      // Unmounted pages no longer guard.
      await tester.pumpWidget(page(false));
      expect(guards.isEmpty, isTrue);
      expect(await guards.canLeave(), isTrue);
      expect(asked, 2);
    });

    testWidgets('a guard uses its latest callback, and is inert without a shell',
        (tester) async {
      final guards = ShellLeaveGuards();
      await tester.pumpWidget(_app(scope(
        guards: guards,
        child: ShellLeaveGuard(
            canLeave: () async => false, child: const SizedBox()),
      )));
      await tester.pumpWidget(_app(scope(
        guards: guards,
        child: ShellLeaveGuard(
            canLeave: () async => true, child: const SizedBox()),
      )));
      expect(await guards.canLeave(), isTrue);

      // No shell: the page still builds and nothing registers anywhere.
      await tester.pumpWidget(_app(ShellLeaveGuard(
          canLeave: () async => false, child: const Text('alone'))));
      expect(find.text('alone'), findsOneWidget);
      expect(guards.isEmpty, isTrue);
    });

    testWidgets('the menu button shows only where the sidebar is hidden',
        (tester) async {
      var opened = 0;
      Widget page({bool? sidebarVisible}) => _app(Builder(
            builder: (context) => sidebarVisible == null
                ? const Scaffold(body: ShellMenuButton())
                : scope(
                    sidebarVisible: sidebarVisible,
                    openNavigation: () => opened++,
                    child: const Scaffold(body: ShellMenuButton()),
                  ),
          ));

      await tester.pumpWidget(page());
      expect(find.byTooltip('Open navigation'), findsNothing);
      await tester.pumpWidget(page(sidebarVisible: true));
      expect(find.byTooltip('Open navigation'), findsNothing);
      await tester.pumpWidget(page(sidebarVisible: false));
      await tester.tap(find.byTooltip('Open navigation'));
      expect(opened, 1);
    });

    testWidgets('each section request is a new object pages can tell apart',
        (tester) async {
      _sectionsSeen.clear();
      Widget page(ShellSection? section) =>
          _app(scope(section: section, child: const _SectionProbe()));
      final first = ShellSection('connection');
      await tester.pumpWidget(page(first));
      // The same request again: nothing to apply, no rebuild.
      await tester.pumpWidget(page(first));
      expect(_sectionsSeen, hasLength(1));
      // Asking for the same section again is a new request.
      await tester.pumpWidget(page(ShellSection('connection')));
      expect(_sectionsSeen, hasLength(2));
      expect(_sectionsSeen.last!.id, 'connection');
      expect(identical(_sectionsSeen.first, _sectionsSeen.last), isFalse);
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

    testWidgets('a toast works under a theme without floating snack bars',
        (tester) async {
      _surface(tester, const Size(1200, 800));
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => TextButton(
              onPressed: () => showSonderToast(context, 'Settings saved'),
              child: const Text('save'),
            ),
          ),
        ),
      ));
      await tester.tap(find.text('save'));
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(find.text('Settings saved'), findsOneWidget);
    });

    testWidgets('an outcome lines its detail up under its title',
        (tester) async {
      await tester.pumpWidget(_app(Scaffold(
        body: Padding(
          padding: const EdgeInsets.all(16),
          child: OutcomeView(
            const ActionOutcome.failed('Revocation not confirmed.',
                detail: 'Retry Sign out, or forget the session.'),
            onDismiss: () {},
          ),
        ),
      )));
      final title = tester.getRect(find.text('Revocation not confirmed.'));
      final detail =
          tester.getRect(find.text('Retry Sign out, or forget the session.'));
      final dismiss = tester.getRect(find.byTooltip('Dismiss'));
      expect(detail.left, title.left);
      // The detail follows the title, not the 48 dp dismiss button.
      expect(detail.top - title.bottom, lessThan(8));
      expect((title.center.dy - dismiss.center.dy).abs(), lessThan(4));
      expect(find.bySemanticsLabel('error: Revocation not confirmed.'),
          findsOneWidget);
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

final _sectionsSeen = <ShellSection?>[];

/// Records the section each time ShellScope makes it rebuild.
class _SectionProbe extends StatelessWidget {
  const _SectionProbe();

  @override
  Widget build(BuildContext context) {
    _sectionsSeen.add(ShellScope.maybeOf(context)!.section);
    return const SizedBox();
  }
}
