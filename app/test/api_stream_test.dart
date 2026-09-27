// P1-1: SSE client. Fixtures mirror serve.py `_chunk` / keep-alive framing.
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

const _hi = [ChatMessage(role: Role.user, content: 'hi')];
const _sse = {'content-type': 'text/event-stream'};

/// A streamed response that emits [chunks] (each after [gapMs]) and then
/// either ends or, with [hang], stays open.
http.StreamedResponse _streamed(List<String> chunks,
    {int gapMs = 1, bool hang = false, Map<String, String> headers = _sse}) {
  final controller = StreamController<List<int>>();
  () async {
    for (final chunk in chunks) {
      await pause(gapMs);
      if (controller.isClosed) return;
      controller.add(utf8.encode(chunk));
    }
    if (!hang) await controller.close();
  }();
  return http.StreamedResponse(controller.stream, 200, headers: headers);
}

/// Split a fixture into small pieces so frames straddle chunk boundaries.
List<String> _pieces(String text, [int size = 17]) => [
      for (var i = 0; i < text.length; i += size)
        text.substring(i, i + size > text.length ? text.length : i + size),
    ];

Future<(List<ChatStreamEvent>, Object?)> _collect(
    Stream<ChatStreamEvent> s) async {
  final events = <ChatStreamEvent>[];
  Object? error;
  try {
    await for (final e in s) {
      events.add(e);
    }
  } catch (e) {
    error = e;
  }
  return (events, error);
}

