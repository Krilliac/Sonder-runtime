import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/classify.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

// The server's wording (serve.py: "refused %s: %s (mode: %s)").
const _refusedWrite =
    "refused /write: file_write changes files and nobody is here to answer "
    "manual's ask, so it is refused rather than assumed; run /approve 3f9a12c0 "
    'at the console to allow this exact call once (mode: manual)';

ChatMessage _assistant(String text) =>
    ChatMessage(role: Role.assistant, content: text);

Future<void> _sendRefused(WidgetTester tester, FakeChatBackend backend,
    {String text = _refusedWrite}) async {
  await tester.enterText(find.byType(TextField), '/write notes.txt hello');
  await tester.testTextInput.receiveAction(TextInputAction.send);
  await tester.pump();
  backend.lastTurn.done(text);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

void main() {
  group('classification', () {
    test('server refusal text becomes a refusal with its call id', () {
      final r = refusalOf(_assistant(_refusedWrite))!;
      expect(r.subject, '/write');
      expect(r.mode, 'manual');
      expect(r.callId, '3f9a12c0');
      expect(r.reason, startsWith('file_write changes files'));
      expect(r.reason, isNot(contains('(mode:')));
      expect(classifyReply(_assistant(_refusedWrite)), ReplyKind.refused);
    });

    test('the unattended mode refusal has no subject and no call id', () {
      final r = refusalOf(_assistant(
          'refused: raising the permission mode from manual to auto needs a '
          'person to confirm it'))!;
      expect(r.subject, '');
      expect(r.callId, '');
    });

    test('status "refused" from the receipt classifies too', () {
      const m = ChatMessage(
        role: Role.assistant,
        content: 'That write was not allowed.',
        responseMetadata: ChatResponseMetadata(status: 'refused'),
      );
      expect(classifyReply(m), ReplyKind.refused);
    });

    test('answers that merely mention refusal stay answers', () {
      expect(classifyReply(_assistant('The server refused my request because…')),
          ReplyKind.answer);
      expect(classifyReply(const ChatMessage(role: Role.user, content: 'refused x: y')),
          ReplyKind.answer);
    });
  });

  testWidgets('a refusal is a notice with no rating chips', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend);

    expect(find.byKey(const Key('refusal-notice')), findsOneWidget);
    expect(find.textContaining('⊘ refused', findRichText: true), findsWidgets);
    expect(find.text('useful'), findsNothing);
    expect(find.text('edited'), findsNothing);
    expect(find.bySemanticsLabel(RegExp('^refused: /write')), findsOneWidget);
    expect(find.text('Approve this call once'), findsOneWidget);
    expect(find.text('Change mode…'), findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('no call id, no approve action', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend,
        text: 'refused /write: file_write changes files (mode: manual)');
    expect(find.byKey(const Key('refusal-notice')), findsOneWidget);
    expect(find.text('Approve this call once'), findsNothing);
    await unmountChat(tester);
  });

  testWidgets('approve once posts exactly once with the chosen validity',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend);

    await tester.tap(find.byKey(const Key('refusal-approve')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('approval-sheet')), findsOneWidget);
    expect(find.textContaining('Runs this exact call once'), findsOneWidget);
    await tester.tap(find.byKey(const Key('approval-confirm')));
    await tester.pumpAndSettle();

    expect(backend.approvals, [('3f9a12c0', const Duration(minutes: 15))]);
    expect(find.byKey(const Key('approval-approved')), findsOneWidget);
    expect(find.textContaining('nonce n_c41a', findRichText: true), findsOneWidget);
    // The action is spent: no second approval from the same notice.
    expect(find.byKey(const Key('refusal-approve')), findsNothing);
    await unmountChat(tester);
  });

  testWidgets('cancelling the sheet sends nothing', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend);
    await tester.tap(find.byKey(const Key('refusal-approve')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('approval-cancel')));
    await tester.pumpAndSettle();
    expect(backend.approvals, isEmpty);
    await unmountChat(tester);
  });

  testWidgets('a server without approvals gets the console command to copy',
      (tester) async {
    final backend = FakeChatBackend()
      ..approvalOutcome = const ApprovalOutcome(ApprovalStatus.unsupported);
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend);
    await tester.tap(find.byKey(const Key('refusal-approve')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('approval-confirm')));
    await tester.pumpAndSettle();

    expect(find.textContaining('Approve from the console: /approve 3f9a12c0',
        findRichText: true), findsOneWidget);
    String? copied;
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') {
        copied = (call.arguments as Map)['text'] as String?;
      }
      return null;
    });
    await tester.tap(find.byKey(const Key('approval-copy')));
    await tester.pump();
    expect(copied, '/approve 3f9a12c0');
    await unmountChat(tester);
  });

  testWidgets('a 403 says who can approve', (tester) async {
    final backend = FakeChatBackend()
      ..approvalOutcome = const ApprovalOutcome(ApprovalStatus.forbidden);
    await pumpChat(tester, backend);
    await _sendRefused(tester, backend);
    await tester.tap(find.byKey(const Key('refusal-approve')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('approval-confirm')));
    await tester.pumpAndSettle();
    expect(
        find.textContaining('Approvals need a developer or admin account',
            findRichText: true),
        findsOneWidget);
    await unmountChat(tester);
  });

  group('SonderApiChatBackend.approveCall', () {
    Future<ApprovalOutcome> approveWith(http.Response Function() reply,
        List<http.Request> seen) {
      final client = MockClient((request) async {
        seen.add(request);
        return reply();
      });
      return http.runWithClient(
        () => SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
            .approveCall('3f9a12c0', ttl: const Duration(minutes: 5)),
        () => client,
      );
    }

    test('posts once to the approvals route (server S2)', () async {
      final seen = <http.Request>[];
      final outcome = await approveWith(
          () => http.Response(jsonEncode({'ok': true, 'nonce': 'n_1'}), 200),
          seen);
      expect(outcome.status, ApprovalStatus.approved);
      expect(outcome.nonce, 'n_1');
      expect(seen, hasLength(1));
      expect(seen.single.method, 'POST');
      expect(seen.single.url.path, '/v1/approvals/3f9a12c0');
      expect(jsonDecode(seen.single.body), {'ttl_seconds': 300});
      expect(seen.single.headers['Authorization'], 'Bearer k');
    });

    test('404 means the server has no approvals route', () async {
      final outcome = await approveWith(
          () => http.Response('{"error":{"message":"not found"}}', 404), []);
      expect(outcome.status, ApprovalStatus.unsupported);
    });

    test('403 names the role', () async {
      final outcome = await approveWith(
          () => http.Response(
              '{"error":{"message":"forbidden","code":"FORBIDDEN"}}', 403),
          []);
      expect(outcome.status, ApprovalStatus.forbidden);
      expect(outcome.message, 'Approvals need a developer or admin account.');
    });
  });
}
