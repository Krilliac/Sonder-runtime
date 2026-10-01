import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/agent_lanes.dart';
import 'package:sonder_runtime/agent_screen.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/agents/transcript_view.dart';
import 'package:sonder_runtime/background_work.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/workspace_ui.dart';

class SearchAgents extends FakeAgents {
  @override
  Map<String, dynamic> lane(String id) => {
        ...super.lane(id),
        'parent_session_id': 'parent-conversation-exact-full-id',
        'status': id == 'a' ? 'running' : 'completed',
        'unread_reports': id == 'b' ? 1 : 0,
      };
  @override
  Future<AgentLanePage> agentLanes(
          {int cursor = 0, String? parentSessionId}) async =>
      AgentLanePage.fromJson({
        'lanes': [lane('a'), lane('b')],
        'has_more': true,
        'next_cursor': 2,
      });
}

class EmptyAgents extends FakeAgents {
  @override
  Future<AgentLanePage> agentLanes(
          {int cursor = 0, String? parentSessionId}) async =>
      AgentLanePage.fromJson({'lanes': const [], 'has_more': false});
}

class FailingReads extends FakeAgents {
  Object? failure = SonderException('Authentication failed', httpStatus: 401);
  int attempts = 0;
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) {
    attempts++;
    if (failure != null) return Future.error(failure!);
    return super.agentInspect(id, cursor: cursor, wait: wait);
  }
}

class LongTitleAgents extends ReportingAgents {
  @override
  Map<String, dynamic> lane(String id) => {
        ...super.lane(id),
        'title':
            'Review the parser and preserve a detailed explanation of every compatibility decision for the next conversation',
        'parent_session_id': 'parent-conversation-exact-full-id',
      };
}

class FakeAgents extends SonderApi {
  FakeAgents() : super(baseUrl: 'http://unused');
  @override
  Future<BackgroundWork> backgroundWork({String project = ''}) async =>
      const BackgroundWork();
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

class MetadataAgents extends FakeAgents {
  @override
  Map<String, dynamic> lane(String id) => {
        ...super.lane(id),
        'task': 'Bounded task for $id',
        'workspace_root': 'C:/workspace/$id',
        'tier': 'code',
        'revision': 7,
      };
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

class BackgroundAgents extends FakeAgents {
  final cancelled = <String>[];

  @override
  Future<BackgroundWork> backgroundWork({String project = ''}) async =>
      BackgroundWork.fromJson({
        'groups': {
          'lanes': const [],
          'fleets': [
            {
              'id': 'fleet-1',
              'task': 'Build a fleet',
              'status': 'running',
              'requested_agents': 3,
              'worker_slots': 2,
              'counts': {'done': 1, 'running': 1, 'queued': 1},
              'children': [
                {
                  'id': 'child-1',
                  'task': 'Child task',
                  'status': 'running',
                  'preview': 'working',
                },
              ],
              'cancelable': true,
            },
          ],
          'autopilot': [
            {
              'id': 'auto-1',
              'objective': 'Keep the goal moving',
              'status': 'running',
              'phase': 'executing',
              'current_task': 'Task one',
              'task_counts': {'total': 2, 'done': 1, 'running': 1},
              'cancelable': true,
            },
          ],
        },
      });

