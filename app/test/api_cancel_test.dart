// P0-6: each chat call owns its client; Stop cancels exactly that call.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

http.Response _answer(String text) => http.Response(
      jsonEncode({
        'choices': [
          {
            'message': {'role': 'assistant', 'content': text}
          }
        ]
      }),
      200,
    );

String _lastUser(http.BaseRequest r) {
  final body = jsonDecode((r as http.Request).body) as Map<String, dynamic>;
  return ((body['messages'] as List).last as Map)['content'] as String;
}

void main() {
  test(
      'quality-a11y probe: 400 ms turn + 20 ms passive call, then cancel '
      '-> cancelled, the turn client closed once', () async {
    final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
    final token = CancelToken();
    String outcome = 'pending';
    var passiveDone = false;

    final clients = await recordClients(() async {
      final turn = api.chatDetailed(
          const [ChatMessage(role: Role.user, content: 'long turn')],
          cancel: token).then<void>((_) {
        outcome = 'answered';
      }, onError: (Object e) {
        outcome = e is SonderException && e.isCancelled ? 'cancelled' : '$e';
      });
      await pause(5);
      final passive = api.recordFeedback('/copied').then((_) {
        passiveDone = true;
      });
      await pause(60); // The passive call (20 ms) has finished by now.
      token.cancel();
      await Future.wait([turn, passive]);
    }, (request) async {
      await pause(_lastUser(request) == '/copied' ? 20 : 400);
      return _answer('ok');
    });

    expect(outcome, 'cancelled');
    expect(passiveDone, isTrue);
    final turnClient = clients
        .firstWhere((c) => c.requests.any((r) => _lastUser(r) == 'long turn'));
    final passiveClient = clients
        .firstWhere((c) => c.requests.any((r) => _lastUser(r) == '/copied'));
    expect(identical(turnClient, passiveClient), isFalse);
    expect(turnClient.closes, 1);
    expect(passiveClient.closes, 1);
  });

  test('cancelChat() stops the turn but not a passive call', () async {
    final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
    String outcome = 'pending';
    var passive = 'pending';
    await recordClients(() async {
      final turn = api.chatDetailed(
          const [ChatMessage(role: Role.user, content: 'long turn')]).then(
        (_) => outcome = 'answered',
        onError: (Object e) => outcome =
            e is SonderException && e.isCancelled ? 'cancelled' : '$e',
      );
      final feedback = api
          .recordFeedback('/copied')
          .then((_) => passive = 'ok', onError: (Object e) => passive = '$e');
      await pause(10);
      api.cancelChat();
      await Future.wait([turn, feedback]);
    }, (request) async {
      await pause(_lastUser(request) == '/copied' ? 40 : 300);
      return _answer('ok');
    });
    expect(outcome, 'cancelled');
    expect(passive, 'ok');
  });

  test('the client is closed after a successful turn', () async {
    final clients = await recordClients(() async {
      final reply = await SonderApi(baseUrl: 'http://127.0.0.1:11435')
          .chatDetailed(const [ChatMessage(role: Role.user, content: 'hi')]);
      expect(reply.text, 'fine');
    }, (_) async => _answer('fine'));
    expect(clients, hasLength(1));
    expect(clients.single.closes, 1);
  });

  test('the client is closed after a server error', () async {
    final clients = await recordClients(() async {
      await expectLater(
        SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .chatDetailed(const [ChatMessage(role: Role.user, content: 'hi')]),
        throwsA(isA<SonderException>()),
      );
    }, (_) async => http.Response('nope', 502));
    expect(clients.single.closes, 1);
  });

  test('a token cancelled before the call sends nothing', () async {
    final token = CancelToken()..cancel();
    final clients = await recordClients(() async {
      await expectLater(
        SonderApi(baseUrl: 'http://127.0.0.1:11435').chatDetailed(
            const [ChatMessage(role: Role.user, content: 'hi')],
            cancel: token),
        throwsA(isA<SonderException>()
            .having((e) => e.code, 'code', SonderException.cancelledCode)),
      );
    }, (_) async => _answer('never'));
    expect(clients.expand((c) => c.requests), isEmpty);
  });

  test('CancelToken runs listeners once and unregisters', () {
    final token = CancelToken();
    var calls = 0;
    final off = token.onCancel(() => calls++);
    token.onCancel(() => calls += 10);
    off();
    token.cancel();
    token.cancel();
    expect(calls, 10);
    expect(token.isCancelled, isTrue);
    var late = 0;
    token.onCancel(() => late++);
    expect(late, 1);
  });
}
