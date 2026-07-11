import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/main.dart';
import 'package:trilobite/models.dart';
import 'package:trilobite/api.dart';
import 'package:trilobite/settings.dart';
import 'package:trilobite/system_screen.dart';

void main() {
  testWidgets('App boots to the chat screen', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    // Model picker in the title bar shows the local model once settings resolve.
    expect(find.textContaining('trilobite'), findsWidgets);
    // Empty state shows the message composer.
    expect(find.byType(TextField), findsOneWidget);
  });

  testWidgets('System always has an explicit return to main chat',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('System'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('System'), findsOneWidget);
    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);

    await tester.tap(find.byTooltip('Back to chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('New chat'), findsOneWidget);
  });

  testWidgets('System exposes persistent autopilot goal controls',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('System'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    final list = find.byType(Scrollable).first;
    await tester.scrollUntilVisible(
      find.byKey(const Key('autopilot-goal')),
      240,
      scrollable: list,
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('Autopilot'), findsOneWidget);
    expect(find.byKey(const Key('autopilot-goal')), findsOneWidget);
    expect(find.byKey(const Key('autopilot-plan')), findsOneWidget);
    expect(find.byKey(const Key('autopilot-run')), findsOneWidget);
    expect(find.text('Workspace'), findsOneWidget);
    expect(find.text('Observe only'), findsOneWidget);
  });

  testWidgets('System shows the shared local runtime policy', (tester) async {
    await tester.binding.setSurfaceSize(const Size(1280, 1200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final captureKey = GlobalKey();
    final info = SystemInfo.fromJson({
      'status': 'Ollama local runtime ready',
      'runtime_policy': {
        'revision': 4,
        'path': r'C:\Users\natew\AppData\Local\trilobite\runtime_policy.json',
        'source': 'runtime_policy_update',
        'error': '',
        'local_models': {
          'fast': 'qwen2.5:3b',
          'code': 'trilobite:latest',
          'general': 'qwen2.5:7b-instruct',
        },
        'routing': {
          'router': 'fast',
          'workbench': 'code',
          'autopilot': 'code',
          'fleet': 'code',
          'review': 'general',
        },
        'missing_models': const [],
      },
      'models': const [],
    });

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData.dark(useMaterial3: true),
        home: RepaintBoundary(
          key: captureKey,
          child: SystemScreen(
            settings: Settings(),
            initialInfo: info,
            liveUpdates: false,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.scrollUntilVisible(
      find.byKey(const Key('runtime-policy-panel')),
      360,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pumpAndSettle();

    expect(find.text('Local Runtime Policy'), findsOneWidget);
    expect(find.text('Shared policy r4'), findsOneWidget);
    expect(find.text('fast  qwen2.5:3b'), findsOneWidget);
    expect(find.text('review  general'), findsOneWidget);
    expect(
        find.textContaining('/runtime set workbench=general'), findsOneWidget);

    if (Platform.environment['TRILOBITE_CAPTURE_UI'] == '1') {
      await tester.runAsync(() async {
        final boundary = captureKey.currentContext!.findRenderObject()!
            as RenderRepaintBoundary;
        final image = await boundary.toImage(pixelRatio: 1);
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        final output = File('build/ui-smoke-runtime-policy.png');
        await output.parent.create(recursive: true);
        await output.writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
        image.dispose();
      });
    }
  });

  testWidgets('Completed autopilot run renders its persisted ledger',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(1280, 1800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final info = SystemInfo.fromJson({
      'status': 'Ollama local runtime ready',
      'stats': '805 checks passed',
      'learn_tiers': 'local tiers: fast, code, general',
      'improvements': 'No urgent improvement items detected.',
      'autopilot': {
        'active_runs': 0,
        'resumable_runs': 0,
        'total_runs': 1,
        'total_listed': 1,
        'latest': {
          'id': 'auto-885ca53e8ef6',
          'objective':
              'Inspect the autonomous controller and verify its completion gates.',
          'project': 'trilobite',
          'tier': 'code',
          'policy': 'observe',
          'allow_web': false,
          'status': 'completed',
          'phase': 'completed',
          'cycles': 3,
          'failures': 0,
          'checkpoints': 1,
          'replans': 1,
          'max_failures': 2,
          'max_tasks': 3,
          'max_replans': 2,
          'adaptive': true,
          'summary': 'Objective completed with host-verified task evidence.',
          'final_report': 'autopilot end report\n3 tasks passed\n0 failures',
          'last_error': '',
          'criteria': [
            'Persistence service exists.',
            'Completion gates are enforced.',
          ],
          'plan': [
            {
              'id': 'task-01',
              'title': 'Verify file existence',
              'instruction': 'Inspect both modules.',
              'kind': 'inspect',
              'status': 'passed',
              'attempts': 1,
            },
            {
              'id': 'task-02',
              'title': 'Check persistence',
              'instruction': 'Read the lifecycle store.',
              'kind': 'research',
              'status': 'passed',
              'attempts': 1,
            },
            {
              'id': 'task-03',
              'title': 'Validate completion gates',
              'instruction': 'Ground every success criterion.',
              'kind': 'validate',
              'status': 'passed',
              'attempts': 1,
            },
          ],
        },
        'runs': const [],
        'events': [
          {'event_id': 1, 'kind': 'created', 'message': 'goal created'},
          {'event_id': 2, 'kind': 'planned', 'message': 'plan accepted'},
          {
            'event_id': 3,
            'kind': 'completed',
            'message': 'evidence gates passed'
          },
        ],
      },
      'models': const [],
    });
    final captureKey = GlobalKey();
    final scheme = ColorScheme.fromSeed(
      seedColor: const Color(0xFF63D6C8),
      brightness: Brightness.dark,
    );

    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(
          useMaterial3: true,
          colorScheme: scheme,
          brightness: Brightness.dark,
          scaffoldBackgroundColor: const Color(0xFF0B1117),
          cardTheme: CardThemeData(
            elevation: 0,
            color: const Color(0xFF121B23),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
              side: const BorderSide(color: Color(0xFF24343D)),
            ),
          ),
        ),
        home: RepaintBoundary(
          key: captureKey,
          child: SystemScreen(
            settings: Settings(),
            initialInfo: info,
            liveUpdates: false,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Persistent checklist'), findsOneWidget);
    expect(
      find.text(
        '3/3 tasks settled • 3 cycles • 0/2 failures • '
        '1 checkpoint • 1/2 replans',
      ),
      findsOneWidget,
    );
    expect(find.textContaining('Validate completion gates'), findsWidgets);

    if (Platform.environment['TRILOBITE_CAPTURE_UI'] == '1') {
      await tester.runAsync(() async {
        final boundary = captureKey.currentContext!.findRenderObject()!
            as RenderRepaintBoundary;
        final image = await boundary.toImage(pixelRatio: 1);
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        final output = File('build/ui-smoke-autopilot.png');
        await output.parent.create(recursive: true);
        await output.writeAsBytes(bytes!.buffer.asUint8List(), flush: true);
        image.dispose();
      });
    }
  });

  testWidgets('Settings always has an explicit return to main chat',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Settings'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('Settings'), findsOneWidget);
    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);

    await tester.tap(find.text('Chat'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.text('New chat'), findsOneWidget);
  });

  testWidgets('Approximate location is explicit opt-in and persists',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Settings'));
    await tester.pumpAndSettle();

    final label = find.text('Allow approximate IP location');
    expect(find.byType(ListView), findsOneWidget);
    await tester.drag(find.byType(ListView), const Offset(0, -600));
    await tester.pumpAndSettle();
    expect(label, findsOneWidget);
    final tile = find.widgetWithText(
      SwitchListTile,
      'Allow approximate IP location',
    );
    expect(tester.widget<SwitchListTile>(tile).value, isFalse);

    await tester.tap(tile);
    await tester.pump();
    expect(tester.widget<SwitchListTile>(tile).value, isTrue);

    final save = find.text('Save');
    await tester.drag(find.byType(ListView), const Offset(0, -700));
    await tester.pumpAndSettle();
    expect(save, findsOneWidget);
    await tester.tap(save);
    await tester.pumpAndSettle();

    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getBool('allow_approximate_location'), isTrue);
  });

  testWidgets(
      'Assistant messages render markdown and collapse activity evidence',
      (tester) async {
    final now = DateTime(2026, 7, 10);
    final thread = ChatThread(
      id: 'markdown-test',
      title: 'Rendered response',
      project: 'ui',
      createdAt: now,
      updatedAt: now,
      messages: const [
        ChatMessage(role: Role.user, content: 'show formatting'),
        ChatMessage(
          role: Role.assistant,
          content: '**Bold answer**\n\n```python\nprint("ok")\n```\n\n'
              '=== ACTIVITY (observable work) ===\ntool calls: 1\n=== END ACTIVITY ===',
        ),
      ],
    );
    SharedPreferences.setMockInitialValues(<String, Object>{
      'chat_threads_v1': jsonEncode([thread.toJson()]),
    });

    await tester.pumpWidget(const TrilobiteApp(manageLocalServer: false));
    await tester.pumpAndSettle();

    expect(find.byType(MarkdownBody), findsOneWidget);
    expect(find.text('Bold answer'), findsOneWidget);
    expect(find.text('Activity evidence'), findsOneWidget);
    expect(find.textContaining('**Bold answer**'), findsNothing);
  });

  testWidgets('Workbench activity panel renders checklist and exact actions',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(1200, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final response = ActivityResponse.fromJson({
      'id': 'r1',
      'label': 'agent:code',
      'status': 'complete',
      'elapsed_ms': 250,
      'tool_calls': 1,
      'model_calls': 2,
      'result_summary': 'Created and validated demo.py',
      'events': [
        {
          'kind': 'tool_call',
          'tool': 'script_run',
          'title': 'Ran Script',
          'command': 'python demo.py',
          'output': 'DEMO_OK',
          'ok': true,
          'elapsed_ms': 90,
        },
      ],
      'checklist': {
        'title': 'Build demo',
        'status': 'done',
        'items': [
          {'id': 'a', 'title': 'Inspect files', 'status': 'done'},
          {'id': 'b', 'title': 'Run validation', 'status': 'done'},
        ],
      },
    });

    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(useMaterial3: true),
      home: Scaffold(
        body: SingleChildScrollView(
          child: WorkbenchActivityPanel(
            response: response,
            totalToolCalls: 7,
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Build demo'), findsOneWidget);
    expect(find.text('Ran Script'), findsOneWidget);
    expect(find.textContaining('DEMO_OK'), findsOneWidget);
    expect(find.text('2/2'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
