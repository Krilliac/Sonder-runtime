import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/overview.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/runtime/status_word.dart';
import 'package:sonder_runtime/runtime/work_runs_panel.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/system_screen.dart' as legacy;
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

Future<void> pumpRuntime(
  WidgetTester tester, {
  SystemInfo? info,
  FakeRuntimeData? data,
  Size size = const Size(1280, 1000),
  ThemeData? theme,
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
      settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
      initialInfo: info,
      liveUpdates: false,
      dataSource: data ?? FakeRuntimeData(),
      now: runtimeNow,
    ),
  ));
  await tester.pumpAndSettle();
}

/// Lets real I/O (LocalManager.inspect, MockClient) finish between frames.
Future<void> settleLive(WidgetTester tester) async {
  for (var i = 0; i < 40; i++) {
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pump(const Duration(milliseconds: 50));
    if (i > 4 && find.byType(LinearProgressIndicator).evaluate().isEmpty) {
      break;
    }
  }
  await pumpFrames(tester);
}

/// The live screen polls every 2 s, so it never "settles"; pump a few
/// frames instead (enough for dialogs and ensureVisible animations).
Future<void> pumpFrames(WidgetTester tester) async {
  for (var i = 0; i < 6; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

void main() {
  group('status vocabulary mirrors style.py', () {
    test('glyphs and words', () {
      // style.py _UNICODE_GLYPHS + NOTICE_KINDS and plan §2.1.
      expect(RuntimeStatus.ok.glyph, '✓');
      expect(RuntimeStatus.fail.glyph, '✗');
      expect(RuntimeStatus.fail.word, 'error');
      expect(RuntimeStatus.refused.glyph, '⊘');
      expect(RuntimeStatus.warn.glyph, '!');
      expect(RuntimeStatus.skipped.glyph, '–');
      expect(RuntimeStatus.skipped.word, 'off');
      expect(RuntimeStatus.note.glyph, '·');
      expect(RuntimeStatus.running.glyph, '◈');
      expect(RuntimeStatus.running.word, 'working');
      expect(RuntimeStatus.skipped.isProblem, isFalse);
      expect(RuntimeStatus.refused.isProblem, isTrue);
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
      expect(byLabel['Server']!.status, RuntimeStatus.ok);
      expect(
          byLabel['Server']!.value, startsWith('192.168.1.20:11435 · ready'));
      expect(byLabel['Models']!.value, 'sonder:latest, code');
      expect(byLabel['Approvals']!.status, RuntimeStatus.warn);
      expect(byLabel['Approvals']!.value, '1 call waiting');
      expect(byLabel['Approvals']!.actionLabel, 'Review');
      expect(byLabel['Work runs']!.status, RuntimeStatus.running);
      expect(byLabel['Work runs']!.value, '1 running · wr-7c1e… 4m');
      expect(byLabel['Autopilot']!.status, RuntimeStatus.skipped);
      expect(byLabel['Autopilot']!.value, 'off');
      expect(byLabel['Agents']!.value, '1 running');
    });

    test('offline keeps a word, never a green dot', () {
      final rows = overviewRows(
          serverUrl: 'http://mypc.local:11435', info: null, offline: true);
      expect(rows.single.status, RuntimeStatus.fail);
      expect(rows.single.value, "Can't reach mypc.local:11435");
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
      expect(byLabel['Work runs']!.status, RuntimeStatus.skipped);
      expect(byLabel['Approvals']!.status, RuntimeStatus.skipped);
      expect(rows.where((row) => row.status.isProblem), isEmpty);
    });

    test('recent activity is newest first, capped at five, with words', () {
      final lines = recentActivity(healthySystemInfo().executionFeed);
      expect(lines.map((line) => line.word), ['done', 'refused', 'error']);
      expect(lines.first.text, 'Model sonder:latest · 61.2s');
      expect(lines[1].text, '/write src/render/pso_cache.cpp (manual)');
    });

    test('compact durations', () {
      expect(compactDuration(const Duration(seconds: 42)), '42s');
      expect(compactDuration(const Duration(minutes: 4, seconds: 12)), '4m');
      expect(compactDuration(const Duration(hours: 3, minutes: 12)), '3h 12m');
    });
  });

  testWidgets('Runtime title, Overview first, rail says Jump to section',
      (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo());
    expect(find.text('Runtime'), findsOneWidget);
    expect(find.text('System'), findsNothing);
    expect(find.byKey(const Key('runtime-overview')), findsOneWidget);
    final overviewTop =
        tester.getTopLeft(find.byKey(const Key('runtime-overview'))).dy;
    final workRunsTop = tester.getTopLeft(find.text('Work runs (0)')).dy;
    expect(overviewTop, lessThan(workRunsTop));
    expect(find.bySemanticsLabel('Jump to section'), findsOneWidget);
    // The compatibility name still resolves to the same screen type.
    expect(find.byType(legacy.SystemScreen), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('fresh single-PC server shows no problem dots', (tester) async {
    await pumpRuntime(tester,
        info: SystemInfo.fromJson({
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
        }));
    final scroll = find.byKey(const Key('runtime-scroll'));
    for (var i = 0; i < 40; i++) {
      expect(find.byKey(const Key('status-row-problem')), findsNothing);
      await tester.drag(scroll, const Offset(0, -400));
      await tester.pump();
    }
    // Takeover/failback are listed once (Deployment), not twice.
    await tester.scrollUntilVisible(
        find.text('Deployment & capabilities'), -400,
        scrollable: find.byType(Scrollable).first);
    expect(find.text('Automatic failback'), findsOneWidget);
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
    await pumpRuntime(tester, info: healthySystemInfo(), data: data);
    expect(find.text('Work runs (2)'), findsOneWidget);
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
    expect(find.textContaining('Stop requested'), findsOneWidget);
  });

  testWidgets('a second Stop while the first is pending sends nothing',
      (tester) async {
    final gate = Completer<void>();
    final data = FakeRuntimeData(runs: [runningWorkRun()])
      ..cancelGate = gate.future;
    await pumpRuntime(tester, info: healthySystemInfo(), data: data);
    for (var i = 0; i < 2; i++) {
      await tester.tap(find.text('Stop…'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Stop run'));
      await tester.pump();
    }
    expect(data.cancelled, hasLength(1));
    gate.complete();
    await tester.pumpAndSettle();
    expect(find.textContaining('Stop requested'), findsOneWidget);
  });

  testWidgets('work runs: empty state and 403 role notice', (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo());
    expect(find.text('No work runs'), findsWidgets);
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(
            runsError: SonderException('forbidden', httpStatus: 403)));
    expect(find.text('Work runs need a developer or admin account.'),
        findsOneWidget);
  });

  testWidgets('Overview Open jumps to the work runs section', (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(runs: [runningWorkRun()]),
        size: const Size(390, 844));
    // Phones start with only the Overview open.
    expect(find.byKey(const Key('work-runs-panel')), findsNothing);
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-runs-panel')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('jobs details load only when opened; 403 is n/a', (tester) async {
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
    await pumpRuntime(tester, info: healthySystemInfo(), data: data);
    await tester.scrollUntilVisible(find.text('Jobs · Details'), 300,
        scrollable: find.byType(Scrollable).first);
    expect(data.jobReads, 0);
    await tester.ensureVisible(find.text('Jobs · Details'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Jobs · Details'));
    await tester.pumpAndSettle();
    expect(data.jobReads, 1);
    expect(find.textContaining('index · job-1 · 1m ago'), findsOneWidget);
    await tester.ensureVisible(find.text('Model fanout · Details'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Model fanout · Details'));
    await tester.pumpAndSettle();
    expect(find.textContaining('3/3 answered'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Compute nodes · Details'), 200,
        scrollable: find.byType(Scrollable).first);
    await tester.ensureVisible(find.text('Compute nodes · Details'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Compute nodes · Details'));
    await tester.pumpAndSettle();
    expect(find.text('Needs an administrator account.'), findsOneWidget);
  });

  testWidgets('Cancel active with nothing running is an info notice',
      (tester) async {
    await pumpRuntime(tester, info: healthySystemInfo(withAgents: false));
    await tester.scrollUntilVisible(find.text('Cancel active'), 300,
        scrollable: find.byType(Scrollable).first);
    await tester.ensureVisible(find.text('Cancel active'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel active'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Nothing to cancel'), findsOneWidget);
    expect(find.text('Cancel active agents?'), findsNothing);
  });

  testWidgets('approvals: console fallback when the server has no route',
      (tester) async {
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(
            approvalsPage: const ApprovalsPage(supported: false)));
    expect(find.textContaining('/approve <call id>'), findsOneWidget);
    expect(find.text('approve from the console (/approvals)'), findsOneWidget);
  });

  testWidgets('phone layouts fit at text scale 1.0, 1.5 and 2.0',
      (tester) async {
    for (final scale in [1.0, 1.5, 2.0]) {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      await tester.pumpWidget(MediaQuery(
        data: MediaQueryData(
            size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
        child: MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(),
            initialInfo: healthySystemInfo(),
            liveUpdates: false,
            dataSource: FakeRuntimeData(runs: [runningWorkRun()]),
            now: runtimeNow,
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: 'scale $scale');
    }
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('Runtime meets tap-target and label guidelines on a phone',
      (tester) async {
    final handle = tester.ensureSemantics();
    await pumpRuntime(tester,
        info: healthySystemInfo(),
        data: FakeRuntimeData(runs: [runningWorkRun()]),
        size: const Size(390, 844));
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
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
        expect(runs.single.running, isTrue);
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
        RuntimeStatus.fail);
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
          ]));
      expect(find.text('Work runs (1)'), findsOneWidget);
      expect(find.textContaining('none waiting · 1 open'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('Updates & extensions'), 400,
          scrollable: find.byType(Scrollable).first);
      await pumpFrames(tester);
      expect(find.textContaining('0.9.0'), findsWidgets);
      await tester.scrollUntilVisible(find.textContaining('ext.demo'), 300,
          scrollable: find.byType(Scrollable).first);
      // Collapse and reopen a group through its header.
      await tester.ensureVisible(find.text('Updates & extensions'));
      await pumpFrames(tester);
      await tester.tap(find.text('Updates & extensions'));
      await pumpFrames(tester);
      expect(find.textContaining('ext.demo'), findsNothing);
      // The rail jumps to (and opens) a section.
      await tester.tap(find.text('Updates').first);
      await pumpFrames(tester);
      expect(find.textContaining('ext.demo'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    }, () => client);
  });

  testWidgets('an HTTP error is shown as the server row, not as offline',
      (tester) async {
    final client = MockClient((request) async =>
        http.Response('{"error":{"message":"denied"}}', 401));
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
        home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435')),
      ));
      await settleLive(tester);
      expect(find.textContaining("Can't reach"), findsNothing);
      expect(find.textContaining('Unauthorized'), findsWidgets);
      await tester.pumpWidget(const SizedBox());
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
