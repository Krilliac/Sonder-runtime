import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/classify.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

const _runId = 'wr-7c1e0a55d2b44b1e9d3f6a8b0c2d4e6f';

/// serve.py `_work_run_pending_text`, verbatim.
const _pending =
    'Work is still running as work run $_runId (wall-clock budget 1800 s). '
    'Fetch the answer with GET /v1/work-runs/$_runId, or stop further changes '
    'with POST /v1/work-runs/$_runId/cancel.';

Future<void> _sendLongTurn(WidgetTester tester, FakeChatBackend backend) async {
  await tester.enterText(find.byType(TextField), 'refactor the shader cache');
  await tester.testTextInput.receiveAction(TextInputAction.send);
  await tester.pump();
  backend.lastTurn.done(_pending);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
}

void main() {
  test('the hand-off text is recognised with its budget', () {
    final ref =
        workRunOf(const ChatMessage(role: Role.assistant, content: _pending))!;
    expect(ref.id, _runId);
    expect(ref.budgetSeconds, 1800);
    expect(ref.shortId, 'wr-7c1e…');
    expect(
        workRunOf(const ChatMessage(
            role: Role.assistant, content: 'Runs are listed by id.')),
        isNull);
  });

  testWidgets('a running turn shows the card, never the raw route text',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendLongTurn(tester, backend);

    expect(find.byKey(const Key('work-run-card')), findsOneWidget);
    expect(find.textContaining('GET /v1/work-runs', findRichText: true),
        findsNothing);
    expect(find.textContaining('work run wr-7c1e… · ', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('of 30m budget', findRichText: true),
        findsOneWidget);
    expect(find.text('Still running on the PC. The answer will appear here.'),
        findsOneWidget);
    // Not an answer: no rating chips.
    expect(find.text('useful'), findsNothing);
    await unmountChat(tester);
  });

  testWidgets(
      'polls with backoff, then the answer replaces the card and is '
      'stored', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendLongTurn(tester, backend);

    await tester.pump(const Duration(seconds: 2));
    expect(backend.workRunGets, hasLength(1));
    await tester.pump(const Duration(seconds: 4));
    expect(backend.workRunGets, hasLength(1), reason: 'second delay is 5 s');
    await tester.pump(const Duration(seconds: 1));
    expect(backend.workRunGets, hasLength(2));

    backend.workRun = (id) => WorkRunInfo(
        id: id, status: 'returned', output: 'The PSO cache now warms at load.');
    await tester.tap(find.byKey(const Key('work-run-refresh')));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(find.byKey(const Key('work-run-card')), findsNothing);
    expect(find.text('The PSO cache now warms at load.'), findsOneWidget);
    expect(
        await storedChatText(), contains('The PSO cache now warms at load.'));
    expect(await storedChatText(), isNot(contains('GET /v1/work-runs')));
    await unmountChat(tester);
  });

  testWidgets('Stop asks first, then cancels exactly once', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendLongTurn(tester, backend);

    await tester.tap(find.byKey(const Key('work-run-stop')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-run-stop-confirm')), findsOneWidget);
    await tester.tap(find.text('Keep running'));
    await tester.pumpAndSettle();
    expect(backend.workRunCancels, isEmpty);

    await tester.tap(find.byKey(const Key('work-run-stop')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('work-run-stop-yes')));
    await tester.pumpAndSettle();
    expect(backend.workRunCancels, [_runId]);
    expect(find.text('Stopping…'), findsOneWidget);

    // A second tap cannot send another cancel.
    await tester.tap(find.byKey(const Key('work-run-stop')),
        warnIfMissed: false);
    await tester.pumpAndSettle();
    expect(backend.workRunCancels, [_runId]);

    backend.workRun = (id) => WorkRunInfo(id: id, status: 'cancelled');
    await tester.pump(const Duration(seconds: 3));
    await tester.pump();
    expect(find.byKey(const Key('work-run-card')), findsNothing);
    expect(
        find.textContaining('was stopped', findRichText: true), findsWidgets);
    await unmountChat(tester);
  });

  testWidgets('403 says which account can follow work runs', (tester) async {
    final backend = FakeChatBackend()
      ..workRun = (_) => throw SonderException(
          'Work runs need a developer or admin account.',
          httpStatus: 403);
    await pumpChat(tester, backend);
    await _sendLongTurn(tester, backend);
    await tester.tap(find.byKey(const Key('work-run-refresh')));
    await tester.pump();
    await tester.pump();
    expect(
        find.textContaining('Work runs need a developer or admin account',
            findRichText: true),
        findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('429 WORK_CAPACITY_EXHAUSTED lists running runs with Stop',
      (tester) async {
    final backend = FakeChatBackend()
      ..runningWork = [const WorkRunInfo(id: _runId, status: 'running')];
    await pumpChat(tester, backend);
    await tester.enterText(find.byType(TextField), 'one more job');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    backend.lastTurn.fail(SonderException(
      'routed work capacity is busy (2 routed work run(s) are already running); '
      'retry later, or cancel a run with POST /v1/work-runs/<id>/cancel',
      httpStatus: 429,
      code: 'WORK_CAPACITY_EXHAUSTED',
      retryable: true,
    ));
    await tester.pump();
    await tester.pump();
    expect(
        find.textContaining('Every work slot on the PC is busy',
            findRichText: true),
        findsOneWidget);
    expect(find.textContaining('POST /v1/work-runs', findRichText: true),
        findsNothing);

    await tester.tap(find.byKey(const Key('error-running-work')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('running-work')), findsOneWidget);
    expect(find.textContaining('wr-7c1e…'), findsOneWidget);
    await tester.tap(find.text('Stop…'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Stop run'));
    await tester.pumpAndSettle();
    expect(backend.workRunCancels, [_runId]);
    expect(find.text('Stopping…'), findsOneWidget);
    await tester.tap(find.text('Close'));
    await tester.pumpAndSettle();
    await unmountChat(tester);
  });

  group('SonderApiChatBackend work runs', () {
    test('GET and cancel use the server routes and parse the record', () async {
      final seen = <http.Request>[];
      final client = MockClient((request) async {
        seen.add(request);
        return http.Response(
            jsonEncode({
              'id': _runId,
              'status': request.method == 'POST' ? 'running' : 'returned',
              'created_at': 1790000000.5,
              'updated_at': 1790000060.0,
              'deadline_at': 1790001800.5,
              'cancel_requested': request.method == 'POST',
              'output': 'done',
              'output_truncated': false,
            }),
            200);
      });
      await http.runWithClient(() async {
        final b = SonderApiChatBackend(baseUrl: 'http://pc:11435/');
        final got = await b.getWorkRun(_runId);
        expect(got.status, 'returned');
        expect(got.output, 'done');
        expect(got.deadlineAt!.difference(got.createdAt!).inSeconds, 1800);
        final cancelled = await b.cancelWorkRun(_runId);
        expect(cancelled.cancelRequested, isTrue);
      }, () => client);
      expect(seen.map((r) => '${r.method} ${r.url.path}'), [
        'GET /v1/work-runs/$_runId',
        'POST /v1/work-runs/$_runId/cancel',
      ]);
    });

    test('403 becomes the role sentence', () async {
      final client = MockClient((_) async => http.Response(
          '{"error":{"message":"developer or admin authentication is required '
          'for work runs","type":"forbidden","code":"FORBIDDEN"}}',
          403));
      await http.runWithClient(() async {
        await expectLater(
          SonderApiChatBackend(baseUrl: 'http://pc:11435').getWorkRun(_runId),
          throwsA(isA<SonderException>()
              .having((e) => e.httpStatus, 'status', 403)
              .having((e) => e.message, 'message',
                  'Work runs need a developer or admin account.')),
        );
      }, () => client);
    });
  });
}