  @override
  Future<void> cancelBackground(
    String kind,
    String id, {
    String project = '',
  }) async {
    cancelled.add('$kind/$id/$project');
  }
}

/// A lane that read a file, plus a prose answer.
class ToolAgents extends FakeAgents {
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) async {
    inspections.add((id: id, cursor: cursor, wait: wait));
    return AgentSnapshot.fromJson({
      'lane': lane(id),
      'messages': const [],
      'events': cursor > 0
          ? const []
          : [
              {
                'sequence': 1,
                'event_type': 'model.response',
                'payload': {
                  'content':
                      '{"tool": "read_file", "arguments": {"path": "src/parser.dart"}}'
                },
              },
              {
                'sequence': 2,
                'event_type': 'tool.requested',
                'payload': {
                  'name': 'read_file',
                  'call_id': 'call-1',
                  'arguments': {'path': 'src/parser.dart', 'limit': 200},
                },
              },
              {
                'sequence': 3,
                'event_type': 'tool.result',
                'payload': {
                  'name': 'read_file',
                  'call_id': 'call-1',
                  'success': true,
                  'output': 'void parse() {}',
                },
              },
              {
                'sequence': 4,
                'event_type': 'model.response',
                'payload': {'content': 'The parser has no error recovery.'},
              },
            ],
      'next_cursor': 4,
    });
  }
}

/// [ToolAgents] with the fleet and autopilot run of [BackgroundAgents].
class ToolBackgroundAgents extends ToolAgents {
  @override
  Future<BackgroundWork> backgroundWork({String project = ''}) =>
      BackgroundAgents().backgroundWork(project: project);
}

/// The lane list answers only when [gate] completes.
class SlowListAgents extends FakeAgents {
  final gate = Completer<void>();
  @override
  Future<AgentLanePage> agentLanes(
      {int cursor = 0, String? parentSessionId}) async {
    await gate.future;
    return super.agentLanes(cursor: cursor);
  }
}

/// Answers the first inspection, then fails until [failure] is cleared.
class FlakyAgents extends FakeAgents {
  Object? failure;
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) {
    if (failure != null && cursor > 0) return Future.error(failure!);
    return super.agentInspect(id, cursor: cursor, wait: wait);
  }
}

