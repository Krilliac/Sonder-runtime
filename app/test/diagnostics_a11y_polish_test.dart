import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/agent_lanes.dart';
import 'package:sonder_runtime/agent_screen.dart';
import 'package:sonder_runtime/agents/background_detail.dart';
import 'package:sonder_runtime/agents/shortcuts.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/app_control.dart';
import 'package:sonder_runtime/app_control_screen.dart';
import 'package:sonder_runtime/app_work_screen.dart';
import 'package:sonder_runtime/background_work.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/runtime/jobs_panel.dart';
import 'package:sonder_runtime/runtime/overview.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/ui/status_row.dart';
import 'package:sonder_runtime/workspace_ui.dart';

import 'runtime_fixtures.dart';
import 'runtime_rich_fixture.dart';

Future<void> _settleLive(WidgetTester tester) async {
  for (var i = 0; i < 30; i++) {
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 40)));
    await tester.pump(const Duration(milliseconds: 50));
    if (i > 3 &&
        find.byKey(const Key('runtime-refreshing')).evaluate().isEmpty) {
      break;
    }
  }
  for (var i = 0; i < 6; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

class _ServerAgentsApi extends SonderApi {
  final List<Map<String, dynamic>> lanes;
  final Object? listError;
  final Completer<void>? gate;
  int laneReads = 0;
  int backgroundReads = 0;

  _ServerAgentsApi({
    required super.baseUrl,
    this.lanes = const [],
    this.listError,
    this.gate,
  });

  @override
  Future<AgentLanePage> agentLanes(
      {int cursor = 0, String? parentSessionId}) async {
    laneReads++;
    if (gate != null) await gate!.future;
    if (listError != null) throw listError!;
    return AgentLanePage.fromJson({'lanes': lanes, 'has_more': false});
  }

  @override
  Future<BackgroundWork> backgroundWork({String project = ''}) async {
    backgroundReads++;
    if (gate != null) await gate!.future;
    return const BackgroundWork();
  }
}

class _GatedJobsSource extends FakeRuntimeData {
  final Completer<void>? gate;
  _GatedJobsSource({super.jobList, this.gate});

  @override
  Future<List<JobSummary>> jobs() async {
    jobReads++;
    if (gate != null) await gate!.future;
    return jobList;
  }
}

void main() {
  group('stale reconnect and server-switch recovery', () {
    testWidgets(
        'RuntimeScreen background poll clears stale server error after recovery',
        (tester) async {
      var failStatus = true;
      final client = MockClient((request) async {
        if (request.url.path == '/v1/sonder/status') {
          if (failStatus) {
            return http.Response(
                '{"error":{"message":"backend temporarily unavailable"}}', 503);
          }
          return http.Response(
              jsonEncode({
                'status': 'ready',
                'models': [
                  {'id': 'sonder:latest', 'owned_by': 'local'}
                ],
              }),
              200);
        }
        return http.Response('{}', 404);
      });

      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      await http.runWithClient(() async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
          ),
        ));
        await _settleLive(tester);

        expect(find.byKey(const Key('runtime-stale')), findsOneWidget);
        expect(find.textContaining('backend temporarily unavailable'),
            findsWidgets);

        // Server recovers; the periodic 2 s background poll must clear the
        // stale error banner once /v1/sonder/status answers 200.
        failStatus = false;
        for (var i = 0; i < 6; i++) {
          await tester.runAsync(
              () => Future<void>.delayed(const Duration(milliseconds: 20)));
          await tester.pump(const Duration(milliseconds: 500));
        }

        expect(find.byKey(const Key('runtime-stale')), findsNothing);
        expect(find.text('Connected'), findsOneWidget);
        await tester.pumpWidget(const SizedBox.shrink());
      }, () => client);
    });

    testWidgets(
        'RuntimeScreen server switch clears stale status/errors and drops '
        'in-flight responses from the previous server', (tester) async {
      final slowServerA = Completer<void>();
      final client = MockClient((request) async {
        if (request.url.path == '/v1/sonder/status') {
          if (request.url.port == 11435) {
            await slowServerA.future;
            return http.Response(
                jsonEncode({
                  'status': 'server-a-stale-status',
                  'models': [
                    {'id': 'server-a-only-model', 'owned_by': 'local'}
                  ],
                }),
                200);
          }
          return http.Response(
              jsonEncode({
                'status': 'server-b-ready',
                'models': [
                  {'id': 'server-b-model', 'owned_by': 'local'}
                ],
              }),
              200);
        }
        return http.Response('{}', 404);
      });

      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      await http.runWithClient(() async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
          ),
        ));
        // Let the initial refresh hit the gated Server A request.
        await tester.runAsync(
            () => Future<void>.delayed(const Duration(milliseconds: 80)));
        await tester.pump(const Duration(milliseconds: 50));

        // Switch to Server B before Server A answers.
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11436'),
          ),
        ));
        await _settleLive(tester);
        expect(find.textContaining('server-b-ready'), findsOneWidget);

        // Now Server A finishes late; its stale response must be discarded.
        slowServerA.complete();
        await _settleLive(tester);
        expect(find.textContaining('server-b-ready'), findsOneWidget);
        expect(find.textContaining('server-a-stale-status'), findsNothing);
        expect(find.textContaining('server-a-only-model'), findsNothing);
        await tester.pumpWidget(const SizedBox.shrink());
      }, () => client);
    });

    testWidgets(
        'narrow phone Runtime detail page shows offline/error banner and Retry',
        (tester) async {
      var failStatus = true;
      final client = MockClient((request) async {
        if (request.url.path == '/v1/sonder/status') {
          if (failStatus) {
            return http.Response(
                '{"error":{"message":"service degraded"}}', 503);
          }
          return http.Response('{"status":"ready","models":[]}', 200);
        }
        return http.Response('{}', 404);
      });

      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      await http.runWithClient(() async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
            initialCategory: RuntimeCategories.overview,
          ),
        ));
        await _settleLive(tester);

        // Banner with Retry must be visible on narrow detail pages, not hidden.
        final banner = find.byKey(const Key('runtime-stale'));
        expect(banner, findsOneWidget);
        final retry = find.descendant(of: banner, matching: find.text('Retry'));
        expect(retry, findsOneWidget);
        expect(tester.takeException(), isNull);

        failStatus = false;
        await tester.tap(retry);
        await _settleLive(tester);
        expect(find.byKey(const Key('runtime-stale')), findsNothing);
        await tester.pumpWidget(const SizedBox.shrink());
      }, () => client);
    });

    testWidgets(
        'LazyRuntimeList resets stale items and drops in-flight loads on '
        'server switch', (tester) async {
      final gateA = Completer<void>();
      final sourceA = _GatedJobsSource(
        gate: gateA,
        jobList: [
          JobSummary(
              id: 'job-server-a',
              kind: 'index',
              status: 'running',
              updatedAt: runtimeNow),
        ],
      );
      final sourceB = _GatedJobsSource(
        jobList: [
          JobSummary(
              id: 'job-server-b',
              kind: 'train',
              status: 'completed',
              updatedAt: runtimeNow),
        ],
      );

      Widget harness(FakeRuntimeData source) => MaterialApp(
            theme: SonderTheme.dark,
            home: Scaffold(
              body: SingleChildScrollView(
                child: JobsList(source: source, now: runtimeNow),
              ),
            ),
          );

      await tester.pumpWidget(harness(sourceA));
      await tester.tap(find.text('Recent jobs'));
      await tester.pump();
      expect(sourceA.jobReads, 1);

      // Switch servers while Server A's job read is still in flight.
      await tester.pumpWidget(harness(sourceB));
      await tester.pumpAndSettle();
      expect(sourceB.jobReads, 1);
      expect(find.textContaining('job-server-b'), findsOneWidget);

      // Complete Server A's stale future; Server B's rows must stay intact.
      gateA.complete();
      await tester.pumpAndSettle();
      expect(find.textContaining('job-server-b'), findsOneWidget);
      expect(find.textContaining('job-server-a'), findsNothing);
    });

    testWidgets(
        'AgentScreen resets stale lanes, paused auth errors, and drops '
        'in-flight fetches when api changes', (tester) async {
      final gateA = Completer<void>();
      final apiErrorA = _ServerAgentsApi(
        baseUrl: 'http://127.0.0.1:11435',
        listError: SonderException('Unauthorized', httpStatus: 401),
      );
      final apiSlowA = _ServerAgentsApi(
        baseUrl: 'http://127.0.0.1:11436',
        gate: gateA,
        lanes: const [
          {
            'id': 'lane-a',
            'session_id': 'sess-a',
            'title': 'Stale Server A Agent',
            'status': 'running',
            'revision': 1,
          }
        ],
      );
      final apiB = _ServerAgentsApi(
        baseUrl: 'http://127.0.0.1:11437',
        lanes: const [
          {
            'id': 'lane-b',
            'session_id': 'sess-b',
            'title': 'Fresh Server B Agent',
            'status': 'completed',
            'revision': 1,
          }
        ],
      );

      tester.view.physicalSize = const Size(1200, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      // Start on an unauthorized server (which pauses list polling).
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AgentScreen(api: apiErrorA),
      ));
      await tester.pumpAndSettle();
      expect(find.textContaining('authentication'), findsWidgets);

      // Switch to a slow server whose fetch stays in flight.
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AgentScreen(api: apiSlowA),
      ));
      await tester.pump();
      expect(apiSlowA.laneReads, 1);

      // Switch again to healthy Server B before slow Server A answers.
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AgentScreen(api: apiB),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Fresh Server B Agent'), findsOneWidget);
      expect(find.textContaining('authentication'), findsNothing);

      // Completing slow Server A must not inject stale lanes onto Server B.
      gateA.complete();
      await tester.pumpAndSettle();
      expect(find.text('Fresh Server B Agent'), findsOneWidget);
      expect(find.text('Stale Server A Agent'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('AppControlScreen and AppWorkScreen rebind when client changes',
        (tester) async {
      final clientA = AppControlClient(
        context: () => AppControlContext(
          serverUrl: 'https://server-a.test',
          deploymentKey: 'deployment',
          account: AccountSession(
              token: 'token-a', origin: 'https://server-a.test'),
        ),
        transportFactory: () => MockClient((_) async => http.Response('{}', 200)),
      );
      final clientB = AppControlClient(
        context: () => AppControlContext(
          serverUrl: 'https://server-b.test',
          deploymentKey: 'deployment',
          account: AccountSession(
              token: 'token-b', origin: 'https://server-b.test'),
        ),
        transportFactory: () => MockClient((_) async => http.Response('{}', 200)),
      );
      addTearDown(clientA.dispose);
      addTearDown(clientB.dispose);

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppControlScreen(client: clientA),
      ));
      await tester.pumpAndSettle();
      expect(find.text('https://server-a.test'), findsOneWidget);

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppControlScreen(client: clientB),
      ));
      await tester.pumpAndSettle();
      expect(find.text('https://server-b.test'), findsOneWidget);
      expect(find.text('https://server-a.test'), findsNothing);

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppWorkScreen(client: clientA),
      ));
      await tester.pumpAndSettle();
      expect(find.textContaining('https://server-a.test'), findsOneWidget);

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppWorkScreen(client: clientB),
      ));
      await tester.pumpAndSettle();
      expect(find.textContaining('https://server-b.test'), findsOneWidget);
    });
  });

  group('error-banner consistency and sanitization', () {
    testWidgets(
        'EcosystemPanel sanitizes non-SonderException errors and wires Retry',
        (tester) async {
      var retries = 0;
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: EcosystemPanel(
              reading: null,
              error: StateError('secret internal stack 0xdeadbeef'),
              runtimeUrl: 'http://127.0.0.1:11435',
              onLaunch: (_) async => const ObservatoryLaunchResult(
                ok: false,
                mode: ObservatoryLaunchMode.unavailable,
                message: 'n/a',
              ),
              onRetry: () => retries++,
            ),
          ),
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('ecosystem-error')), findsOneWidget);
      expect(find.textContaining('0xdeadbeef'), findsNothing);
      expect(find.textContaining('StateError'), findsNothing);
      expect(find.textContaining('Could not reach the server'), findsOneWidget);

      await tester.tap(find.text('Retry'));
      await tester.pump();
      expect(retries, 1);
    });

    testWidgets('EcosystemPanel sanitizes unexpected Observatory launch errors',
        (tester) async {
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: EcosystemPanel(
              reading: EcosystemReading.parse(
                  jsonDecode(jsonEncode(ecosystemReadySynthetic()))),
              runtimeUrl: 'http://127.0.0.1:11435',
              onLaunch: (_) async =>
                  throw StateError('raw-process-exception-secret'),
            ),
          ),
        ),
      ));
      await tester.pumpAndSettle();

      await tester.ensureVisible(
          find.byKey(const Key('ecosystem-open-observatory')));
      await tester.tap(find.byKey(const Key('ecosystem-open-observatory')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('ecosystem-launch-result')), findsOneWidget);
      expect(find.textContaining('raw-process-exception-secret'), findsNothing);
      expect(
          find.textContaining('Check the Observatory executable'), findsOneWidget);
    });

    testWidgets(
        'RuntimeScreen sanitizes raw FormatException during status refresh',
        (tester) async {
      final client = MockClient((request) async {
        if (request.url.path == '/v1/sonder/status') {
          throw const FormatException('raw_internal_parse_marker_999');
        }
        return http.Response('{}', 404);
      });

      tester.view.physicalSize = const Size(1280, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      await http.runWithClient(() async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
          ),
        ));
        await _settleLive(tester);

        expect(find.byKey(const Key('runtime-stale')), findsOneWidget);
        expect(find.textContaining('raw_internal_parse_marker_999'),
            findsNothing);
        await tester.pumpWidget(const SizedBox.shrink());
      }, () => client);
    });
  });

  group('320px narrow width and 2.0x text scale overflow sweep', () {
    testWidgets(
        'all Runtime pages render with zero overflow at 320x640 and 2.0x scale',
        (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      for (final category in [
        null,
        RuntimeCategories.overview,
        RuntimeCategories.activity,
        RuntimeCategories.models,
        RuntimeCategories.memory,
        RuntimeCategories.permissions,
        RuntimeCategories.server,
        RuntimeCategories.observatory,
        RuntimeCategories.cluster,
        RuntimeCategories.developer,
        RuntimeCategories.about,
      ]) {
        await tester.pumpWidget(MediaQuery(
          data: const MediaQueryData(
            size: Size(320, 640),
            textScaler: TextScaler.linear(2.0),
          ),
          child: MaterialApp(
            key: ValueKey('320-2x-$category'),
            theme: SonderTheme.dark,
            home: RuntimeScreen(
              settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
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
            reason: 'overflow at 320x640 2.0x on ${category ?? 'list'}');
      }
    });

    testWidgets(
        'loading Overview tiles, StatusRow, WorkspaceNotice, Shortcuts dialog, '
        'and Fleet/Autopilot detail views fit at 320x640 with 2.0x text scale',
        (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      final fleet = BackgroundFleet.fromJson({
        'id': 'fleet-1',
        'task': 'Audit every subsystem for narrow viewport layout safety',
        'status': 'running',
        'requested_agents': 3,
        'worker_slots': 2,
        'elapsed_seconds': 42,
        'updated_ts': 1000,
        'counts': {'done': 1, 'running': 1, 'queued': 1},
        'children': [
          {
            'id': 'child-1',
            'task': 'Inspect diagnostics screens and verify touch targets',
            'status': 'running',
            'elapsed_seconds': 21,
            'cancelable': true,
          },
        ],
      });
      final autopilot = BackgroundAutopilot.fromJson({
        'id': 'ap-1',
        'objective': 'Verify accessibility and reconnect recovery end to end',
        'status': 'running',
        'phase': 'executing',
        'current_task': 'Run full Flutter widget tests',
        'elapsed_seconds': 95,
        'updated_ts': 1000,
        'task_counts': {'total': 4, 'done': 2, 'running': 1, 'queued': 1},
      });

      await tester.pumpWidget(MediaQuery(
        data: const MediaQueryData(
          size: Size(320, 640),
          textScaler: TextScaler.linear(2.0),
        ),
        child: MaterialApp(
          theme: SonderTheme.light,
          home: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const RuntimeOverview(rows: [], loading: true),
                  StatusRow(
                    kind: StatusKind.warn,
                    word: 'needs you',
                    label: 'Approvals',
                    value: '3 refused calls waiting for confirmation',
                    trailing: OutlinedButton(
                      onPressed: () {},
                      child: const Text('Review'),
                    ),
                  ),
                  WorkspaceNotice(
                    kind: StatusKind.refused,
                    title: 'Administrator authorization is required.',
                    detail: 'Check your API key or account role in Settings.',
                    hint: 'use local-open mode on this PC',
                    actions: [
                      OutlinedButton(
                        onPressed: () {},
                        child: const Text('Retry'),
                      ),
                    ],
                  ),
                  SizedBox(
                    height: 360,
                    child: FleetDetailView(
                      fleet: fleet,
                      capturedAt: 1030,
                      narrow: false,
                      confirmCancel: () async => false,
                      hasLane: (_) => true,
                      onOpenLane: (_) {},
                    ),
                  ),
                  SizedBox(
                    height: 360,
                    child: AutopilotDetailView(
                      run: autopilot,
                      capturedAt: 1030,
                      narrow: true,
                      confirmCancel: () async => false,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ));
      // Skeleton shimmer loops continuously, so pump frames instead of settling.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));
      expect(tester.takeException(), isNull);

      // Also open the Agent shortcuts dialog at 320x640 + 2.0x text scale.
      final ctx = tester.element(find.byType(Scaffold));
      unawaited(showAgentShortcuts(ctx));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('Agent conversation shortcuts'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });

  group('48dp touch targets and semantics labels', () {
    testWidgets(
        'RawDisclosure meets 48dp tap target and announces collapsed/expanded',
        (tester) async {
      final semantics = tester.ensureSemantics();
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: const Scaffold(
          body: RawDisclosure(
            title: 'Status report',
            text: 'ready\nmodels: sonder:latest',
          ),
        ),
      ));
      await tester.pumpAndSettle();

      final header = find.byType(HoverSurface);
      expect(tester.getSize(header).height, greaterThanOrEqualTo(48));
      expect(find.bySemanticsLabel('Status report, collapsed'), findsOneWidget);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));

      await tester.tap(header);
      await tester.pumpAndSettle();
      expect(find.bySemanticsLabel('Status report, expanded'), findsOneWidget);
      semantics.dispose();
    });

    testWidgets(
        'Observatory, Cluster, About, and AppControlScreen meet tap-target and '
        'label guidelines on phone width', (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      final semantics = tester.ensureSemantics();
      for (final category in [
        RuntimeCategories.observatory,
        RuntimeCategories.cluster,
        RuntimeCategories.about,
      ]) {
        await tester.pumpWidget(MaterialApp(
          key: ValueKey('a11y-$category'),
          theme: SonderTheme.dark,
          home: RuntimeScreen(
            settings: Settings(serverUrl: 'http://127.0.0.1:11435'),
            initialInfo: richSystemInfo(),
            liveUpdates: false,
            dataSource: richRuntimeData(),
            now: runtimeNow,
            initialCategory: category,
          ),
        ));
        await tester.pumpAndSettle();
        await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
        await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      }

      final client = AppControlClient(
        context: () => AppControlContext(
          serverUrl: 'https://host.test',
          deploymentKey: 'deployment',
          account: AccountSession(
              token: 'account', origin: 'https://host.test'),
        ),
        transportFactory: () => MockClient((_) async => http.Response('{}', 200)),
      );
      addTearDown(client.dispose);
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AppControlScreen(client: client),
      ));
      await tester.pumpAndSettle();
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      semantics.dispose();
    });
  });
}
