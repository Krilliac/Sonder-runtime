import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/app_control.dart';

const sessionId = '0123456789abcdef0123456789abcdef';
final controlToken = 'sac1.$sessionId.${'A' * 43}';
Map<String, Object> enrollment() => {
      'ok': true,
      'control_session_id': sessionId,
      'control_token': controlToken,
      'runtime_id': 'runtime-a',
      'expires_at': 4102444800,
    };
AppControlContext context([String token = 'account']) => AppControlContext(
      serverUrl: 'https://host.test',
      deploymentKey: 'deployment',
      account: AccountSession(token: token, origin: 'https://host.test'),
    );

void main() {
  test('unknown mutation retries exact immutable bytes and blocks new changes',
      () async {
    final bodies = <String>[];
    final client = AppControlClient(
        context: context,
        transportFactory: () => MockClient((r) async {
              if (r.url.path.endsWith('/enroll')) {
                return http.Response(jsonEncode(enrollment()), 201);
              }
              bodies.add(r.body);
              if (bodies.length == 1) {
                throw http.ClientException('private response');
              }
              final id = (jsonDecode(r.body) as Map)['command_id'];
              return http.Response(
                  jsonEncode({
                    'ok': true,
                    'receipt': {
                      'command_id': id,
                      'action': 'create_binding',
                      'result_code': 'COMMITTED',
                      'entity_id': 'binding-1',
                      'entity_revision': 1,
                      'selection_epoch': null
                    }
                  }),
                  200);
            }));
    addTearDown(client.dispose);
    await client.enroll(project: 'demo', password: 'password');
    await expectLater(client.createBinding(title: 'Original'),
        throwsA(isA<AppControlFailure>()));
    expect(client.mutationPending, isTrue);
    await expectLater(client.createBinding(title: 'Changed'),
        throwsA(isA<AppControlFailure>()));
    expect(bodies.length, 1);
    await client.retryMutation();
    expect(bodies[0], bodies[1]);
    expect(client.mutationPending, isFalse);
  });

  test('clear projection retains epoch and uses it on next mutation', () async {
    final client = AppControlClient(
        context: context,
        transportFactory: () => MockClient((r) async {
              if (r.url.path.endsWith('/enroll')) {
                return http.Response(jsonEncode(enrollment()), 201);
              }
              if (r.method == 'GET') {
                return http.Response(
                    '{"ok":true,"selection":{"selection_id":"s1","binding_id":null,"binding_revision":null,"epoch":7}}',
                    200);
              }
              final body = jsonDecode(r.body) as Map;
              expect(body['expected_epoch'], 7);
              return http.Response(
                  jsonEncode({
                    'ok': true,
                    'receipt': {
                      'command_id': body['command_id'],
                      'action': 'clear_selection',
                      'result_code': 'COMMITTED',
                      'entity_id': 's1',
                      'entity_revision': null,
                      'selection_epoch': 8
                    }
                  }),
                  200);
            }));
    addTearDown(client.dispose);
    await client.enroll(project: 'demo', password: 'password');
    await client.loadSelection();
    await client.clearSelection();
  });

  test('origin switch expiry and disposal never send old bearer', () async {
    var scope = context();
    var calls = 0;
    var now = DateTime.utc(2026);
    final client = AppControlClient(
        context: () => scope,
        clock: () => now,
        transportFactory: () => MockClient((r) async {
              calls++;
              return http.Response(
                  jsonEncode({
                    ...enrollment(),
                    'expires_at': now
                            .add(const Duration(seconds: 1))
                            .millisecondsSinceEpoch /
                        1000
                  }),
                  201);
            }));
    await client.enroll(project: 'demo', password: 'password');
    now = now.add(const Duration(seconds: 2));
    await expectLater(client.loadBindings(), throwsA(isA<AppControlFailure>()));
    expect(calls, 1);
    await client.enroll(project: 'demo', password: 'password');
    scope = AppControlContext(
        serverUrl: 'https://other.test',
        deploymentKey: 'deployment',
        account: scope.account);
    await expectLater(client.loadBindings(), throwsA(isA<AppControlFailure>()));
    expect(calls, 2);
    client.dispose();
    await expectLater(client.loadBindings(), throwsA(isA<AppControlFailure>()));
    expect(calls, 2);
    final fresh = AppControlClient(context: context);
    expect(fresh.hasSession, isFalse);
    fresh.dispose();
  });

  test('redirect or malformed credential response is unknown and never adopted',
      () async {
    for (final status in [302, 201]) {
      var calls = 0;
      final client = AppControlClient(
          context: context,
          transportFactory: () => MockClient((r) async {
                calls++;
                expect(r.followRedirects, isFalse);
                return http.Response(
                    jsonEncode({
                      ...enrollment(),
                      'control_token': 'secret-from-another-scope'
                    }),
                    status,
                    headers: {'location': 'https://other.test'});
              }));
      await expectLater(
          client.enroll(project: 'demo', password: 'password'),
          throwsA(isA<AppControlFailure>()
              .having((e) => e.unknown, 'unknown', true)));
      expect(calls, 1);
      expect(client.hasSession, isFalse);
      expect(client.enrollmentPending, isTrue);
      client.dispose();
    }
  });

  test('bounded pagination rejects nonadvancing cursor without replacing data',
      () async {
    final client = AppControlClient(
        context: context,
        transportFactory: () => MockClient((r) async {
              if (r.method == 'POST') {
                return http.Response(jsonEncode(enrollment()), 201);
              }
              return http.Response(
                  '{"ok":true,"items":[],"next_position":0}', 200);
            }));
    addTearDown(client.dispose);
    await client.enroll(project: 'demo', password: 'password');
    await expectLater(client.loadBindings(), throwsA(isA<AppControlFailure>()));
    expect(client.bindings, isEmpty);
  });

  test('dedicated exact headers, no redirects, memory reset before new account',
      () async {
    var scope = context();
    final requests = <http.Request>[];
    final client = AppControlClient(
        context: () => scope,
        transportFactory: () => MockClient((r) async {
              requests.add(r);
              return http.Response(
                  jsonEncode(r.url.path.endsWith('/enroll')
                      ? enrollment()
                      : {'ok': true, 'items': [], 'next_position': null}),
                  r.url.path.endsWith('/enroll') ? 201 : 200);
            }));
    addTearDown(client.dispose);
    await client.enroll(project: 'demo', password: 'password');
    await client.loadBindings();
    expect(
        requests.every(
            (r) => !r.followRedirects && r.url.origin == 'https://host.test'),
        isTrue);
    expect(requests.first.headers['X-Sonder-Account-Token'], 'account');
    expect(requests.first.headers['Authorization'], 'Bearer deployment');
    expect(requests.first.headers.containsKey('X-Sonder-App-Control'), isFalse);
    expect(requests.last.headers['X-Sonder-App-Control'], controlToken);
    expect(client.toString(), isNot(contains(controlToken)));
    scope = context('other-account');
    await expectLater(client.loadBindings(), throwsA(isA<AppControlFailure>()));
    expect(requests.length, 2);
    expect(client.hasSession, isFalse);
  });

  test(
      'unknown enrollment retains identity, never replays automatically or retains password',
      () async {
    final ids = <String>[];
    final client = AppControlClient(
        context: context,
        transportFactory: () => MockClient((r) async {
              ids.add((jsonDecode(r.body) as Map)['command_id'] as String);
              if (ids.length == 1) {
                throw http.ClientException('private password response');
              }
              return http.Response(
                  '{"ok":false,"error":{"code":"CREDENTIAL_DELIVERY_UNKNOWN"}}',
                  409);
            }));
    addTearDown(client.dispose);
    await expectLater(client.enroll(project: 'demo', password: 'secret'),
        throwsA(isA<AppControlFailure>()));
    expect(ids.length, 1);
    expect(client.enrollmentPending, isTrue);
    await expectLater(client.reconcileEnrollment(password: 'secret-again'),
        throwsA(isA<AppControlFailure>()));
    expect(ids[0], ids[1]);
    expect(client.hasSession, isFalse);
    expect(client.enrollmentPending, isFalse);
  });

  test('late enrollment cannot restore credentials after context change',
      () async {
    var scope = context();
    final response = Completer<http.Response>();
    final client = AppControlClient(
        context: () => scope,
        transportFactory: () => MockClient((r) => response.future));
    addTearDown(client.dispose);
    final future = client.enroll(project: 'demo', password: 'secret');
    scope = context('new-account');
    client.synchronize();
    response.complete(http.Response(jsonEncode(enrollment()), 201));
    await expectLater(future, throwsA(isA<AppControlFailure>()));
    expect(client.hasSession, isFalse);
  });
}
