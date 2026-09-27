// P0-7: the local fallback is used only when the primary refused the
// connection or did not resolve; a timeout never re-sends the turn.
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';

import 'fixtures/server_fixtures.dart';

const _hi = [ChatMessage(role: Role.user, content: 'hi')];

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

SonderApi _api({Duration timeout = const Duration(minutes: 5)}) => SonderApi(
      baseUrl: 'https://pc.example',
      apiKey: 'deployment',
      accountSession:
          AccountSession(token: 'account', origin: 'https://pc.example'),
      chatTimeout: timeout,
    );

void main() {
  test('a timeout sends exactly one POST, to the primary, and closes it',
      () async {
    final clients = await recordClients(() async {
      await expectLater(
        _api(timeout: const Duration(milliseconds: 80)).chatDetailed(_hi),
        throwsA(isA<SonderException>()
            .having((e) => e.code, 'code', SonderException.timeoutCode)),
      );
      await pause(250); // Let the slow handler finish; nothing new is sent.
    }, (_) async {
      await pause(200);
      return _answer('late');
    });
    final requests = clients.expand((c) => c.requests).toList();
    expect(requests, hasLength(1));
    expect(requests.single.url.host, 'pc.example');
    expect(clients.single.closes, 1);
  });

  test('connection refused -> one POST to the fallback, with no credentials',
      () async {
    final clients = await recordClients(() async {
      final reply = await _api().chatDetailed(_hi);
      expect(reply.text, contains('Fell back to local server'));
      expect(reply.text, endsWith('local answer'));
    }, (request) async {
      if (request.url.host == 'pc.example') {
        throw http.ClientException(
            'SocketException: Connection refused (OS Error: Connection '
            'refused, errno = 111), address = pc.example, port = 443');
      }
      return _answer('local answer');
    });
    final requests = clients.expand((c) => c.requests).toList();
    expect(requests.map((r) => r.url.host), ['pc.example', '127.0.0.1']);
    final fallback = requests.last;
    expect(fallback.headers.containsKey('Authorization'), isFalse);
    expect(fallback.headers.containsKey('X-Sonder-Account-Token'), isFalse);
    expect(clients.every((c) => c.closes == 1), isTrue);
  });

  test('failed DNS falls back too', () async {
    final hosts = <String>[];
    await recordClients(() async {
      await _api().chatDetailed(_hi);
    }, (request) async {
      hosts.add(request.url.host);
      if (request.url.host == 'pc.example') {
        throw http.ClientException(
            "Failed host lookup: 'pc.example' (OS Error: No address "
            'associated with hostname, errno = 7)');
      }
      return _answer('ok');
    });
    expect(hosts, ['pc.example', '127.0.0.1']);
  });

  for (final error in [
    'HandshakeException: Handshake error in client (OS Error: '
        'CERTIFICATE_VERIFY_FAILED)',
    'Connection closed before full header was received',
    'Connection reset by peer',
    'unreachable',
  ]) {
    test('no fallback after: $error', () async {
      final hosts = <String>[];
      await recordClients(() async {
        await expectLater(
            _api().chatDetailed(_hi), throwsA(isA<SonderException>()));
      }, (request) async {
        hosts.add(request.url.host);
        throw http.ClientException(error);
      });
      expect(hosts, ['pc.example']);
    });
  }

  test('passive feedback never falls back', () async {
    final hosts = <String>[];
    await recordClients(() async {
      await expectLater(
          _api().recordFeedback('/copied'), throwsA(isA<SonderException>()));
    }, (request) async {
      hosts.add(request.url.host);
      throw http.ClientException('Connection refused');
    });
    expect(hosts, ['pc.example']);
  });

  test('isPreRequestConnectFailure classification', () {
    expect(
        isPreRequestConnectFailure(http.ClientException('Connection refused')),
        isTrue);
    expect(
        isPreRequestConnectFailure(http.ClientException(
            'The remote computer refused the network connection. errno = 1225')),
        isTrue);
    expect(
        isPreRequestConnectFailure(
            http.ClientException("Failed host lookup: 'mypc.local'")),
        isTrue);
    expect(isPreRequestConnectFailure(TimeoutException('x')), isFalse);
    expect(isPreRequestConnectFailure(SonderException.cancelled()), isFalse);
    expect(
        isPreRequestConnectFailure(
            http.ClientException('Connection closed before full header')),
        isFalse);
    expect(
        isPreRequestConnectFailure(
            http.ClientException('XMLHttpRequest error.')),
        isFalse);
  });
}
