// P0-1: every non-2xx is readable, carries the server's code, and never shows
// a raw body or a Dart map. Bodies are the server's own (test/fixtures/server).
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';

import 'fixtures/server_fixtures.dart';

class _Case {
  final String name;
  final int status;
  final String fixture;
  final Map<String, String> headers;
  final String code;
  final Pattern message;
  const _Case(this.name, this.status, this.fixture, this.code, this.message,
      {this.headers = jsonHeaders});
}

final _cases = <_Case>[
  const _Case('421 host allowlist', 421, 'host_not_allowed_421.json',
      'HOST_NOT_ALLOWED', 'The server at mypc.local refused this address'),
  _Case('403 FORBIDDEN envelope', 403, 'permission_mode_forbidden_403.json',
      'FORBIDDEN', RegExp(r'^Only an administrator can ')),
  const _Case(
      '409 already completed',
      409,
      'idempotent_action_completed_409.json',
      'IDEMPOTENT_ACTION_COMPLETED',
      'already ran'),
  const _Case('422 key reused', 422, 'idempotency_key_reused_422.json',
      'IDEMPOTENCY_KEY_REUSED', 'same retry key'),
  const _Case(
      '429 sign-in limit',
      429,
      'auth_rate_limited_429.json',
      'AUTH_RATE_LIMITED',
      'Too many failed sign-ins from this network. '
          'Try again in 42 s.',
      headers: {...jsonHeaders, 'retry-after': '42'}),
  const _Case(
      '503 receipt unavailable',
      503,
      'idempotency_receipt_unavailable_503.json',
      'IDEMPOTENCY_RECEIPT_UNAVAILABLE',
      'Try again in 1 s',
      headers: {...jsonHeaders, 'retry-after': '1'}),
  const _Case(
      '500 non-JSON', 500, 'internal_500.txt', '', 'Server returned HTTP 500.',
      headers: {'content-type': 'text/plain'}),
];

typedef _Call = Future<Object?> Function(SonderApi api);

final _calls = <String, _Call>{
  'listModels': (api) => api.listModels(),
  'systemInfo': (api) => api.systemInfo(),
  'fetchCommands': (api) => api.fetchCommands(),
  'completeCommands': (api) => api.completeCommands('/mo'),
  'commandHelp': (api) => api.commandHelp('mode'),
  'fetchPermissionMode': (api) => api.fetchPermissionMode(),
  'setPermissionMode': (api) => api.setPermissionMode('auto'),
  'chatDetailed': (api) => api.chatDetailed(const []),
  'login': (api) => api.login('bob', 'pw'),
  'register': (api) => api.register('bob', 'pw'),
  'workRuns.list': (api) => api.workRuns.list(),
};

void _expectReadable(SonderException e) {
  for (final text in [e.message, e.remedy, e.toString()]) {
    expect(text, isNot(contains('{code:')));
    expect(text, isNot(contains('{message:')));
    expect(text, isNot(contains('Map<')));
    expect(text, isNot(contains('_Map')));
    expect(text, isNot(contains('{"')));
  }
}