void main() {
  group('SseFrameParser', () {
    test('data, comments, multi-line data and [DONE]', () {
      final p = SseFrameParser();
      expect(p.addLine(': keep-alive').single.comment, isTrue);
      expect(p.addLine(''), isEmpty);
      expect(p.addLine('data: {"a":1}'), isEmpty);
      expect(p.addLine('data: {"b":2}'), isEmpty);
      final frame = p.addLine('').single;
      expect(frame.data, '{"a":1}\n{"b":2}');
      p.addLine('event: ignored');
      p.addLine('data:[DONE]');
      expect(p.close().single.isDone, isTrue);
    });
  });

  test('content + finish + usage + [DONE]: deltas then the reply', () async {
    http.BaseRequest? seen;
    late (List<ChatStreamEvent>, Object?) result;
    final clients = await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi));
    }, (request, body) async {
      seen = request;
      return _streamed(_pieces(serverFixture('stream_ok.sse')));
    });
    final (events, error) = result;
    expect(error, isNull);
    final body = jsonDecode((seen! as http.Request).body) as Map;
    expect(body['stream'], isTrue);
    expect(seen!.headers['Accept'], 'text/event-stream');
    expect(events.first, isA<ChatStreamOpened>());
    expect(events.whereType<ChatStreamKeepAlive>(), hasLength(2));
    final deltas = events.whereType<ChatStreamDelta>().toList();
    expect(deltas.map((d) => d.text), ['PELI', 'CAN']);
    expect(deltas.last.accumulated, 'PELICAN');
    final done = events.last as ChatStreamDone;
    expect(done.reply.text, 'PELICAN');
    final m = done.reply.metadata!;
    expect(m.finishReason, 'stop');
    expect(m.requestId, 'req_c4303a90110e42eb967f40b997a7c088');
    expect(m.promptTokens, 2600);
    expect(m.completionTokens, 143);
    expect(m.modelCalls, 1);
    expect(m.status, 'complete');
    expect(m.elapsedMs, 136293);
    expect(clients.single.closes, 1);
  });

  test('partial text is visible before [DONE]', () async {
    final seen = <String>[];
    await recordStreamingClients(() async {
      await for (final e
          in SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi)) {
        if (e is ChatStreamDelta) seen.add(e.accumulated);
        if (e is ChatStreamDone) seen.add('DONE');
      }
    }, (request, body) async {
      final text = serverFixture('stream_ok.sse');
      final cut = text.indexOf('data: [DONE]');
      return _streamed([text.substring(0, cut), text.substring(cut)],
          gapMs: 30);
    });
    expect(seen, ['PELI', 'PELICAN', 'DONE']);
  });

  test('keep-alives reset the stall timer; silence then stalls', () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(SonderApi(
        baseUrl: 'http://127.0.0.1:11435',
        streamStallTimeout: const Duration(milliseconds: 150),
      ).chatStream(_hi));
    }, (request, body) async {
      // 3 keep-alives 60 ms apart (each well inside the 150 ms budget, 180 ms
      // in total), then the connection stays silent.
      return _streamed(_pieces(serverFixture('stream_keepalive_only.sse'), 13),
          gapMs: 60, hang: true);
    });
    final (events, error) = result;
    expect(events.whereType<ChatStreamKeepAlive>(), hasLength(3));
    expect(
        error,
        isA<SonderException>()
            .having((e) => e.code, 'code', SonderException.stalledCode));
  });

  test('keep-alive only, then the connection closes -> interrupted', () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi));
    },
        (request, body) async =>
            _streamed([serverFixture('stream_keepalive_only.sse')]));
    expect(
        result.$2,
        isA<SonderException>()
            .having((e) => e.code, 'code', 'STREAM_INTERRUPTED'));
  });

  test('content then object:error -> SonderException(code)', () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi));
    }, (request, body) async => _streamed([serverFixture('stream_error.sse')]));
    final (events, error) = result;
    expect(events.whereType<ChatStreamDelta>().single.text, 'partial ');
    expect(events.whereType<ChatStreamDone>(), isEmpty);
    expect(
        error,
        isA<SonderException>()
            .having((e) => e.code, 'code', 'MODEL_BACKEND_ERROR')
            .having((e) => e.message, 'message',
                'the model backend failed while generating'));
  });

  test('connection dropping mid-stream -> interrupted after partial text',
      () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi));
    }, (request, body) async {
      final controller = StreamController<List<int>>();
      () async {
        controller.add(utf8.encode(serverFixture('stream_dropped.sse')));
        await pause(5);
        controller.addError(http.ClientException('Connection reset by peer'));
        await controller.close();
      }();
      return http.StreamedResponse(controller.stream, 200, headers: _sse);
    });
    final (events, error) = result;
    expect(
        events.whereType<ChatStreamDelta>().single.accumulated, 'half an ans');
    expect(
        error,
        isA<SonderException>()
            .having((e) => e.code, 'code', 'STREAM_INTERRUPTED'));
  });

  test('an error before the headers keeps the JSON envelope path', () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://mypc.local:11435').chatStream(_hi));
    },
        (request, body) async => http.StreamedResponse(
            Stream.value(
                utf8.encode(serverFixture('host_not_allowed_421.json'))),
            421,
            headers: jsonHeaders));
    expect(result.$1, isEmpty);
    expect(
        result.$2,
        isA<SonderException>()
            .having((e) => e.code, 'code', 'HOST_NOT_ALLOWED')
            .having((e) => e.status, 'status', 421));
  });

  test('a JSON answer to a stream request is read as one completion', () async {
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'http://127.0.0.1:11435').chatStream(_hi));
    },
        (request, body) async => http.StreamedResponse(
            Stream.value(utf8.encode(serverFixture('chat_work_running.json'))),
            200,
            headers: jsonHeaders));
    final done = result.$1.whereType<ChatStreamDone>().single;
    expect(done.reply.pendingWorkRunId, startsWith('wr-'));
    expect(done.reply.text, isNot(contains('GET /v1/work-runs')));
  });

  test('Stop closes the stream and its client', () async {
    final token = CancelToken();
    late (List<ChatStreamEvent>, Object?) result;
    final clients = await recordStreamingClients(() async {
      final collecting = _collect(SonderApi(baseUrl: 'http://127.0.0.1:11435')
          .chatStream(_hi, cancel: token));
      await pause(40);
      token.cancel();
      result = await collecting;
    }, (request, body) async {
      final text = serverFixture('stream_ok.sse');
      return _streamed([text.substring(0, text.indexOf('data:')), text],
          gapMs: 20, hang: true);
    });
    expect(
        result.$2,
        isA<SonderException>()
            .having((e) => e.isCancelled, 'cancelled', isTrue));
    expect(clients.single.closes, 1);
  });

  test('refused connection falls back once, without credentials', () async {
    final hosts = <String>[];
    late (List<ChatStreamEvent>, Object?) result;
    await recordStreamingClients(() async {
      result = await _collect(
          SonderApi(baseUrl: 'https://pc.example', apiKey: 'k')
              .chatStream(_hi));
    }, (request, body) async {
      hosts.add(request.url.host);
      if (request.url.host == 'pc.example') {
        throw http.ClientException('Connection refused');
      }
      expect(request.headers.containsKey('Authorization'), isFalse);
      return _streamed([serverFixture('stream_ok.sse')]);
    });
    expect(hosts, ['pc.example', '127.0.0.1']);
    final done = result.$1.whereType<ChatStreamDone>().single;
    expect(done.reply.text, startsWith('Warning: hosted server'));
    expect(done.reply.text, endsWith('PELICAN'));
  });

  group('bounds', () {
    ChatStreamRequest req({
      Duration maxDuration = const Duration(minutes: 30),
      int maxLineChars = 64,
      int maxAnswerChars = 1 << 20,
    }) =>
        ChatStreamRequest(
          uri: Uri.parse('http://127.0.0.1:11435/v1/chat/completions'),
          headers: const {},
          body: '{}',
          stallTimeout: const Duration(milliseconds: 150),
          maxDuration: maxDuration,
          maxLineChars: maxLineChars,
          maxAnswerChars: maxAnswerChars,
        );

    Future<(List<ChatStreamEvent>, Object?, List<RecordingClient>)> run(
        ChatStreamRequest request,
        http.StreamedResponse Function() respond) async {
      late (List<ChatStreamEvent>, Object?) result;
      final clients = await recordStreamingClients(() async {
        result = await _collect(openChatStream(request));
      }, (_, __) async => respond());
      return (result.$1, result.$2, clients);
    }

    Matcher tooLarge = isA<SonderException>()
        .having((e) => e.code, 'code', streamTooLargeCode);

    test('a huge line with no newline fails instead of buffering', () async {
      // 10 chunks of 20 chars, never a line break, connection kept open.
      final (_, error, clients) = await run(
          req(), () => _streamed(List.filled(10, 'x' * 20), hang: true));
      expect(error, tooLarge);
      expect(clients.single.closes, 1);
    });

    test('a frame of many short data lines is bounded too', () async {
      final (_, error, _) = await run(
          req(), () => _streamed(List.filled(10, 'data: 0123456789\n')));
      expect(error, tooLarge);
    });

    test('an answer over the cap fails', () async {
      String chunk(String t) => 'data: ${jsonEncode({
                'choices': [
                  {
                    'delta': {'content': t}
                  }
                ]
              })}\n\n';
      final (events, error, _) = await run(
          req(maxLineChars: 1024, maxAnswerChars: 8),
          () => _streamed([chunk('12345'), chunk('67890')]));
      expect(events.whereType<ChatStreamDelta>(), hasLength(1));
      expect(error, tooLarge);
    });

    test('never-ending keep-alives end at maxDuration', () async {
      // A keep-alive every 30 ms keeps the stall timer (150 ms) happy
      // forever; the hard cap still ends the turn.
      final controller = StreamController<List<int>>();
      final ticker = Timer.periodic(const Duration(milliseconds: 30), (_) {
        if (!controller.isClosed) controller.add(utf8.encode(': ka\n\n'));
      });
      final (events, error, clients) = await run(
          req(maxDuration: const Duration(milliseconds: 400)),
          () => http.StreamedResponse(controller.stream, 200, headers: _sse));
      ticker.cancel();
      await controller.close();
      expect(events.whereType<ChatStreamKeepAlive>().length, greaterThan(5));
      expect(
          error,
          isA<SonderException>()
              .having((e) => e.code, 'code', SonderException.timeoutCode));
      expect(clients.single.closes, 1);
    });

    test('malformed JSON frames are skipped, the answer still completes',
        () async {
      final (events, error, _) = await run(
          req(maxLineChars: 4096),
          () => _streamed([
                'data: {not json\n\n',
                'data: [1,2]\n\n',
                serverFixture('stream_ok.sse'),
              ]));
      expect(error, isNull);
      expect(events.whereType<ChatStreamDone>().single.reply.text,
          endsWith('PELICAN'));
    });
  });
}
