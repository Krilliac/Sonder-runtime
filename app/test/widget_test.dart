import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/main.dart';
import 'package:trilobite/models.dart';
import 'package:trilobite/api.dart';
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
