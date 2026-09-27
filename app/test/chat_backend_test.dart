// Integration of lane C's ChatBackend seam with lane A's real SonderApi:
// one long-lived API instance, a CancelToken per turn, streamed deltas,
// recordFeedback on its own request, and the S5 `history: "client"` opt-out.
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

const _sse = {'content-type': 'text/event-stream'};

http.StreamedResponse _streamed(List<String> chunks,
    {int gapMs = 1, bool hang = false}) {
  final controller = StreamController<List<int>>();
  () async {
    for (final chunk in chunks) {
      await pause(gapMs);
      if (controller.isClosed) return;
      controller.add(utf8.encode(chunk));
    }
    if (!hang) await controller.close();
  }();
  return http.StreamedResponse(controller.stream, 200, headers: _sse);
}

http.StreamedResponse _json(String text) => http.StreamedResponse(
      Stream.value(utf8.encode(jsonEncode({
        'choices': [
          {
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
          }
        ]
      }))),
      200,
      headers: const {'content-type': 'application/json'},
    );

Map<String, dynamic> _body(String body) =>
    jsonDecode(body) as Map<String, dynamic>;

String _lastUser(String body) =>
    ((_body(body)['messages'] as List).last as Map)['content'] as String;

TurnRequest _turn(String text, {String? historyMode}) => TurnRequest(
      history: [ChatMessage(role: Role.user, content: text)],
      model: 'sonder',
      sessionId: 's1',
      historyMode: historyMode,
    );

Future<(List<TurnEvent>, Object?)> _collect(ChatTurn turn) async {
  final events = <TurnEvent>[];
  Object? error;
  try {
    await for (final e in turn.events) {
      events.add(e);
    }
  } catch (e) {
    error = e;
  }
  return (events, error);
}

void main() {
  test('fromSettings-style construction keeps one SonderApi for every call',
      () {
    final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
    final backend = SonderApiChatBackend.withApi(api);
    expect(identical(backend.api, api), isTrue);
    expect(backend.serverUrl, 'http://127.0.0.1:11435');
    final built = SonderApiChatBackend(baseUrl: 'http://pc:11435');
    expect(identical(built.api, built.api), isTrue);
    expect(built.api, isA<SonderApi>());
  });

  test('a turn streams: deltas in order, then one TurnDone', () async {
    late (List<TurnEvent>, Object?) result;
    String? sentBody;
    await recordStreamingClients(() async {
      final backend = SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435');
      result = await _collect(backend.startTurn(_turn('hi')));
    }, (request, body) async {
      sentBody = await body.bytesToString();
      return _streamed([serverFixture('stream_ok.sse')]);
    });
    final (events, error) = result;
    expect(error, isNull);
    expect(_body(sentBody!)['stream'], isTrue);
    expect(_body(sentBody!).containsKey('history'), isFalse);
    expect(events.whereType<TurnDelta>().map((d) => d.text), ['PELI', 'CAN']);
    expect(events.last, isA<TurnDone>());
    expect((events.last as TurnDone).reply.text, 'PELICAN');
    expect(events.whereType<TurnDone>(), hasLength(1));
  });

  test('a plain JSON answer still ends the turn with one TurnDone', () async {
    late (List<TurnEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435')
              .startTurn(_turn('hi')));
    }, (request, body) async => _json('plain answer'));
    final (events, error) = result;
    expect(error, isNull);
    expect(events.whereType<TurnDone>(), hasLength(1));
    expect(events.last, isA<TurnDone>());
    expect((events.last as TurnDone).reply.text, 'plain answer');
    expect(events.whereType<TurnDelta>().map((d) => d.text).join(),
        anyOf('', 'plain answer'));
  });

  test('historyMode "client" reaches the server as history: client (S5)',
      () async {
    String? sentBody;
    await recordStreamingClients(() async {
      await _collect(SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435')
          .startTurn(_turn('after a rotation', historyMode: 'client')));
    }, (request, body) async {
      sentBody = await body.bytesToString();
      return _json('ok');
    });
    expect(_body(sentBody!)['history'], 'client');
  });

  test(
      'Stop cancels exactly its own turn: a second turn and a feedback '
      'call on the same backend still finish', () async {
    final backend = SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435');
    late (List<TurnEvent>, Object?) stopped;
    late (List<TurnEvent>, Object?) other;
    var feedback = 'pending';
    final clients = await recordStreamingClients(() async {
      final a = backend.startTurn(_turn('turn a'));
      final b = backend.startTurn(_turn('turn b'));
      final collectA = _collect(a);
      final collectB = _collect(b);
      final passive = backend
          .recordFeedback('/copied', _turn('ignored'))
          .then((_) => feedback = 'ok', onError: (Object e) => feedback = '$e');
      await pause(20);
      a.cancel();
      a.cancel(); // Idempotent.
      stopped = await collectA;
      other = await collectB;
      await passive;
    }, (request, body) async {
      final text = _lastUser(await body.bytesToString());
      if (text == 'turn a') {
        return _streamed(['data: {"choices":[{"delta":{"content":"x"}}]}\n\n'],
            hang: true);
      }
      await pause(text == '/copied' ? 5 : 60);
      return _json('answer to $text');
    });
    // The cancelled turn ends quietly: no error, no final reply.
    expect(stopped.$2, isNull);
    expect(stopped.$1.whereType<TurnDone>(), isEmpty);
    expect(other.$2, isNull);
    expect((other.$1.last as TurnDone).reply.text, 'answer to turn b');
    expect(feedback, 'ok');
    // Every request had its own client, and every client was closed.
    expect(clients, hasLength(3));
    for (final client in clients) {
      expect(client.closes, 1);
    }
  });

  test('recordFeedback posts once, non-streamed, with the turn context',
      () async {
    final seen = <String>[];
    await recordClients(() async {
      await SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435')
          .recordFeedback('/accept', _turn('ignored'));
    }, (request) async {
      seen.add(request.body);
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'noted'}
              }
            ]
          }),
          200);
    });
    expect(seen, hasLength(1));
    final body = _body(seen.single);
    expect(body['stream'], isFalse);
    expect(body['session'], 's1');
    expect(_lastUser(seen.single), '/accept');
  });

  test(
      'SonderApi.chatDetailed sends history only when asked; bad values '
      'never reach the wire', () async {
    final bodies = <String>[];
    await recordClients(() async {
      final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
      const hi = [ChatMessage(role: Role.user, content: 'hi')];
      await api.chatDetailed(hi);
      await api.chatDetailed(hi, history: 'client');
      await expectLater(
          api.chatDetailed(hi, history: 'everything'), throwsArgumentError);
    }, (request) async {
      bodies.add(request.body);
      return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'ok'}
              }
            ]
          }),
          200);
    });
    expect(bodies, hasLength(2));
    expect(_body(bodies[0]).containsKey('history'), isFalse);
    expect(_body(bodies[1])['history'], 'client');
  });
}