void main() {
  for (final c in _cases) {
    for (final call in _calls.entries) {
      test('${call.key}: ${c.name}', () async {
        final api = SonderApi(baseUrl: 'https://mypc.local:11435');
        Object? caught;
        await http.runWithClient(
          () async {
            try {
              await call.value(api);
            } catch (e) {
              caught = e;
            }
          },
          () => MockClient((r) async =>
              fixtureResponse(c.fixture, c.status, headers: c.headers)),
        );
        expect(caught, isA<SonderException>());
        final e = caught! as SonderException;
        expect(e.status, c.status);
        expect(e.code, c.code);
        _expectReadable(e);
        if (call.key == 'setPermissionMode' && c.status == 403) {
          expect(e.message,
              'Only an administrator can change the permission mode.');
        } else if (call.key == 'register' && c.status == 403) {
          expect(e.message, 'Only an administrator can create accounts.');
        } else {
          expect(e.message, contains(c.message));
        }
      });
    }
  }

  test('Retry-After is parsed into retryAfter', () {
    final e = responseException(
      fixtureResponse('auth_rate_limited_429.json', 429,
          headers: {...jsonHeaders, 'retry-after': '42'}),
      'fallback',
    );
    expect(e.retryAfter, const Duration(seconds: 42));
    expect(e.retryAfterSeconds, 42);
    expect(e.retryable, isTrue);
    expect(e.correlationId, 'req_1f0c2b7e9a4d4c1b8e3f6a5d2c1b0a99');
  });

  test('describeServerError names the host and the setting for 421', () {
    final raw = responseException(
        fixtureResponse('host_not_allowed_421.json', 421), 'fallback');
    final d = describeServerError(raw, Uri.parse('http://mypc.local:11435'));
    expect(
      d.message,
      "The server at mypc.local refused this address. Connect with the PC's "
      'IP (or 127.0.0.1 with `adb reverse`), or add `mypc.local` to '
      '`[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS` on the PC.',
    );
    expect(d.remedy, 'SONDER_ALLOWED_HOSTS=mypc.local');
    expect(d.isHostNotAllowed, isTrue);
    expect(d.code, 'HOST_NOT_ALLOWED');
  });

  test('421 from the Android emulator host suggests adb reverse', () {
    final raw = responseException(
        fixtureResponse('host_not_allowed_421.json', 421), 'fallback');
    final d = describeServerError(raw, Uri.parse('http://10.0.2.2:11435'));
    expect(d.remedy, contains('adb reverse tcp:11435 tcp:11435'));
  });

  test('a 421 without a body is still HOST_NOT_ALLOWED', () {
    final raw = responseException(http.Response('', 421), 'fallback');
    expect(raw.code, 'HOST_NOT_ALLOWED');
  });

  test('permission-mode 400 keeps the server wording, not a map', () async {
    Object? caught;
    await http.runWithClient(() async {
      try {
        await SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .setPermissionMode('bogus');
      } catch (e) {
        caught = e;
      }
    },
        () => MockClient((r) async =>
            fixtureResponse('permission_mode_unknown_400.json', 400)));
    final e = caught! as SonderException;
    expect(e.message,
        "unknown mode 'bogus'. modes: plan, manual, acceptEdits, auto");
    _expectReadable(e);
  });

  test('permission-mode POST carries a fresh Idempotency-Key per call',
      () async {
    final keys = <String?>[];
    await http.runWithClient(() async {
      final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
      await api.setPermissionMode('plan');
      await api.setPermissionMode('plan');
    },
        () => MockClient((r) async {
              keys.add(r.headers['Idempotency-Key']);
              return http.Response('{"mode":"plan","label":"plan"}', 200);
            }));
    expect(keys, hasLength(2));
    expect(keys[0], matches(RegExp(r'^mode-[0-9a-f]{32}$')));
    expect(keys[0], isNot(keys[1]));
  });

  test('agent-lanes bare-string error code is read, not stringified', () {
    final e = responseException(
      http.Response('{"error": "FORBIDDEN", "message": "not your lane"}', 403),
      'fallback',
    );
    expect(e.code, 'FORBIDDEN');
    expect(e.message, 'not your lane');
  });

  test('an error object without message falls back, never prints the map', () {
    final e = responseException(
        http.Response('{"error": {"code": "FORBIDDEN"}}', 403),
        'Server returned HTTP 403.');
    expect(e.message, 'Server returned HTTP 403.');
    final d = describeServerError(e, Uri.parse('https://pc.test'),
        action: 'view compute inventory');
    expect(d.message, 'Only an administrator can view compute inventory.');
  });

  test('401 keeps a specific server reason and adds a remedy', () {
    final e = describeServerError(
      responseException(
          http.Response(
              '{"error":{"message":"API key expired","code":"AUTH_EXPIRED"}}',
              401),
          'Unauthorized'),
      Uri.parse('https://pc.test'),
    );
    expect(e.message, 'API key expired');
    expect(e.remedy, contains('API key'));
  });

  test('launcher errors never print a Dart map', () async {
    Object? caught;
    await http.runWithClient(() async {
      try {
        await SonderLauncherApi(
                baseUrl: 'http://127.0.0.1:11436', token: 't' * 32)
            .status();
      } catch (e) {
        caught = e;
      }
    },
        () => MockClient(
            (r) async => fixtureResponse('host_not_allowed_421.json', 421)));
    final e = caught! as SonderException;
    expect(e.message, 'host is not allowed for this listener');
    expect(e.code, 'HOST_NOT_ALLOWED');
  });
}
