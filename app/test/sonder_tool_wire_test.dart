// The `sonder_tool` frames below are real server output: see
// test/fixtures/server/generate_tool_stream.py, which drives the server's
// ChatToolTurn emitter and frames each payload with serve.py:_chunk.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/tool_run_card.dart';
import 'package:sonder_runtime/theme.dart';

import 'fixtures/server_fixtures.dart';

List<ChatStreamToolEvent> _events(String fixture) => [
      for (final line in const LineSplitter().convert(serverFixture(fixture)))
        if (line.startsWith('data: '))
          ChatStreamToolEvent.fromJson(Map<String, dynamic>.from(
              (jsonDecode(line.substring(6)) as Map)['sonder_tool'] as Map)),
    ];

Widget _card(ChatStreamToolEvent e) => MaterialApp(
      theme: SonderTheme.dark,
      home: Scaffold(
        body: SingleChildScrollView(
          child: ToolRunCard(
            event: e,
            onApprove: (_, __, ___) async =>
                const ApprovalOutcome(ApprovalStatus.approved),
            onDeny: (_, __, ___) async =>
                const ApprovalOutcome(ApprovalStatus.approved),
          ),
        ),
      ),
    );

void main() {
  const code = "print('hi')\nprint(6 * 7)";

  group('server sonder_tool frames', () {
    test('awaiting_approval carries the code, arguments and timeout', () {
      final e = _events('tool_stream_approved.sse').first;
      expect(e.isAwaitingApproval, isTrue);
      expect(e.callId, matches(RegExp(r'^[0-9a-f]{16}$')));
      expect(e.tool, 'run_code');
      expect(e.backend, 'host');
      expect(e.risk, 'execution');
      expect(e.code, code);
      expect(e.args, {'code': code});
      expect(e.timeout, 120);
      expect(e.hasPayload, isTrue);
    });

    test('approved, started and finished advance the state', () {
      final events = _events('tool_stream_approved.sse');
      expect(events.map((e) => e.state),
          ['awaiting_approval', 'approved', 'running', 'done']);
      final finished = events.last;
      expect(finished.ok, isTrue);
      expect(finished.result, isNotNull);
      expect(finished.result!.ok, isTrue);
      expect(finished.result!.returncode, 0);
      expect(finished.result!.elapsedMs, 0);
    });

    test('denied and expired outcomes keep their own state', () {
      final denied = _events('tool_stream_denied.sse');
      expect(denied.map((e) => e.state), ['awaiting_approval', 'denied']);
      expect(denied.last.denied, isTrue);
      final expired = _events('tool_stream_expired.sse');
      expect(expired.map((e) => e.state), ['awaiting_approval', 'expired']);
    });

    test('a long payload is marked truncated and its hidden characters kept',
        () {
      final e = _events('tool_stream_bidi_long.sse').first;
      expect(e.detailTruncated, isTrue);
      expect(e.detailChars, greaterThan(4000));
      expect(e.code, contains('(truncated'));
      expect(e.bidiWarning, isTrue);
      expect(e.bidiChars, contains('U+202E'));
    });
  });

  group('approval card on server frames', () {
    testWidgets('shows the exact code before Approve can be tapped',
        (tester) async {
      await tester.pumpWidget(_card(_events('tool_stream_approved.sse').first));
      expect(find.byKey(const Key('tool-run-code')), findsOneWidget);
      expect(find.textContaining('print(6 * 7)'), findsWidgets);
      expect(find.byKey(const Key('tool-run-approve')), findsOneWidget);
      expect(find.byKey(const Key('tool-run-deny')), findsOneWidget);
    });

    testWidgets('says when the shown payload is truncated', (tester) async {
      await tester
          .pumpWidget(_card(_events('tool_stream_bidi_long.sse').first));
      expect(find.byKey(const Key('tool-run-truncated')), findsOneWidget);
    });

    testWidgets('offers no Approve when the frame carries no payload',
        (tester) async {
      final blind = ChatStreamToolEvent.fromJson({
        'phase': 'awaiting_approval',
        'call_id': '3354e8ee4b42c23c',
        'tool': 'run_code',
        'backend': 'host',
      });
      expect(blind.hasPayload, isFalse);
      await tester.pumpWidget(_card(blind));
      expect(find.byKey(const Key('tool-run-approve')), findsNothing);
      expect(find.byKey(const Key('tool-run-no-payload')), findsOneWidget);
    });
  });
}
