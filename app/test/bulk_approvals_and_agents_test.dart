import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/agent_lanes.dart';
import 'package:sonder_runtime/agent_screen.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/background_work.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

class _BulkClearAgentsApi extends SonderApi {
  _BulkClearAgentsApi() : super(baseUrl: 'http://unused');

  int clearLanesCalls = 0;
  int clearBgCalls = 0;
  bool lanesCleared = false;
  bool bgCleared = false;

  @override
  Future<AgentLanePage> agentLanes({
    int cursor = 0,
    String? parentSessionId,
  }) async =>
      AgentLanePage.fromJson({
        'lanes': [
          {
            'id': 'lane-run',
            'task': 'Still working',
            'status': 'running',
            'parent_session_id': 'ps-1',
            'tier': 'code',
          },
          if (!lanesCleared)
            {
              'id': 'lane-done',
              'task': 'Finished task',
              'status': 'completed',
              'parent_session_id': 'ps-1',
              'tier': 'fast',
            },
        ],
        'has_more': false,
        'next_cursor': 0,
      });

  @override
  Future<BackgroundWork> backgroundWork({String project = ''}) async =>
      BackgroundWork.fromJson({
        'captured_at': 1700000000.0,
        'groups': {
          'lanes': const [],
          'fleets': const [],
          'autopilot': bgCleared
              ? const []
              : [
                  {
                    'id': 'ap-old-fail',
                    'objective': 'Old failed goal',
                    'status': 'failed',
                    'phase': 'done',
                    'task_counts': {'total': 2, 'done': 1, 'failed': 1},
                    'cancelable': false,
                  },
                ],
        },
        'truncated': const {},
      });

  @override
  Future<int> clearFinishedAgentLanes({List<String>? laneIds}) async {
    clearLanesCalls++;
    lanesCleared = true;
    return 1;
  }

  @override
  Future<({int autopilot, int fleets})> clearFinishedBackgroundWork({
    String project = '',
  }) async {
    clearBgCalls++;
    bgCleared = true;
    return (autopilot: 1, fleets: 0);
  }
}

void main() {
  testWidgets('ApprovalsPanel mass approve all and deny all require confirm',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    final pending = [
      const PendingApproval(
        callId: 'a1b2c3d4e5f60001',
        digest:
            'a1b2c3d4e5f60001a1b2c3d4e5f60001a1b2c3d4e5f60001a1b2c3d4e5f60001',
        tool: 'file_check',
        preview: 'path=a.py',
        mode: 'manual',
      ),
      const PendingApproval(
        callId: 'a1b2c3d4e5f60002',
        digest:
            'a1b2c3d4e5f60002a1b2c3d4e5f60002a1b2c3d4e5f60002a1b2c3d4e5f60002',
        tool: 'script_run',
        preview: 'path=b.py',
        mode: 'manual',
      ),
    ];

    final data = FakeRuntimeData(
      approvalsPage: ApprovalsPage(supported: true, pending: pending),
    );
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
        initialInfo: healthySystemInfo(),
        liveUpdates: false,
        dataSource: data,
        now: runtimeNow,
        initialCategory: 'permissions',
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('approvals-approve-all')), findsOneWidget);
    expect(find.byKey(const Key('approvals-deny-all')), findsOneWidget);
    expect(find.byKey(const Key('approval-deny-a1b2c3d4e5f60001')),
        findsOneWidget);

    // Mass approve all
    await tester.tap(find.byKey(const Key('approvals-approve-all')));
    await tester.pumpAndSettle();
    expect(
        find.byKey(const Key('approvals-approve-all-dialog')), findsOneWidget);
    await tester.tap(find.byKey(const Key('approvals-approve-all-confirm')));
    await tester.pumpAndSettle();
    expect(data.approved.length, 2);
    expect(data.approved.map((e) => e.$1).toList(),
        ['a1b2c3d4e5f60001', 'a1b2c3d4e5f60002']);

    // Mass deny all
    await tester.tap(find.byKey(const Key('approvals-deny-all')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('approvals-deny-all-dialog')), findsOneWidget);
    await tester.tap(find.byKey(const Key('approvals-deny-all-confirm')));
    await tester.pumpAndSettle();
    expect(data.denied.length, 2);
    expect(data.denied.map((e) => e.$1).toList(),
        ['a1b2c3d4e5f60001', 'a1b2c3d4e5f60002']);
  });

  testWidgets('ApprovalsPanel hides already-approved and denied pending rows',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    final pending = [
      const PendingApproval(
        callId: 'a1b2c3d4e5f60001',
        tool: 'file_check',
        preview: 'path=a.py',
        mode: 'manual',
      ),
      const PendingApproval(
        callId: 'a1b2c3d4e5f60002',
        tool: 'script_run',
        preview: 'path=b.py',
        mode: 'manual',
        denied: true,
      ),
    ];
    final open = [
      const IssuedApproval(
        nonce: 'apv_0001',
        callId: 'a1b2c3d4e5f60001',
        tool: 'file_check',
        ttlSeconds: 900,
      ),
      const IssuedApproval(
        nonce: 'apv_0002',
        callId: 'a1b2c3d4e5f60009',
        tool: 'run_code',
        ttlSeconds: 900,
      ),
    ];
    final data = FakeRuntimeData(
      approvalsPage:
          ApprovalsPage(supported: true, pending: pending, open: open),
    );
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: RuntimeScreen(
        settings: Settings(serverUrl: 'http://192.168.1.20:11435'),
        initialInfo: healthySystemInfo(),
        liveUpdates: false,
        dataSource: data,
        now: runtimeNow,
        initialCategory: 'permissions',
      ),
    ));
    await tester.pumpAndSettle();

    // Neither the already-approved call nor the denied call appears as waiting!
    expect(find.byKey(const Key('approvals-approve-all')), findsNothing);
    expect(find.byKey(const Key('approvals-deny-all')), findsNothing);
    expect(
        find.byKey(const Key('approval-pending-a1b2c3d4e5f60001')), findsNothing);
    expect(
        find.byKey(const Key('approval-pending-a1b2c3d4e5f60002')), findsNothing);
    // Revoke all (2) is shown for the 2 open approvals!
    expect(find.byKey(const Key('approvals-revoke-all')), findsOneWidget);
    await tester.tap(find.byKey(const Key('approvals-revoke-all')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('approvals-revoke-all-confirm')));
    await tester.pumpAndSettle();
    expect(data.revoked, ['apv_0001', 'apv_0002']);
  });

  testWidgets('AgentScreen mass clear finished removes terminal lanes and bg',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    final api = _BulkClearAgentsApi();
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: AgentScreen(api: api),
    ));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('agents-clear-all-finished')), findsOneWidget);
    expect(find.byKey(const Key('background-clear-finished')), findsOneWidget);
    expect(
        find.byKey(const Key('agent-lanes-clear-finished')), findsOneWidget);

    await tester.tap(find.byKey(const Key('agents-clear-all-finished')));
    await tester.pumpAndSettle();
    expect(
        find.byKey(const Key('agents-clear-finished-dialog')), findsOneWidget);
    await tester.tap(find.byKey(const Key('agents-clear-finished-confirm')));
    await tester.pumpAndSettle();

    expect(api.clearLanesCalls, 1);
    expect(api.clearBgCalls, 1);
    expect(find.text('Finished task'), findsNothing);
    expect(find.text('Old failed goal'), findsNothing);
    expect(find.text('Still working'), findsOneWidget);
  });
}
