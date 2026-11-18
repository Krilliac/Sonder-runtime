import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/tool_run_card.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';

const _digest =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

void main() {
  testWidgets(
      'ToolRunCard shows tool, code, host vs container trust badge, and bidi warning',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: Scaffold(
        body: SingleChildScrollView(
          child: Column(
            children: [
              ToolRunCard(
                event: const ChatStreamToolEvent(
                  id: 'tc_1',
                  callId: 'c03cd79321b5f7a2',
                  digest: _digest,
                  tool: 'run_code',
                  language: 'python',
                  code: 'print("hi\u202e")',
                  backend: 'host',
                  bidiWarning: true,
                  bidiChars: ['U+202E'],
                  state: 'awaiting_approval',
                ),
                onApprove: (_, __, ___) async =>
                    const ApprovalOutcome(ApprovalStatus.approved),
                onDeny: (_, __, ___) async =>
                    const ApprovalOutcome(ApprovalStatus.approved),
              ),
              const ToolRunCard(
                event: ChatStreamToolEvent(
                  id: 'tc_2',
                  tool: 'run_code',
                  language: 'python',
                  code: 'print("ok")',
                  backend: 'container',
                  state: 'done',
                  result: ToolRunResult(
                    ok: true,
                    returncode: 0,
                    elapsedMs: 18,
                    output: 'ok\n',
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    ));

    expect(find.text('run_code'), findsNWidgets(2));
    expect(find.text('host · unsandboxed'), findsOneWidget);
    expect(find.text('container · isolated'), findsOneWidget);
    expect(find.byKey(const Key('tool-run-bidi-warning')), findsOneWidget);
    expect(find.textContaining('U+202E'), findsOneWidget);
    expect(find.byKey(const Key('tool-run-approve')), findsOneWidget);
    expect(find.byKey(const Key('tool-run-deny')), findsOneWidget);
    expect(find.textContaining('18ms'), findsOneWidget);
    expect(find.textContaining('ok\n'), findsOneWidget);
  });

  testWidgets(
      'ToolRunCard approves and denies inline in ChatScreen without re-submitting',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);

    await tester.enterText(find.byType(TextField), 'run python 2+2');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();

    expect(backend.turns, hasLength(1));
    backend.lastTurn.tool(const ChatStreamToolEvent(
      id: 'tc_1',
      callId: 'c03cd79321b5f7a2',
      digest: _digest,
      tool: 'run_code',
      language: 'python',
      code: 'print(2 + 2)',
      backend: 'host',
      state: 'awaiting_approval',
    ));
    await tester.pump();

    expect(find.byKey(const Key('tool-run-card-tc_1')), findsOneWidget);
    expect(find.text('print(2 + 2)'), findsOneWidget);
    expect(find.textContaining('awaiting approval', findRichText: true),
        findsOneWidget);

    await tester.tap(find.byKey(const Key('tool-run-approve')));
    await tester.pump();
    expect(backend.approvals.single.$1, 'c03cd79321b5f7a2');
    expect(backend.approvalBindings, [('run_code', _digest)]);
    // Prompt was NOT re-submitted!
    expect(backend.turns, hasLength(1));
    expect(find.byKey(const Key('tool-run-submitted')), findsOneWidget);

    // Server continues the in-flight turn.
    backend.lastTurn.tool(const ChatStreamToolEvent(
      id: 'tc_1',
      callId: 'c03cd79321b5f7a2',
      digest: _digest,
      tool: 'run_code',
      language: 'python',
      code: 'print(2 + 2)',
      backend: 'host',
      state: 'done',
      result: ToolRunResult(ok: true, returncode: 0, output: '4\n'),
    ));
    backend.lastTurn.done('It printed 4.');
    await tester.pumpAndSettle();

    expect(find.text('It printed 4.'), findsOneWidget);
    expect(find.text('4\n'), findsOneWidget);
    expect(backend.turns, hasLength(1));

    // Second turn: test inline Deny without re-submitting.
    await tester.enterText(find.byType(TextField), 'run something else');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(backend.turns, hasLength(2));

    backend.lastTurn.tool(const ChatStreamToolEvent(
      id: 'tc_2',
      callId: 'deadbeef12345678',
      digest: _digest,
      tool: 'run_code',
      language: 'python',
      code: 'import os',
      backend: 'host',
      state: 'awaiting_approval',
    ));
    await tester.pump();
    await tester.tap(find.byKey(const Key('tool-run-deny')));
    await tester.pump();
    expect(backend.denials, [('deadbeef12345678', 'run_code', _digest)]);
    expect(backend.turns, hasLength(2));

    backend.lastTurn.tool(const ChatStreamToolEvent(
      id: 'tc_2',
      callId: 'deadbeef12345678',
      digest: _digest,
      tool: 'run_code',
      language: 'python',
      code: 'import os',
      backend: 'host',
      state: 'denied',
      error: 'user declined',
    ));
    backend.lastTurn.done('Okay, skipped running the code.');
    await tester.pumpAndSettle();
    expect(find.text('Okay, skipped running the code.'), findsOneWidget);
    expect(find.text('user declined'), findsOneWidget);

    await unmountChat(tester);
  });
}