/// Lanes with long transcripts, for scroll positions. Like the server,
/// every inspection returns the messages; only events are cursor-paged.
class LongAgents extends FakeAgents {
  @override
  Future<AgentSnapshot> agentInspect(String id,
      {int cursor = 0, bool wait = false}) async {
    inspections.add((id: id, cursor: cursor, wait: wait));
    return AgentSnapshot.fromJson({
      'lane': lane(id),
      'messages': [
        for (var i = 1; i <= 40; i++)
          {
            'id': '$id-$i',
            'sequence': i,
            'author': i.isOdd ? 'parent' : 'child',
            'content': 'Message $i for $id',
            'delivery_state': 'handled',
          },
      ],
      'events': const [],
      'next_cursor': 40,
    });
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
  testWidgets(
      'long titles and reports remain usable at narrow width with large text',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: const TextScaler.linear(1.3)),
            child: child!),
        home: AgentScreen(api: LongTitleAgents())));
    await tester.pumpAndSettle();
    await tester.tap(find.textContaining('Review the parser').first);
    await tester.pumpAndSettle();
    expect(find.byTooltip('Send to agent'), findsOneWidget);
    expect(find.textContaining('Parent conversation ·'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('loaded search, unread filter and exact parent context',
      (tester) async {
    await open(tester, SearchAgents());
    expect(find.text('Search loaded conversations'), findsOneWidget);
    await tester.enterText(find.byKey(const Key('agent-search')), 'parser');
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsNothing);
    expect(find.text('Load more conversations'), findsOneWidget);
    await tester.tap(find.byTooltip('Clear search'));
    await tester.pumpAndSettle();
    // Status filters are pills; each states its loaded count.
    expect(find.bySemanticsLabel('Unread, 1'), findsOneWidget);
    await tester.tap(find.byKey(const Key('agent-filter-unread')));
    await tester.pumpAndSettle();
    expect(find.text('Parser agent'), findsNothing);
    expect(find.text('Docs agent'), findsOneWidget);
    await tester.tap(find.text('Parent · parent-c…l-id'));
    await tester.pumpAndSettle();
    expect(find.text('parent-conversation-exact-full-id'), findsOneWidget);
    expect(find.text('Copy ID'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('authentication read failure stops polling and offers settings',
      (tester) async {
    final api = FailingReads();
    WorkspaceDestination? destination;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home:
            AgentScreen(api: api, onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.textContaining('needs authentication'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    await tester.pump(const Duration(seconds: 30));
    expect(api.attempts, 1);
    await tester.tap(find.text('Open Settings'));
    await tester.pumpAndSettle();
    expect(destination, WorkspaceDestination.settings);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
      'transient reads stop after three attempts and explicit retry recovers',
      (tester) async {
    final api = FailingReads()..failure = TimeoutException('offline');
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 3));
    await tester.pump();
    await tester.pump(const Duration(seconds: 6));
    await tester.pumpAndSettle();
    expect(api.attempts, 3);
    await tester.pump(const Duration(seconds: 30));
    expect(api.attempts, 3);
    api.failure = null;
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets(
      'keyboard send targets selected agent and leaving protects drafts',
      (tester) async {
    final api = FakeAgents();
    WorkspaceDestination? destination;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home:
            AgentScreen(api: api, onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    final composer = find.byWidgetPredicate((widget) =>
        widget is TextField &&
        widget.decoration?.labelText == 'Message this agent');
    await tester.enterText(composer, 'Keyboard followup');
    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pumpAndSettle();
    expect(api.calls, hasLength(1));
    await tester.enterText(composer, 'Keep this draft');
    await tester.tap(find.byTooltip('Workspace navigation'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Settings'));
    await tester.pumpAndSettle();
    expect(destination, isNull);
    await tester.tap(find.text('Keep editing'));
    await tester.pumpAndSettle();
    expect(find.text('Keep this draft'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('sending a followup creates a web-safe command ID',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byWidgetPredicate((widget) =>
            widget is TextField &&
            widget.decoration?.labelText == 'Message this agent'),
        'Keep the parser correction');
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
    await tester.enterText(
        find.byWidgetPredicate((widget) =>
            widget is TextField &&
            widget.decoration?.labelText == 'Message this agent'),
        'Parser correction');
    await tester.tap(find.text('Docs agent'));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsNothing);
    expect(find.text('Task for b'), findsOneWidget);
    expect(find.text('Parser correction'), findsNothing);
    await tester.enterText(
        find.byWidgetPredicate((widget) =>
            widget is TextField &&
            widget.decoration?.labelText == 'Message this agent'),
        'Docs correction');
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

  testWidgets('shortcut guide and keyboard lane navigation stay discoverable',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    await tester.tap(find.byTooltip('Agent conversation shortcuts'));
    await tester.pumpAndSettle();
    expect(find.text('Agent conversation shortcuts'), findsOneWidget);
    expect(find.text('Alt+↑ / Alt+↓'), findsOneWidget);
    expect(
        find.textContaining('A request is sent only after the server confirms'),
        findsOneWidget);
    await tester.tap(find.text('Close'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.sendKeyDownEvent(LogicalKeyboardKey.altLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.altLeft);
    await tester.pumpAndSettle();
    expect(find.text('Task for b'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('Escape returns from a narrow transcript without cancelling it',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api, size: const Size(390, 844));
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsNothing);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsOneWidget);
    expect(api.calls, isEmpty);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets(
      'run header shows public execution identity and resource boundary',
      (tester) async {
    final api = MetadataAgents();
    WorkspaceDestination? destination;
    tester.view.physicalSize = const Size(1100, 800);
    tester.view.devicePixelRatio = 1;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home:
            AgentScreen(api: api, onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    // Tier and revision are quiet mono facts beside the status pill.
    expect(find.text('tier code'), findsOneWidget);
    expect(find.text('revision 7'), findsOneWidget);
    expect(
        find.bySemanticsLabel(
            'Server execution status: Running · tier code · revision 7'),
        findsOneWidget);
    await tester.tap(find.text('Task, workspace and run details'));
    await tester.pumpAndSettle();
    expect(find.text('Running · tier code · revision 7'), findsOneWidget);
    expect(find.textContaining('Per-lane capacity counters are not reported'),
        findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.tap(find.text('Open Runtime'));
    await tester.pumpAndSettle();
    expect(destination, WorkspaceDestination.runtime);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('empty agents: guidance at the top, Go to Chat, one search',
      (tester) async {
    WorkspaceDestination? destination;
    tester.view.physicalSize = const Size(1200, 800);
    tester.view.devicePixelRatio = 1;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AgentScreen(
            api: EmptyAgents(), onNavigate: (value) => destination = value)));
    await tester.pumpAndSettle();
    expect(find.text('Agents start from Chat with /delegate'), findsOneWidget);
    expect(find.text('Select an agent conversation'), findsNothing);
    expect(find.byKey(const Key('agent-search')), findsNothing);
    expect(find.byTooltip('Find conversation (Ctrl+Shift+F)'), findsNothing);
    final top = tester
        .getTopLeft(find.text('Agents start from Chat with /delegate'))
        .dy;
    expect(top, lessThan(200));
    await tester.tap(find.text('Go to Chat'));
    await tester.pumpAndSettle();
    expect(destination, WorkspaceDestination.chat);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets(
      'background work opens details with progress, agents and confirmed cancellation',
      (tester) async {
    final api = BackgroundAgents();
    await open(tester, api);
    expect(find.byKey(const Key('background-group')), findsOneWidget);
    expect(find.byKey(const Key('fleet-group')), findsOneWidget);
    expect(find.byKey(const Key('autopilot-group')), findsOneWidget);
    // Rows state progress in words, beside a bar that is never the only cue.
    expect(find.textContaining('1 of 3 done'), findsOneWidget);
    expect(
        find.bySemanticsLabel(RegExp(
            r'^Fleet: Build a fleet\. Running, 1 of 3 done, 1 running, 1 queued')),
        findsOneWidget);
    expect(find.textContaining('1 of 2 tasks'), findsOneWidget);

    await tester.tap(find.byKey(const Key('fleet-fleet-1')));
    await tester.pumpAndSettle();
    expect(find.text('1 done'), findsOneWidget);
    expect(find.text('1 running'), findsOneWidget);
    expect(find.text('1 queued'), findsOneWidget);
    expect(find.text('Child task'), findsOneWidget);
    expect(find.byKey(const Key('fleet-child-child-1')), findsOneWidget);

    // Cancelling asks first; declining sends nothing.
    await tester.tap(find.text('Cancel fleet'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel this fleet?'), findsOneWidget);
    await tester.tap(find.text('Keep running'));
    await tester.pumpAndSettle();
    expect(api.cancelled, isEmpty);
    await tester.tap(find.text('Cancel fleet'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel fleet').last);
    await tester.pumpAndSettle();
    expect(api.cancelled, ['fleet/fleet-1/']);

    await tester.tap(find.byKey(const Key('autopilot-auto-1')));
    await tester.pumpAndSettle();
    expect(find.text('Keep the goal moving'), findsWidgets);
    expect(find.text('Task one'), findsOneWidget);
    await tester.tap(find.text('Cancel autopilot'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel autopilot run'));
    await tester.pumpAndSettle();
    expect(api.cancelled, contains('autopilot/auto-1/'));
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('wide layouts keep a single search control', (tester) async {
    await open(tester, FakeAgents());
    expect(find.byKey(const Key('agent-search')), findsOneWidget);
    expect(find.byTooltip('Find conversation (Ctrl+Shift+F)'), findsNothing);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('Agents meets tap-target and label guidelines on a phone',
      (tester) async {
    final handle = tester.ensureSemantics();
    await open(tester, FakeAgents(), size: const Size(390, 844));
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    handle.dispose();
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('phone agents fit at text scale 1.0, 1.5 and 2.0',
      (tester) async {
    for (final scale in [1.0, 1.5, 2.0]) {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      for (final api in [FakeAgents(), EmptyAgents()]) {
        await tester.pumpWidget(MediaQuery(
            data: MediaQueryData(
                size: const Size(390, 844),
                textScaler: TextScaler.linear(scale)),
            child: MaterialApp(
                theme: SonderTheme.dark, home: AgentScreen(api: api))));
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull, reason: 'scale $scale');
      }
    }
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('inside the app shell the page draws no way back to Chat',
      (tester) async {
    tester.view.physicalSize = const Size(1100, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    WorkspaceDestination? shellDestination;
    // Pushed over another page, where the old chrome drew a back arrow.
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark, home: const Scaffold(body: Text('base'))));
    final navigator = tester.state<NavigatorState>(find.byType(Navigator));
    unawaited(navigator.push(MaterialPageRoute<void>(
        builder: (_) => ShellScope(
              current: WorkspaceDestination.agents,
              sidebarVisible: true,
              navigate: (destination) => shellDestination = destination,
              openNavigation: () => fail('the sidebar is visible'),
              child: AgentScreen(
                  api: MetadataAgents(),
                  onNavigate: (_) => fail('the shell owns navigation')),
            ))));
    await tester.pumpAndSettle();
    expect(find.byTooltip('Back to chat'), findsNothing);
    expect(find.byTooltip('Workspace navigation'), findsNothing);
    expect(find.text('Chat'), findsNothing);
    expect(find.byTooltip('Open navigation'), findsNothing);
    expect(find.byTooltip('Agent conversation shortcuts'), findsOneWidget);
    // In-page links leave through the shell.
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Task, workspace and run details'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Open Runtime'));
    await tester.pumpAndSettle();
    expect(shellDestination, WorkspaceDestination.runtime);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('narrow shell pages open the navigation drawer, not Chat',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    var opened = 0;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: ShellScope(
          current: WorkspaceDestination.agents,
          sidebarVisible: false,
          navigate: (_) {},
          openNavigation: () => opened++,
          child: AgentScreen(api: FakeAgents()),
        )));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Open navigation'));
    expect(opened, 1);
    // A transcript keeps its in-page way back to the list.
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.byTooltip('All agent conversations'), findsOneWidget);
    expect(find.byTooltip('Open navigation'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('the shell can ask before leaving unsent drafts', (tester) async {
    tester.view.physicalSize = const Size(1100, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    AgentLeaveGuard? guard;
    await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: AgentScreen(
            api: FakeAgents(), registerLeaveGuard: (value) => guard = value)));
    await tester.pumpAndSettle();
    expect(guard, isNotNull);
    expect(await guard!(), isTrue, reason: 'nothing unsent: leave at once');
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byWidgetPredicate((widget) =>
            widget is TextField &&
            widget.decoration?.labelText == 'Message this agent'),
        'Unsent correction');
    final leaving = guard!();
    await tester.pumpAndSettle();
    expect(find.text('Leave agent conversations?'), findsOneWidget);
    await tester.tap(find.text('Keep editing'));
    await tester.pumpAndSettle();
    expect(await leaving, isFalse);
    expect(find.text('Unsent correction'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    expect(guard, isNull, reason: 'unregistered when the screen goes');
  });

  testWidgets('tool calls are cards with readable arguments, not JSON prose',
      (tester) async {
    await open(tester, ToolAgents());
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    // The tool request the model wrote is represented by its card only.
    expect(find.textContaining('"tool"'), findsNothing);
    expect(find.text('The parser has no error recovery.'), findsOneWidget);
    expect(
        find.bySemanticsLabel(
            RegExp(r'^read_file, done, src/parser\.dart\. Show details$')),
        findsOneWidget);
    expect(find.text('Arguments'), findsNothing);
    await tester.tap(find.text('read_file'));
    await tester.pumpAndSettle();
    expect(find.text('Arguments'), findsOneWidget);
    expect(find.text('path'), findsOneWidget);
    expect(find.text('limit'), findsOneWidget);
    expect(find.text('200'), findsOneWidget);
    expect(find.text('void parse() {}'), findsOneWidget);
    await tester.tap(find.text('Raw JSON'));
    await tester.pumpAndSettle();
    expect(find.textContaining('"path": "src/parser.dart"'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('list rows carry the status word, not colour alone',
      (tester) async {
    await open(tester, SearchAgents());
    expect(find.bySemanticsLabel(RegExp(r'^Parser agent\. Running$')),
        findsOneWidget);
    expect(
        find.bySemanticsLabel(
            RegExp(r'^Docs agent\. Completed, 1 unread report$')),
        findsOneWidget);
    // The word is drawn too, beside the glyph.
    expect(find.textContaining('Running'), findsOneWidget);
    expect(find.textContaining('Completed'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('loading shows skeletons, never a blocking spinner',
      (tester) async {
    final api = SlowListAgents();
    tester.view.physicalSize = const Size(1100, 800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
        MaterialApp(theme: SonderTheme.dark, home: AgentScreen(api: api)));
    await tester.pump();
    expect(find.byType(SkeletonRows), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    api.gate.complete();
    await tester.pumpAndSettle();
    expect(find.byType(SkeletonRows), findsNothing);
    expect(find.text('Parser agent'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());

    // A conversation's first page loads behind message-shaped placeholders.
    final delayed = DelayedAgents();
    await tester.pumpWidget(
        MaterialApp(theme: SonderTheme.dark, home: AgentScreen(api: delayed)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Parser agent'));
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byType(TranscriptSkeleton), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    delayed.first.complete(await delayed.agentInspect('b'));
    await tester.pumpAndSettle();
    expect(find.byType(TranscriptSkeleton), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('a failed refresh keeps the loaded conversation visible',
      (tester) async {
    final api = FlakyAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsOneWidget);
    api.failure = TimeoutException('offline');
    // The next two-second inspection fails.
    await tester.pump(const Duration(seconds: 3));
    await tester.pumpAndSettle();
    expect(find.text('Task for a'), findsOneWidget);
    expect(find.textContaining('took too long'), findsOneWidget);
    expect(find.textContaining('Reconnecting'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('Cancel work asks first; declining sends nothing',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel work'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel Parser agent?'), findsOneWidget);
    await tester.tap(find.text('Keep working'));
    await tester.pumpAndSettle();
    expect(api.calls, isEmpty);
    await tester.tap(find.text('Cancel work'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cancel work').last);
    await tester.pumpAndSettle();
    expect(api.calls.single, matches(r'^a/cancel/ui-[0-9a-f]{32}$'));
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('each conversation keeps its own scroll position',
      (tester) async {
    await open(tester, LongAgents());
    Finder transcript() => find
        .descendant(
            of: find.byKey(const Key('agent-transcript')),
            matching: find.byType(Scrollable))
        .first;
    double offset() =>
        tester.state<ScrollableState>(transcript()).position.pixels;
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.drag(transcript(), const Offset(0, -600));
    await tester.pumpAndSettle();
    final parser = offset();
    expect(parser, greaterThan(300));
    await tester.tap(find.text('Docs agent'));
    await tester.pumpAndSettle();
    expect(offset(), 0);
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(offset(), parser);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('switching faster than the cross-fade never shares a scroller',
      (tester) async {
    await open(tester, LongAgents());
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    // Each tap lands while the previous transcript is still fading out.
    for (final name in ['Docs agent', 'Parser agent', 'Docs agent']) {
      await tester.tap(find.text(name).first);
      await tester.pump(const Duration(milliseconds: 40));
    }
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('Message 1 for b'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('system back from a narrow transcript returns to the list',
      (tester) async {
    final api = FakeAgents();
    await open(tester, api, size: const Size(390, 844));
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsNothing);
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(find.text('Docs agent'), findsOneWidget);
    expect(api.calls, isEmpty);
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('the phone transcript meets tap-target and label guidelines',
      (tester) async {
    final handle = tester.ensureSemantics();
    await open(tester, ToolAgents(), size: const Size(390, 844));
    await tester.tap(find.text('Parser agent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('read_file'));
    await tester.pumpAndSettle();
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    handle.dispose();
    await tester.pumpWidget(const SizedBox());
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('phone transcript and background detail fit at text scale 2.0',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    for (final scale in [1.5, 2.0]) {
      // A fresh screen per scale; the last one ended on a detail page.
      await tester.pumpWidget(const SizedBox());
      await tester.pumpWidget(MediaQuery(
          data: MediaQueryData(
              size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
          child: MaterialApp(
              theme: SonderTheme.dark,
              home: AgentScreen(api: ToolBackgroundAgents()))));
      await tester.pumpAndSettle();
      final list = find
          .descendant(
              of: find.byKey(const Key('agent-list')),
              matching: find.byType(Scrollable))
          .first;
      await tester.scrollUntilVisible(find.text('Parser agent'), 200,
          scrollable: list);
      await tester.tap(find.text('Parser agent'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('read_file'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: 'transcript at $scale');
      await tester.tap(find.byTooltip('All agent conversations'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
          find.byKey(const Key('fleet-fleet-1')), -200,
          scrollable: list);
      await tester.tap(find.byKey(const Key('fleet-fleet-1')));
      await tester.pumpAndSettle();
      expect(find.text('Child task'), findsOneWidget);
      expect(tester.takeException(), isNull, reason: 'fleet at $scale');
    }
    await tester.pumpWidget(const SizedBox());
  });
}
