import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/runtime/overview.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/runtime/status_word.dart';
import 'package:sonder_runtime/runtime/work_runs_panel.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/system_screen.dart' as legacy;
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/ui/status_row.dart';
import 'package:sonder_runtime/workspace_ui.dart' show WorkspaceDestination;

import 'runtime_fixtures.dart';
import 'runtime_rich_fixture.dart';

Future<void> pumpRuntime(
  WidgetTester tester, {
  SystemInfo? info,
  FakeRuntimeData? data,
  Size size = const Size(1280, 1000),
  ThemeData? theme,
  String? category,
  Settings? settings,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(MaterialApp(
    theme: theme ?? SonderTheme.dark,
    home: RuntimeScreen(
      settings: settings ?? Settings(serverUrl: 'http://192.168.1.20:11435'),
      initialInfo: info,
      liveUpdates: false,
      dataSource: data ?? FakeRuntimeData(),
      now: runtimeNow,
      initialCategory: category,
    ),
  ));
  await tester.pumpAndSettle();
}

/// Opens a category from the wide rail.
Future<void> openCategory(WidgetTester tester, String id) async {
  await tester.tap(find.byKey(Key('category-$id')));
  await tester.pumpAndSettle();
}

/// Lets real I/O (LocalManager.inspect, MockClient) finish between frames.
Future<void> settleLive(WidgetTester tester) async {
  for (var i = 0; i < 40; i++) {
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pump(const Duration(milliseconds: 50));
    if (i > 4 &&
        find.byKey(const Key('runtime-refreshing')).evaluate().isEmpty) {
      break;
    }
  }
  await pumpFrames(tester);
}

/// The live screen polls every 2 s, so it never "settles"; pump a few
/// frames instead (enough for dialogs and page switches).
Future<void> pumpFrames(WidgetTester tester) async {
  for (var i = 0; i < 6; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

/// A status mark that reads as a problem (error or warn).
final _problemMarks = find.byWidgetPredicate((widget) =>
    widget is StatusMark &&
    (widget.kind == StatusKind.fail ||
        widget.kind == StatusKind.warn ||
        widget.kind == StatusKind.refused));

ButtonStyleButton _button(WidgetTester tester, Finder label) =>
    tester.widget<ButtonStyleButton>(find.ancestor(
        of: label, matching: find.bySubtype<ButtonStyleButton>()));

const _digest =
    '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08';

PendingApproval _pending({String callId = '3f9a12c0d1e2f3a4'}) =>
    PendingApproval(
      callId: callId,
      digest: _digest,
      tool: 'write_file',
      preview: 'path: src/render/pso_cache.cpp',
      mode: 'manual',
      refusedAt: runtimeNow.subtract(const Duration(minutes: 2)),
    );

void main() {
  // These fixtures use a plain-HTTP LAN host with a key: the person has
  // explicitly allowed it (see cleartext_key_test.dart for the refusal).
  setUpAll(() => CleartextKeyPolicy.allowOnly(['pc.test:11435']));
  tearDownAll(() => CleartextKeyPolicy.allowOnly(const []));

  group('status vocabulary mirrors style.py', () {
    test('glyphs and words', () {
      // style.py _UNICODE_GLYPHS + NOTICE_KINDS and plan §2.1.
      expect(StatusKind.ok.glyph, '✓');
      expect(StatusKind.fail.glyph, '✗');
      expect(StatusKind.fail.word, 'error');
      expect(StatusKind.refused.glyph, '⊘');
      expect(StatusKind.warn.glyph, '!');
      expect(StatusKind.skipped.glyph, '–');
      // The shared word is style.py's "skipped"; Runtime shows the "off"
      // synonym from the same row (plan §2.1, §2.4).
      expect(StatusKind.skipped.word, 'skipped');
      expect(StatusKind.skipped.runtimeWord, 'off');
      expect(StatusKind.ok.runtimeWord, 'ok');
      expect(StatusKind.note.glyph, '·');
      expect(StatusKind.running.glyph, '◈');
      expect(StatusKind.running.word, 'working');
      expect(StatusKind.skipped.isProblem, isFalse);
      expect(StatusKind.refused.isProblem, isTrue);
    });
  });

  group('overview rows', () {
    test('healthy server with a running work run and pending approval', () {
      final rows = overviewRows(
        serverUrl: 'http://192.168.1.20:11435',
        info: healthySystemInfo(),
        offline: false,
        workRuns: [runningWorkRun()],
        approvals: const ApprovalsPage(supported: true, pending: [
          PendingApproval(callId: '3f9a12c0', tool: 'write_file'),
        ]),
        now: runtimeNow,
      );
      final byLabel = {for (final row in rows) row.label: row};
      expect(byLabel['Server']!.status, StatusKind.ok);
      expect(
          byLabel['Server']!.value, startsWith('192.168.1.20:11435 · ready'));
      expect(byLabel['Models']!.value, 'sonder:latest, code');
      expect(byLabel['Approvals']!.status, StatusKind.warn);
      expect(byLabel['Approvals']!.value, '1 call waiting');
      expect(byLabel['Approvals']!.actionLabel, 'Review');
      expect(byLabel['Work runs']!.status, StatusKind.running);
      expect(byLabel['Work runs']!.value, '1 running · wr-7c1e… 4m');
      expect(byLabel['Autopilot']!.status, StatusKind.skipped);
      expect(byLabel['Autopilot']!.value, 'off');
      expect(byLabel['Agents']!.value, '1 running');
    });

    test('tiles carry a headline, a detail and the page that owns them', () {
      final rows = overviewRows(
        serverUrl: 'http://192.168.1.20:11435',
        info: healthySystemInfo(),
        offline: false,
        workRuns: [runningWorkRun()],
        approvals: const ApprovalsPage(supported: true, pending: [
          PendingApproval(callId: '3f9a12c0', tool: 'write_file'),
        ]),
        now: runtimeNow,
      );
      final byLabel = {for (final row in rows) row.label: row};
      expect(byLabel['Server']!.headline, 'Connected');
      expect(byLabel['Server']!.category, 'server');
      expect(byLabel['Approvals']!.headline, '1 waiting');
      expect(byLabel['Approvals']!.word, 'needs you');
      expect(byLabel['Approvals']!.detail, 'write_file');
      expect(byLabel['Approvals']!.category, 'permissions');
      expect(byLabel['Work runs']!.headline, '1 running');
      expect(byLabel['Work runs']!.detail, 'wr-7c1e… · 4m of 30m');
      expect(byLabel['Work runs']!.category, 'activity');
      expect(byLabel['Models']!.category, 'models');
    });

    test('offline keeps a word, never a green dot', () {
      final rows = overviewRows(
          serverUrl: 'http://mypc.local:11435', info: null, offline: true);
      expect(rows.single.status, StatusKind.fail);
      expect(rows.single.value, "Can't reach mypc.local:11435");
      expect(rows.single.headline, 'Offline');
    });

    test('403 on work runs reads as off-by-design, not failure', () {
      final rows = overviewRows(
        serverUrl: 'http://127.0.0.1:11435',
        info: healthySystemInfo(),
        offline: false,
        workRunsError: SonderException('forbidden', httpStatus: 403),
        approvals: const ApprovalsPage(supported: false),
      );
      final byLabel = {for (final row in rows) row.label: row};
      expect(byLabel['Work runs']!.status, StatusKind.skipped);
      expect(byLabel['Approvals']!.status, StatusKind.skipped);
      expect(rows.where((row) => row.status.isProblem), isEmpty);
    });

    test('recent activity is newest first, capped at five, with words', () {
      final lines = recentActivity(healthySystemInfo().executionFeed);
      expect(lines.map((line) => line.word), ['done', 'refused', 'error']);
      expect(lines.first.text, 'Model sonder:latest · 61.2s');
      expect(lines[1].text, '/write src/render/pso_cache.cpp (manual)');
      // Event times are shown on the viewer's clock.
      expect(lines.first.time, '12:41');
    });

    test('compact durations and counts', () {
      expect(compactDuration(const Duration(seconds: 42)), '42s');
      expect(compactDuration(const Duration(minutes: 4, seconds: 12)), '4m');
      expect(compactDuration(const Duration(hours: 3, minutes: 12)), '3h 12m');
      expect(compactCount(812), '812');
      expect(compactCount(2100), '2.1k');
      expect(compactCount(41200), '41.2k');
      expect(compactCount(1200000), '1.2M');
    });
  });

  testWidgets('Runtime title, one page per category, Overview first',
      (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo());
    expect(find.text('Runtime'), findsOneWidget);
    expect(find.text('System'), findsNothing);
    expect(find.byKey(const Key('runtime-nav')), findsOneWidget);
    for (final id in [
      'overview',
      'activity',
      'models',
      'memory',
      'permissions',
      'server',
      'observatory',
      'cluster',
      'developer',
      'about',
    ]) {
      expect(find.byKey(Key('category-$id')), findsOneWidget, reason: id);
    }
    // Updates & extensions are admin-only: hidden until the server reports
    // them.
    expect(find.byKey(const Key('category-updates')), findsNothing);
    expect(find.byKey(const Key('category-page-overview')), findsOneWidget);
    expect(find.byKey(const Key('runtime-overview')), findsOneWidget);
    // One page at a time: the Activity page is not built under Overview.
    expect(find.byKey(const Key('work-runs-panel')), findsNothing);
    // The compatibility name still resolves to the same screen type.
    expect(find.byType(legacy.SystemScreen), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('the rail shows where you are and opens one page',
      (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo());
    await openCategory(tester, 'activity');
    expect(find.byKey(const Key('category-page-activity')), findsOneWidget);
    expect(find.byKey(const Key('category-page-overview')), findsNothing);
    final selected =
        tester.widget<HoverSurface>(find.byKey(const Key('category-activity')));
    expect(selected.selected, isTrue);
    expect(
        tester
            .widget<HoverSurface>(find.byKey(const Key('category-overview')))
            .selected,
        isFalse);
  });

  testWidgets('fresh single-PC server shows no problems', (tester) async {
    final info = SystemInfo.fromJson({
      'status': 'ready',
      'models': const [],
      'deployment': {
        'profile': 'single-pc',
        'capabilities': {
          'automatic_takeover': {'available': false, 'reason': 'n/a'},
          'automatic_failback': {'available': false, 'reason': 'n/a'},
        },
      },
      'operational_capabilities': {
        'schema_version': 1,
        'mobility': {
          'automatic_takeover_available': false,
          'automatic_failback_available': false,
        },
      },
    });
    await pumpRuntime(tester, info: info, category: 'cluster');
    // Off by design reads "– off", never a problem mark.
    expect(_problemMarks, findsNothing);
    expect(find.text('off'), findsWidgets);
    // Takeover/failback are listed once (Deployment), not twice.
    expect(find.text('Automatic failback'), findsOneWidget);
    await openCategory(tester, 'server');
    expect(_problemMarks, findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('work runs list, stop asks first, then posts once',
      (tester) async {
    final data = FakeRuntimeData(runs: [
      runningWorkRun(),
      WorkRun(
        id: 'wr-aaaa0000000000000000000000000002',
        status: 'returned',
        createdAt: runtimeNow.subtract(const Duration(minutes: 30)),
        updatedAt: runtimeNow.subtract(const Duration(minutes: 12)),
      ),
    ]);
    await pumpRuntime(tester,
        info: healthySystemInfo(), data: data, category: 'activity');
    expect(find.byKey(const Key('work-runs-panel')), findsOneWidget);
    expect(find.text('1 running · 2 recent'), findsOneWidget);
    expect(find.textContaining('4m of 30m budget'), findsOneWidget);
    expect(find.textContaining('12m ago'), findsOneWidget);
    await tester.tap(find.text('Stop…'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Stop work run'), findsOneWidget);
    await tester.tap(find.text('Keep running'));
    await tester.pumpAndSettle();
    expect(data.cancelled, isEmpty);
    await tester.tap(find.text('Stop…'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Stop run'));
    await tester.pumpAndSettle();
    expect(data.cancelled, ['wr-7c1e0000000000000000000000000001']);
    // The result sits under the run it stopped.
    final row =
        find.byKey(const Key('work-run-wr-7c1e0000000000000000000000000001'));
    expect(
        find.descendant(
            of: row, matching: find.textContaining('Stop requested')),
        findsOneWidget);
  });

  testWidgets('a Stop in flight disables its button: a second sends nothing',
      (tester) async {
    final gate = Completer<void>();
    final data = FakeRuntimeData(runs: [runningWorkRun()])
      ..cancelGate = gate.future;
    await pumpRuntime(tester,
        info: healthySystemInfo(), data: data, category: 'activity');
    await tester.tap(find.text('Stop…'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Stop run'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    expect(data.cancelled, hasLength(1));
    // The row reads "Stopping…" and its button takes no second press.
    expect(find.text('Stop…'), findsNothing);
    expect(_button(tester, find.text('Stopping…')).onPressed, isNull);
    await tester.tap(find.text('Stopping…'), warnIfMissed: false);
    await tester.pump();
    expect(data.cancelled, hasLength(1));
    gate.complete();
    await tester.pumpAndSettle();
    expect(find.textContaining('Stop requested'), findsOneWidget);
  });

  testWidgets('work runs: empty state and 403 role notice', (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo(), category: 'activity');
    expect(find.text('No work runs'), findsOneWidget);
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'activity',
        data: FakeRuntimeData(
            runsError: SonderException('forbidden', httpStatus: 403)));
    expect(find.text('Work runs need a developer or admin account.'),
        findsOneWidget);
  });

  testWidgets('Overview tiles open the page that owns them (phone)',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(runs: [runningWorkRun()]),
        size: const Size(390, 844),
        category: 'overview');
    expect(find.byKey(const Key('work-runs-panel')), findsNothing);
    final tile = find.byKey(const Key('overview-tile-work-runs'));
    await tester.ensureVisible(tile);
    await tester.pumpAndSettle();
    await tester.tap(tile);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-runs-panel')), findsOneWidget);
    // Back goes to the list of categories, not to Chat.
    await tester.tap(find.byTooltip('All runtime sections'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('runtime-nav')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Recent activity "All" opens Activity', (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo());
    expect(find.text('Model sonder:latest · 61.2s'), findsOneWidget);
    await tester.tap(find.text('All'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('category-page-activity')), findsOneWidget);
  });

  testWidgets('lists load only when opened; 403 is n/a', (tester) async {
    final data = FakeRuntimeData(
      jobList: [
        JobSummary(
            id: 'job-1',
            kind: 'index',
            status: 'running',
            updatedAt: runtimeNow.subtract(const Duration(minutes: 1))),
      ],
      fanoutList: const [
        FanoutSummary(
            id: 'fan-1', status: 'completed', selected: 3, answered: 3),
      ],
      computeError: SonderException('forbidden', httpStatus: 403),
    );
    await pumpRuntime(tester,
        info: healthySystemInfo(), data: data, category: 'activity');
    expect(data.jobReads, 0);
    final jobs = find.byKey(const Key('jobs-details'));
    await tester.ensureVisible(jobs);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Recent jobs'));
    await tester.pumpAndSettle();
    expect(data.jobReads, 1);
    expect(find.textContaining('job-1 · 1m ago'), findsOneWidget);

    await openCategory(tester, 'models');
    final fanout = find.byKey(const Key('fanout-details'));
    await tester.ensureVisible(fanout);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Recent fanout runs'));
    await tester.pumpAndSettle();
    expect(find.textContaining('3/3 answered'), findsOneWidget);

    await openCategory(tester, 'cluster');
    await tester.tap(find.text('Nodes'));
    await tester.pumpAndSettle();
    expect(find.text('Needs an administrator account.'), findsOneWidget);
  });

  testWidgets('Observatory page opens the Observatory; Models shows providers',
      (tester) async {
    final data = FakeRuntimeData(
        ecosystemReading: EcosystemReading.parse(ecosystemReadySynthetic()));
    final launched = <List<String>>[];
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
        initialInfo: healthySystemInfo(),
        liveUpdates: false,
        dataSource: data,
        now: runtimeNow,
        observatoryLauncher: (urls) async {
          launched.add(urls);
          return const ObservatoryLaunchResult(
            ok: true,
            mode: ObservatoryLaunchMode.executable,
            message: 'Opened the Observatory with 2 producers.',
          );
        },
      ),
    ));
    await tester.pumpAndSettle();
    expect(data.ecosystemReads, 1);
    await openCategory(tester, 'models');
    expect(find.byKey(const Key('ecosystem-inference')), findsOneWidget);
    expect(find.text('SYNTHETIC'), findsOneWidget);
    // Providers live on Models, the export on Observatory.
    expect(find.byKey(const Key('ecosystem-observatory')), findsNothing);
    await openCategory(tester, 'observatory');
    expect(find.byKey(const Key('ecosystem-panel')), findsOneWidget);
    expect(find.byKey(const Key('ecosystem-inference')), findsNothing);
    await tester.tap(find.byKey(const Key('ecosystem-open-observatory')));
    await tester.pumpAndSettle();
    expect(
        launched.single, ['http://127.0.0.1:11435', 'http://127.0.0.1:11437']);
    expect(
        find.text('Opened the Observatory with 2 producers.'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Models names the provider each route is bound to',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'models',
        data: FakeRuntimeData(
            ecosystemReading: EcosystemReading.parse(
                ecosystemJson(inference: inferenceStatusJson()))));
    final code = find.byKey(const Key('model-row-code'));
    expect(find.descendant(of: code, matching: find.text('Sonder Inference')),
        findsOneWidget);
    expect(
        find.descendant(of: code, matching: find.text('local')), findsNothing);
    expect(find.textContaining('Sonder Inference serves'), findsOneWidget);
    expect(find.textContaining('Ollama hosts and runs the local model weights'),
        findsNothing);
  });

  testWidgets('Models claims only the routes the runtime offers',
      (tester) async {
    // healthySystemInfo offers the `code` route only; every tier is bound.
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'models',
        data: FakeRuntimeData(
            ecosystemReading: EcosystemReading.parse(
                ecosystemJson(inference: inferenceStatusJson()))));
    expect(
        find.textContaining(
            'Sonder Inference serves the code route with mock:tiny.'),
        findsOneWidget);
    // The provider bindings still list every configured tier; the routes
    // explanation claims only the offered ones.
    final panel = tester
        .widget<Text>(find.textContaining('Sonder Runtime routes requests'));
    expect(panel.data, isNot(contains('reasoning')));
    expect(panel.data, isNot(contains('vision')));
    expect(find.byKey(const Key('ecosystem-tier-reasoning')), findsOneWidget);
  });

  testWidgets('Models falls back to /v1/models rows for non-admins',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'models',
        data: FakeRuntimeData(
            ecosystemError: SonderException(SonderApi.adminRequiredMessage,
                httpStatus: 403))
          ..catalog = const ModelCatalog(ids: [
            'sonder',
            'code'
          ], origins: {
            'code': ModelOrigin(
                kind: 'route',
                provider: 'sonder_inference',
                servedModel: 'qwen3:14b'),
          }));
    final code = find.byKey(const Key('model-row-code'));
    expect(find.descendant(of: code, matching: find.text('Sonder Inference')),
        findsOneWidget);
    expect(
        find.textContaining(
            'Sonder Inference serves the code route with qwen3:14b.'),
        findsOneWidget);
    expect(find.byKey(const Key('ecosystem-admin-required')), findsOneWidget);
  });

  testWidgets('Models keeps the old wording when all is on Ollama',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'models',
        data: FakeRuntimeData(
            ecosystemReading: EcosystemReading.parse(ecosystemAllOllama())));
    final code = find.byKey(const Key('model-row-code'));
    expect(find.descendant(of: code, matching: find.text('local')),
        findsOneWidget);
    expect(find.textContaining('Ollama hosts and runs the local model weights'),
        findsOneWidget);
  });

  group('local server row', () {
    test('launcher-detected server reads Reachable', () {
      expect(
          localServerRow(
              launcherDetected: true,
              serverUrl: 'http://127.0.0.1:11435',
              connected: true),
          ('Reachable on 127.0.0.1:11435', true));
    });
    test('connected to 127.0.0.1:11435 without the launcher identity', () {
      expect(
          localServerRow(
              launcherDetected: false,
              serverUrl: 'http://127.0.0.1:11435',
              connected: true),
          ('Connected on 127.0.0.1:11435 (not launcher-managed)', true));
      expect(
          localServerRow(
              launcherDetected: false,
              serverUrl: 'http://localhost:11435/',
              connected: true),
          ('Connected on 127.0.0.1:11435 (not launcher-managed)', true));
    });
    test('not connected, or connected elsewhere, is Not detected', () {
      for (final (url, connected) in [
        ('http://127.0.0.1:11435', false),
        ('http://192.168.1.20:11435', true),
        ('http://127.0.0.1:8080', true),
      ]) {
        expect(
            localServerRow(
                launcherDetected: false, serverUrl: url, connected: connected),
            ('Not detected on 127.0.0.1:11435', false));
      }
    });
  });

  testWidgets('ecosystem 403 keeps the rest of Runtime working',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        category: 'activity',
        data: FakeRuntimeData(
            ecosystemError: SonderException(
                'Administrator authorization is required.',
                httpStatus: 403)));
    expect(find.byKey(const Key('work-runs-panel')), findsOneWidget);
    await openCategory(tester, 'observatory');
    expect(find.byKey(const Key('ecosystem-admin-required')), findsOneWidget);
    expect(
        find.text('Administrator authorization is required.'), findsOneWidget);
  });

  testWidgets('Cancel active with nothing running is an info note',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(withAgents: false), category: 'activity');
    await tester.tap(find.text('Cancel active'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Nothing to cancel'), findsOneWidget);
    expect(find.byKey(const Key('runtime-info-notice')), findsOneWidget);
    expect(find.text('Cancel active agents?'), findsNothing);
    // The Developer quick command says the same in its console.
    await openCategory(tester, 'developer');
    await tester.tap(find.text('Cancel active'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel active agents?'), findsNothing);
    expect(find.textContaining('Nothing to cancel'), findsOneWidget);
  });

  testWidgets('Cancel active with agents running asks first', (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo(), category: 'activity');
    await tester.tap(find.text('Cancel active'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel active agents?'), findsOneWidget);
    await tester.tap(find.text('Keep running'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel active agents?'), findsNothing);
    // Nothing was sent, so nothing is reported.
    expect(find.byKey(const Key('runtime-info-notice')), findsNothing);
  });

  testWidgets('approvals: console fallback when the server has no route',
      (tester) async {
    final data =
        FakeRuntimeData(approvalsPage: const ApprovalsPage(supported: false));
    await pumpRuntime(tester,
        info: healthySystemInfo(), data: data, category: 'permissions');
    expect(find.textContaining('/approve <call id>'), findsOneWidget);
    expect(find.text('Approve once…'), findsNothing);
    await openCategory(tester, 'overview');
    expect(find.text('approve from the console (/approvals)'), findsOneWidget);
  });

  group('approvals queue', () {
    testWidgets(
        'Approve once re-reads the call, asks with the sheet, posts once',
        (tester) async {
      final data = FakeRuntimeData(
          approvalsPage: ApprovalsPage(supported: true, pending: [_pending()]));
      await pumpRuntime(tester,
          info: healthySystemInfo(), data: data, category: 'permissions');
      expect(find.textContaining('write_file'), findsOneWidget);
      expect(find.textContaining('path: src/render/pso_cache.cpp'),
          findsOneWidget);
      final readsBefore = data.approvalReads.length;

      // Cancel sends nothing.
      await tester.tap(find.text('Approve once…'));
      await tester.pumpAndSettle();
      expect(data.approvalReads.skip(readsBefore), contains(200));
      expect(find.byKey(const Key('approval-sheet')), findsOneWidget);
      expect(find.text('write_file · call 3f9a12c0'), findsOneWidget);
      await tester.tap(find.byKey(const Key('approval-cancel')));
      await tester.pumpAndSettle();
      expect(data.approved, isEmpty);

      // Approve once sends exactly one request, bound to tool and digest.
      await tester.tap(find.text('Approve once…'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('approval-confirm')));
      await tester.pumpAndSettle();
      expect(data.approved, [
        ('3f9a12c0d1e2f3a4', const Duration(minutes: 15), 'write_file', _digest)
      ]);
      expect(
          find.textContaining('write_file call 3f9a12c0 once'), findsOneWidget);
      expect(find.textContaining('approved'), findsWidgets);
    });

    testWidgets('a call no longer pending is not approvable', (tester) async {
      final data = FakeRuntimeData(
          approvalsPage: ApprovalsPage(supported: true, pending: [_pending()]));
      await pumpRuntime(tester,
          info: healthySystemInfo(), data: data, category: 'permissions');
      // It ran, or aged out, after the list was drawn.
      data.approvalsPage = const ApprovalsPage(supported: true);
      await tester.tap(find.text('Approve once…'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('approval-sheet')), findsNothing);
      expect(data.approved, isEmpty);
      expect(find.textContaining('is waiting for approval'), findsOneWidget);
    });

    testWidgets('a server without the approve route shows the console command',
        (tester) async {
      final data = FakeRuntimeData(
          approvalsPage: ApprovalsPage(supported: true, pending: [_pending()]))
        ..approveError = SonderException(
            ApprovalsApi.consoleFallback('3f9a12c0d1e2f3a4'),
            code: ApprovalsApi.unavailableCode,
            httpStatus: 404);
      await pumpRuntime(tester,
          info: healthySystemInfo(), data: data, category: 'permissions');
      await tester.tap(find.text('Approve once…'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('approval-confirm')));
      await tester.pumpAndSettle();
      expect(find.text('/approve 3f9a12c0d1e2f3a4'), findsOneWidget);
    });

    testWidgets('a 403 says approvals need a developer or admin account',
        (tester) async {
      final data = FakeRuntimeData(
          approvalsPage: ApprovalsPage(supported: true, pending: [_pending()]))
        ..approveError = SonderException('forbidden', httpStatus: 403);
      await pumpRuntime(tester,
          info: healthySystemInfo(), data: data, category: 'permissions');
      await tester.tap(find.text('Approve once…'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('approval-confirm')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Approvals need a developer or admin account'),
          findsOneWidget);
    });

    testWidgets('Revoke cancels an open approval', (tester) async {
      final data = FakeRuntimeData(
        approvalsPage: ApprovalsPage(supported: true, open: [
          IssuedApproval(
              nonce: 'n_1234',
              callId: '11aa22bb33cc44dd',
              tool: 'git_commit',
              expiresAt: runtimeNow.add(const Duration(minutes: 14))),
        ]),
      );
      await pumpRuntime(tester,
          info: healthySystemInfo(), data: data, category: 'permissions');
      expect(find.text('once · 14m left'), findsOneWidget);
      await tester.tap(find.text('Revoke'));
      await tester.pumpAndSettle();
      expect(data.revoked, ['n_1234']);
      expect(find.textContaining('Revoked the approval for git_commit'),
          findsOneWidget);
    });
  });

  testWidgets('Permissions shows the mode read-only, with its risk matrix',
      (tester) async {
    final data = FakeRuntimeData()
      ..mode = PermissionMode.fromJson({
        'mode': 'manual',
        'label': 'Manual',
        'blurb': 'asks before changes',
        'matrix': {'file_write': 'ask', 'destructive': 'deny'},
      });
    await pumpRuntime(tester,
        info: healthySystemInfo(), data: data, category: 'permissions');
    expect(find.text('Manual'), findsOneWidget);
    expect(find.text('asks before changes'), findsOneWidget);
    expect(find.text('File write'), findsOneWidget);
    expect(find.text('Asks first'), findsOneWidget);
    expect(find.text('Refused'), findsOneWidget);
    // No second mode-changing surface unless the shell wires the one flow.
    expect(find.byKey(const Key('permission-mode-change')), findsNothing);
    expect(find.textContaining('mode chip under the chat composer'),
        findsOneWidget);
  });

  testWidgets('Change mode… uses the flow the shell hands in', (tester) async {
    var opened = 0;
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: Settings(),
        initialInfo: healthySystemInfo(),
        liveUpdates: false,
        dataSource: FakeRuntimeData(),
        initialCategory: 'permissions',
        onChangePermissionMode: () => opened++,
      ),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('permission-mode-change')));
    expect(opened, 1);
  });

  testWidgets('phone layouts fit at text scale 1.0, 1.5 and 2.0',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    for (final scale in [1.0, 1.5, 2.0]) {
      for (final category in [
        null,
        'overview',
        'activity',
        'models',
        'memory',
        'permissions',
        'server',
        'observatory',
        'cluster',
        'developer',
        'about',
      ]) {
        await tester.pumpWidget(MediaQuery(
          data: MediaQueryData(
              size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
          child: MaterialApp(
            key: ValueKey('$scale-$category'),
            theme: SonderTheme.dark,
            home: RuntimeScreen(
              settings: Settings(),
              // Every panel has content, so every row shape is measured.
              initialInfo: richSystemInfo(),
              liveUpdates: false,
              dataSource: richRuntimeData(),
              now: runtimeNow,
              initialCategory: category,
            ),
          ),
        ));
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull,
            reason: 'scale $scale, ${category ?? 'list'}');
      }
    }
  });

  testWidgets('Runtime meets tap-target and label guidelines on a phone',
      (tester) async {
    final handle = tester.ensureSemantics();
    for (final category in [
      null,
      'overview',
      'activity',
      'models',
      'memory',
      'permissions',
      'server',
      'developer',
    ]) {
      await pumpRuntime(tester,
          info: richSystemInfo(),
          data: richRuntimeData(),
          size: const Size(390, 844),
          category: category);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    }
    handle.dispose();
  });

  group('chrome', () {
    testWidgets('alone: a way back to Chat, the workspace menu and Refresh',
        (tester) async {
      WorkspaceDestination? went;
      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: RuntimeScreen(
          settings: Settings(),
          liveUpdates: false,
          onNavigate: (destination) => went = destination,
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.byTooltip('Back to chat'), findsOneWidget);
      expect(find.text('Chat'), findsOneWidget);
      expect(find.byTooltip('Workspace navigation'), findsOneWidget);
      expect(find.byTooltip('Refresh'), findsOneWidget);
      expect(went, isNull);
    });

    testWidgets('inside the shell: no back arrow, Chat or workspace menu',
        (tester) async {
      Future<void> pumpShell(bool sidebar, VoidCallback onOpen) async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: ShellScope(
            current: WorkspaceDestination.runtime,
            sidebarVisible: sidebar,
            navigate: (_) {},
            openNavigation: onOpen,
            child: RuntimeScreen(
              settings: Settings(),
              liveUpdates: false,
              onNavigate: (_) {},
            ),
          ),
        ));
        await tester.pumpAndSettle();
      }

      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await pumpShell(true, () {});
      expect(find.byTooltip('Back to chat'), findsNothing);
      expect(find.text('Chat'), findsNothing);
      expect(find.byTooltip('Workspace navigation'), findsNothing);
      expect(find.byTooltip('Open navigation'), findsNothing);
      expect(find.byTooltip('Refresh'), findsOneWidget);
      expect(find.text('Runtime'), findsOneWidget);

      var opened = 0;
      tester.view.physicalSize = const Size(390, 844);
      await pumpShell(false, () => opened++);
      await tester.tap(find.byTooltip('Open navigation'));
      expect(opened, 1);
      expect(find.byTooltip('Back to chat'), findsNothing);
    });
  });

  testWidgets('rail badges: running work and approvals waiting',
      (tester) async {
    final handle = tester.ensureSemantics();
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(
            runs: [runningWorkRun()],
            approvalsPage:
                ApprovalsPage(supported: true, pending: [_pending()])));
    // One work run and one agent are running.
    expect(find.bySemanticsLabel(RegExp('Activity.*2 running', dotAll: true)),
        findsOneWidget);
    expect(
        find.bySemanticsLabel(
            RegExp('Permissions.*1 approval waiting', dotAll: true)),
        findsOneWidget);
    handle.dispose();
  });

  group('HttpRuntimeDataSource', () {
    test('reads work runs with auth and no redirects; 404 approvals is n/a',
        () async {
      final seen = <String>[];
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(
            baseUrl: 'http://pc.test:11435/', apiKey: 'k');
        final runs = await source.workRuns();
        expect(runs.single.isRunning, isTrue);
        expect(runs.single.budget, const Duration(minutes: 30));
        final approvals = await source.approvals();
        expect(approvals.supported, isFalse);
        await source.cancelWorkRun('wr-${'0' * 32}');
        expect(() => source.cancelWorkRun('../x'), throwsArgumentError);
      },
          () => MockClient((request) async {
                expect(request.followRedirects, isFalse);
                expect(request.headers['Authorization'], 'Bearer k');
                seen.add('${request.method} ${request.url.path}');
                if (request.url.path == '/v1/work-runs') {
                  return http.Response(
                      jsonEncode({
                        'runs': [
                          {
                            'id': 'wr-${'1' * 32}',
                            'status': 'running',
                            'created_at': 1000,
                            'updated_at': 1100,
                            'deadline_at': 2800,
                            'cancel_requested': false,
                          }
                        ]
                      }),
                      200);
                }
                if (request.url.path == '/v1/approvals') {
                  return http.Response(
                      '{"error":{"message":"not found"}}', 404);
                }
                return http.Response(
                    '{"id":"wr-${'0' * 32}","status":"running"}', 200);
              }));
      expect(seen, [
        'GET /v1/work-runs',
        'GET /v1/approvals',
        'POST /v1/work-runs/wr-${'0' * 32}/cancel',
      ]);
    });

    test('approves one call bound to tool and digest, revokes by nonce',
        () async {
      final seen = <String>[];
      await http.runWithClient(() async {
        const source =
            HttpRuntimeDataSource(baseUrl: 'http://pc.test:11435', apiKey: 'k');
        final page = await source.approvals(limit: 200);
        expect(page.pending.single.callId, '3f9a12c0');
        final issued = await source.approveCall('3f9a12c0',
            ttl: const Duration(minutes: 5),
            tool: 'write_file',
            digest: _digest);
        expect(issued.nonce, 'n_c41a');
        await source.revokeApproval('n_c41a');
      },
          () => MockClient((request) async {
                seen.add('${request.method} ${request.url}');
                if (request.method == 'POST' &&
                    request.url.path == '/v1/approvals/3f9a12c0') {
                  expect(request.headers['Idempotency-Key'], isNotEmpty);
                  final body = jsonDecode(request.body) as Map;
                  expect(body['ttl_seconds'], 300);
                  expect(body['tool'], 'write_file');
                  expect(body['digest'], _digest);
                  return http.Response(
                      jsonEncode({
                        'nonce': 'n_c41a',
                        'call_id': '3f9a12c0',
                        'ttl_seconds': 300,
                      }),
                      200);
                }
                if (request.method == 'POST') {
                  return http.Response('{"revoked": true}', 200);
                }
                return http.Response(
                    jsonEncode({
                      'pending': [
                        {'call_id': '3f9a12c0', 'tool': 'write_file'}
                      ],
                      'approvals': const [],
                    }),
                    200);
              }));
      expect(seen, [
        'GET http://pc.test:11435/v1/approvals?limit=200',
        'POST http://pc.test:11435/v1/approvals/3f9a12c0',
        'POST http://pc.test:11435/v1/approvals/revoke/n_c41a',
      ]);
    });

    test('reads the permission mode; 404 is a server without modes', () async {
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        final mode = await source.permissionMode();
        expect(mode!.mode, 'manual');
        expect(mode.matrix['file_write'], 'ask');
      },
          () => MockClient((_) async => http.Response(
              jsonEncode({
                'mode': 'manual',
                'matrix': {'file_write': 'ask'},
              }),
              200)));
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        expect(await source.permissionMode(), isNull);
      }, () => MockClient((_) async => http.Response('{}', 404)));
    });

    test('parses jobs, fanout and compute pages', () async {
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        final jobs = await source.jobs();
        expect(jobs.single.kind, 'index');
        expect(jobs.single.status, 'running');
        final fanout = await source.fanoutRuns();
        expect(fanout.single.answered, 2);
        expect(fanout.single.running, 1);
        final nodes = await source.computeNodes();
        expect(nodes.single.local, isTrue);
        expect(nodes.single.health, 'healthy');
        expect(nodes.single.stale, isFalse);
      },
          () => MockClient((request) async {
                switch (request.url.path) {
                  case '/v1/jobs':
                    expect(request.url.queryParameters['limit'], '20');
                    return http.Response(
                        jsonEncode({
                          'object': 'list',
                          'data': [
                            {
                              'job_id': 'job-1',
                              'kind': 'index',
                              'status': 'RUNNING',
                              'updated_at': '2026-09-25T12:00:00Z',
                            }
                          ]
                        }),
                        200);
                  case '/v1/fanout':
                    return http.Response(
                        jsonEncode({
                          'runs': [
                            {
                              'run_id': 'fan-1',
                              'status': 'running',
                              'models_selected': 3,
                              'models_answered': 2,
                              'models_running': 1,
                              'updated_ts': 1790000000,
                            }
                          ]
                        }),
                        200);
                  default:
                    return http.Response(
                        jsonEncode({
                          'object': 'compute_inventory_page',
                          'nodes': [
                            {
                              'node_id': 'pc-a',
                              'local': true,
                              'stale': false,
                              'health': 'healthy',
                              'active_jobs': 0,
                            }
                          ]
                        }),
                        200);
                }
              }));
    });

    test('transport failures and unreadable bodies are SonderExceptions',
        () async {
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        await expectLater(source.workRuns(), throwsA(isA<SonderException>()));
      }, () => MockClient((_) async => http.Response('not json', 200)));
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        await expectLater(
            source.fanoutRuns(),
            throwsA(isA<SonderException>().having(
                (e) => e.message, 'message', contains('cannot reach'))));
      }, () => MockClient((_) async => throw Exception('refused')));
    });

    test('errors carry status and code', () async {
      await http.runWithClient(() async {
        const source = HttpRuntimeDataSource(baseUrl: 'http://pc.test');
        await expectLater(
            source.jobs(),
            throwsA(isA<SonderException>()
                .having((e) => e.httpStatus, 'status', 403)
                .having((e) => e.code, 'code', 'FORBIDDEN')));
      },
          () => MockClient((_) async => http.Response(
              '{"error":{"code":"FORBIDDEN","message":"administrator authorization is required"}}',
              403)));
    });
  });

  test('work run words', () {
    final run = runningWorkRun();
    expect(workRunWord(run), 'working');
    expect(workRunDetail(run, runtimeNow), '4m of 30m budget');
    expect(workRunStatus(const WorkRun(id: 'x', status: 'budget_exceeded')),
        StatusKind.fail);
    expect(workRunsSummary(null), isNull);
    expect(workRunsSummary(const []), 'None yet');
    expect(workRunsSummary([runningWorkRun()]), '1 running · 1 recent');
  });

  testWidgets('live refresh reads status, updates, extensions and extras',
      (tester) async {
    final paths = <String>[];
    final client = MockClient((request) async {
      paths.add(request.url.path);
      switch (request.url.path) {
        case '/v1/sonder/status':
          return http.Response(
              jsonEncode({
                'status': 'ready',
                'models': [
                  {'id': 'sonder:latest', 'owned_by': 'local'}
                ],
                'selfmod': {
                  'enabled': true,
                  'mode': 'propose',
                  'active': 0,
                  'deployed': 2,
                  'rollback_points': 1,
                  'runs': const [],
                },
              }),
              200);
        case '/v1/admin/updates/status':
          return http.Response(
              jsonEncode({
                'running_version': '0.9.0',
                'running_commit': '55a8f684fede0000',
                'platform': 'linux',
                'architecture': 'x86_64',
                'plans': [
                  {
                    'update_id': 'u1',
                    'status': 'verified',
                    'channel': 'stable',
                    'target_version': '0.9.1',
                    'created_at_utc': '2026-09-25T12:00:00Z',
                  }
                ],
              }),
              200);
        case '/v1/extensions':
          return http.Response(
              jsonEncode({
                'persistence': 'durable',
                'records': [
                  {
                    'extension_id': 'ext.demo',
                    'scope': 'user',
                    'version': '1.0.0',
                    'enabled': true,
                    'health_state': 'healthy',
                  }
                ],
              }),
              200);
        case '/v1/work-runs':
          return http.Response(
              jsonEncode({
                'runs': [
                  {
                    'id': 'wr-${'2' * 32}',
                    'status': 'failed',
                    'created_at': 1000,
                    'updated_at': 1100,
                  }
                ]
              }),
              200);
        case '/v1/approvals':
          return http.Response(
              jsonEncode({
                'pending': const [],
                'approvals': [
                  {'call_id': 'c1', 'tool': 'write_file', 'nonce': 'n1'}
                ],
              }),
              200);
      }
      return http.Response('{}', 404);
    });
    await http.runWithClient(() async {
      tester.view.physicalSize = const Size(1280, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.light,
        home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435')),
      ));
      await settleLive(tester);
      expect(
          paths,
          containsAll(<String>[
            '/v1/sonder/status',
            '/v1/admin/updates/status',
            '/v1/extensions',
            '/v1/work-runs',
            '/v1/approvals',
            '/v1/sonder/ecosystem',
            '/v1/permission-mode',
          ]));
      // Overview: approvals and work runs, from the extras.
      expect(find.text('None waiting'), findsOneWidget);
      expect(find.text('1 approved once'), findsOneWidget);
      expect(find.text('1 recent'), findsOneWidget);
      // Updates & extensions appear once the server reports them.
      await tester.tap(find.byKey(const Key('category-updates')));
      await pumpFrames(tester);
      expect(find.textContaining('0.9.0'), findsWidgets);
      expect(find.text('ext.demo'), findsOneWidget);
      // Leaving the page and coming back keeps it.
      await tester.tap(find.byKey(const Key('category-overview')));
      await pumpFrames(tester);
      expect(find.text('ext.demo'), findsNothing);
      await tester.tap(find.byKey(const Key('category-updates')));
      await pumpFrames(tester);
      expect(find.text('ext.demo'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    }, () => client);
  });

  testWidgets('an HTTP error is a banner and the server tile, not offline',
      (tester) async {
    final client = MockClient((request) async =>
        http.Response('{"error":{"message":"denied"}}', 401));
    await http.runWithClient(() async {
      tester.view.physicalSize = const Size(1280, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      await tester.pumpWidget(MaterialApp(
        home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435')),
      ));
      await settleLive(tester);
      expect(find.textContaining("Can't reach"), findsNothing);
      // Lane A keeps the server's own 401 reason instead of a generic
      // "Unauthorized" (describeServerError).
      expect(find.textContaining('denied'), findsWidgets);
      expect(find.byKey(const Key('runtime-stale')), findsOneWidget);
      // The Server rail entry flags the problem in words.
      expect(
          find.descendant(
              of: find.byKey(const Key('category-server')),
              matching: find.text('error')),
          findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    }, () => client);
  });

  testWidgets('one running action does not lock the rest of the page',
      (tester) async {
    final reply = Completer<void>();
    final client = MockClient((request) async {
      if (request.url.path == '/v1/chat/completions') {
        await reply.future;
        return http.Response(
            '{"choices":[{"message":{"content":"turns: 1284"}}]}', 200);
      }
      return http.Response('{}', 404);
    });
    await http.runWithClient(() async {
      await pumpRuntime(tester,
          info: healthySystemInfo(),
          data: FakeRuntimeData(runs: [runningWorkRun()]),
          category: 'developer');
      await tester.tap(find.text('Stats'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));
      // The command is in flight under its own entry...
      expect(find.text('/stats'), findsOneWidget);
      expect(find.text('working'), findsWidgets);
      // ...and every other control still works.
      expect(_button(tester, find.text('Context')).onPressed, isNotNull);
      expect(_button(tester, find.text('Send')).onPressed, isNotNull);
      await openCategory(tester, 'activity');
      expect(_button(tester, find.text('Stop…')).onPressed, isNotNull);
      expect(_button(tester, find.text('Run goal')).onPressed, isNotNull);
      await openCategory(tester, 'developer');
      reply.complete();
      await tester.runAsync(() => Future<void>.delayed(Duration.zero));
      await tester.pumpAndSettle();
      expect(find.text('turns: 1284'), findsOneWidget);
    }, () => client);
  });

  testWidgets('console: each reply under its command, newest first, five kept',
      (tester) async {
    final client = MockClient((request) async {
      final body = jsonDecode(request.body) as Map;
      final messages = body['messages'] as List;
      final command = (messages.last as Map)['content'] as String;
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'content': 'reply to $command'}
              }
            ]
          }),
          200);
    });
    await http.runWithClient(() async {
      await pumpRuntime(tester,
          info: healthySystemInfo(), category: 'developer');
      for (var i = 1; i <= 6; i++) {
        await tester.enterText(
            find.byKey(const Key('runtime-command')), '/echo $i');
        await tester.tap(find.byKey(const Key('runtime-command-send')));
        await tester.runAsync(() => Future<void>.delayed(Duration.zero));
        await tester.pumpAndSettle();
      }
      expect(find.text('reply to /echo 6'), findsOneWidget);
      expect(find.text('reply to /echo 2'), findsOneWidget);
      // Only the last five stay.
      expect(find.text('reply to /echo 1'), findsNothing);
      expect(find.byKey(const Key('console-entry-1')), findsNothing);
      // Each reply sits under its own command, newest first.
      final newest = find.byKey(const Key('console-entry-6'));
      expect(find.descendant(of: newest, matching: find.text('/echo 6')),
          findsOneWidget);
      expect(
          find.descendant(of: newest, matching: find.text('reply to /echo 6')),
          findsOneWidget);
      expect(
          tester.getTopLeft(newest).dy,
          lessThan(
              tester.getTopLeft(find.byKey(const Key('console-entry-5'))).dy));
    }, () => client);
  });

  testWidgets('a failed server start shows its reason inline, not a dialog',
      (tester) async {
    final client = MockClient((request) async {
      if (request.url.path == '/v1/launcher/start') {
        return http.Response(
            '{"error":{"message":"port 11435 is in use"}}', 409);
      }
      if (request.url.path == '/v1/launcher/status') {
        return http.Response(
            jsonEncode({
              'ok': true,
              'launcher': 'sonder-launcher 1.2',
              'server_running': false,
              'server_state': 'stopped',
              'server_host': '127.0.0.1',
              'server_port': 11435,
            }),
            200);
      }
      return http.Response('{}', 404);
    });
    await http.runWithClient(() async {
      await pumpRuntime(tester,
          info: healthySystemInfo(),
          category: 'server',
          settings: Settings(
            serverUrl: 'http://127.0.0.1:11435',
            launcherUrl: 'http://127.0.0.1:11436',
            launcherToken: 'launcher-token-0123456789abcdef',
          ));
      expect(find.byKey(const Key('runtime-busy')), findsNothing);
      expect(find.byKey(const Key('runtime-failure')), findsNothing);
      await tester.tap(find.byKey(const Key('start-server')));
      await tester.runAsync(() => Future<void>.delayed(Duration.zero));
      await tester.pumpAndSettle();
      expect(find.byType(AlertDialog), findsNothing);
      final failure = find.byKey(const Key('runtime-failure'));
      expect(failure, findsOneWidget);
      expect(
          find.descendant(
              of: failure, matching: find.text('Start server failed')),
          findsOneWidget);
      expect(
          find.descendant(of: failure, matching: find.textContaining('in use')),
          findsOneWidget);
      // The rail flags the Server page.
      expect(
          find.descendant(
              of: find.byKey(const Key('category-server')),
              matching: find.text('warn')),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 3));
    }, () => client);
  });

  testWidgets('polls stop while the app is paused or a route covers Runtime',
      (tester) async {
    var statusReads = 0;
    final client = MockClient((request) async {
      if (request.url.path == '/v1/sonder/status') {
        statusReads++;
        return http.Response('{"status": "ready", "models": []}', 200);
      }
      if (request.url.path == '/v1/work-runs') {
        return http.Response('{"runs": []}', 200);
      }
      return http.Response('{}', 404);
    });
    Future<void> wait(Duration total) async {
      for (var elapsed = Duration.zero;
          elapsed < total;
          elapsed += const Duration(milliseconds: 500)) {
        await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 5)));
        await tester.pump(const Duration(milliseconds: 500));
      }
    }

    final navigator = GlobalKey<NavigatorState>();
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
        navigatorKey: navigator,
        home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435')),
      ));
      await settleLive(tester);
      final afterLoad = statusReads;
      await wait(const Duration(seconds: 6));
      expect(statusReads, greaterThan(afterLoad), reason: 'visible: polls');

      for (final state in [
        AppLifecycleState.inactive,
        AppLifecycleState.hidden,
        AppLifecycleState.paused,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      final paused = statusReads;
      await wait(const Duration(seconds: 12));
      expect(statusReads, paused, reason: 'paused: no polls');

      for (final state in [
        AppLifecycleState.hidden,
        AppLifecycleState.inactive,
        AppLifecycleState.resumed,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await settleLive(tester);
      navigator.currentState!.push(MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('covering page'))));
      await pumpFrames(tester);
      final covered = statusReads;
      await wait(const Duration(seconds: 12));
      expect(statusReads, covered, reason: 'covered: no polls');

      navigator.currentState!.pop();
      await pumpFrames(tester);
      await wait(const Duration(seconds: 6));
      expect(statusReads, greaterThan(covered), reason: 'back: polls again');
      await tester.pumpWidget(const SizedBox());
    }, () => client);
  });
}
