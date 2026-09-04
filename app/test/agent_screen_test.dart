import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/agent_lanes.dart';
import 'package:sonder_runtime/agent_screen.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/theme.dart';

class FakeAgents extends SonderApi {
  FakeAgents() : super(baseUrl: 'http://unused');
  final calls = <String>[];
  final inspections = <({String id, int cursor, bool wait})>[];
  bool failCommand = false;
  String status = 'running';
  Map<String, dynamic> lane(String id) => {
        'id': id,
        'session_id': 'session-$id',
        'title': id == 'a' ? 'Parser agent' : 'Docs agent',
        'status': status,
        'revision': 1
      };
  @override
  Future<AgentLanePage> agentLanes(
          {int cursor = 0, String? parentSessionId}) async =>
      AgentLanePage.fromJson({
        'lanes': [lane('a'), lane('b')]
      });
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) async {
    inspections.add((id: id, cursor: cursor, wait: wait));
    if (wait) return Completer<AgentSnapshot>().future;
    return AgentSnapshot.fromJson({
      'lane': lane(id),
      'messages': [
        {
          'id': '$id-message',
          'sequence': 1,
          'author': 'parent',
          'content': 'Task for $id',
          'delivery_state': 'handled'
        }
      ],
      'events': [],
      'next_cursor': 1
    });
  }

  @override
  Future<AgentReportPage> agentReports(String parentSessionId,
          {int cursor = 0}) async =>
      AgentReportPage.fromJson({'reports': []});
  @override
  Future<AgentReceipt> agentCommand(String id, String action,
      {required String commandId, String? content}) async {
    calls.add('$id/$action/$commandId');
    if (failCommand) throw Exception('offline');
    if (action == 'interrupt') status = 'interrupt_requested';
    return AgentReceipt.fromJson({'command_id': commandId, 'lane': lane(id)});
  }
}

class DelayedAgents extends FakeAgents {
  final first = Completer<AgentSnapshot>();
  bool delayed = false;
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) {
    if (id == 'a' && !delayed) {
      delayed = true;
      return first.future;
    }
    return super.agentInspect(id, cursor: cursor, wait: wait);
  }
}

class ReportingAgents extends FakeAgents {
  bool acknowledged = false;
  @override
  Future<AgentReportPage> agentReports(String parentSessionId,
          {int cursor = 0}) async =>
      AgentReportPage.fromJson({
        'reports': [
          {
            'id': 'report-a',
            'lane_id': 'a',
            'summary': 'Parser verified',
            'artifacts': ['parser.diff'],
            'acknowledged': acknowledged
          },
          {
            'id': 'report-b',
            'lane_id': 'b',
            'summary': 'Other agent private report',
            'acknowledged': false
          },
        ]
      });
  @override
  Future<AgentReceipt> agentAcknowledge(String id,
      {required String commandId}) async {
    acknowledged = true;
    return AgentReceipt.fromJson({'command_id': commandId, 'revision': 2});
  }
}

Future<void> open(WidgetTester tester, FakeAgents api,
    {Size size = const Size(1100, 800)}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  await tester.pumpWidget(
      MaterialApp(theme: SonderTheme.dark, home: AgentScreen(api: api)));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('sending a followup creates a web-safe command ID',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byType(TextField), 'Keep the parser correction');
    await tester.pump();
    await tester.tap(find.byTooltip('Send to agent'));
    await tester.pumpAndSettle();
    expect(api.calls, hasLength(1));
    expect(api.calls.single, matches(r'^a/messages/ui-[0-9a-f]{32}$'));
    expect(find.text('Keep the parser correction'), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets(
      'switching agents refreshes cursors without occupying long-poll slots',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    for (final name in ['Parser agent', 'Docs agent', 'Parser agent']) {
      await tester.tap(find.text(name).first);
      await tester.pumpAndSettle();
      await tester.pump(const Duration(seconds: 3));
      await tester.pumpAndSettle();
    }
    expect(api.inspections.where((request) => request.wait), isEmpty);
    expect(api.inspections.where((request) => request.cursor == 1), isNotEmpty);
    final beforeClosing = api.inspections.length;
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 6));
    expect(api.inspections.length, beforeClosing);
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  testWidgets(
      'reports to external parent remain scoped and require explicit mark read',
      (tester) async {
    final api = ReportingAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Parser verified'), findsOneWidget);
    expect(find.text('Other agent private report'), findsNothing);
    expect(api.acknowledged, isFalse);
    await tester.tap(find.text('Mark read'));
    await tester.pumpAndSettle();
    expect(api.acknowledged, isTrue);
    expect(find.text('Mark read'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  testWidgets(
      'late response cannot replace another agent and reopening reloads durable history',
      (tester) async {
    final api = DelayedAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pump();
    await tester.tap(find.text('Docs agent'));
    await tester.pumpAndSettle();
    api.first.complete(AgentSnapshot.fromJson({
      'lane': api.lane('a'),
      'messages': [
        {
          'id': 'a',
          'sequence': 1,
          'author': 'user',
          'content': 'Stale parser response'
        }
      ],
      'events': []
    }));
    await tester.pumpAndSettle();
    expect(find.text('Task for b'), findsOneWidget);
    expect(find.text('Stale parser response'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    await open(tester, api);
    await tester.tap(find.text('Docs agent'));
    await tester.pumpAndSettle();
    expect(find.text('Task for b'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  testWidgets('switching agents isolates transcripts and preserves each draft',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsOneWidget);
    await tester.enterText(find.byType(TextField), 'Parser correction');
    await tester.tap(find.text('Docs agent'));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsNothing);
    expect(find.text('Task for b'), findsOneWidget);
    expect(find.text('Parser correction'), findsNothing);
    await tester.enterText(find.byType(TextField), 'Docs correction');
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Parser correction'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets(
      'interrupt acknowledgement is not fabricated and retry reuses command',
      (tester) async {
    final api = FakeAgents()..failCommand = true;
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Interrupt'));
    await tester.pumpAndSettle();
    expect(find.text('Retry request'), findsOneWidget);
    api.failCommand = false;
    await tester.tap(find.text('Retry request'));
    await tester.pumpAndSettle();
    expect(api.calls.length, 2);
    expect(api.calls[0], api.calls[1]);
    expect(find.text('Interrupt requested'), findsWidgets);
    expect(find.text('Resume'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('narrow view navigates back to the lane list without cancelling',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api, size: const Size(390, 844));
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.tap(find.byTooltip('All agent conversations'));
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsOneWidget);
    expect(api.calls, isEmpty);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}
