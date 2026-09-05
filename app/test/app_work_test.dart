import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/app_control.dart';
import 'app_control_test.dart' as fixture;

Map<String, Object?> work({String state = 'prepared', int revision = 1}) => {
      'work_id': 'a' * 64,
      'state': state,
      'revision': revision,
      'project': 'demo',
      'expires_at': 4102444800,
      'options': {
        'tier': 'auto',
        'max_steps': 8,
        'allow_web': false,
        'allow_location': false
      }
    };

void main() {
  test(
      'bounded pending status retains exact immutable evidence and rejects malformed authority hints',
      () {
    final value = work(state: 'verification_pending', revision: 5);
    final identity = <String, Object?>{
      'continuation_id': 'c1',
      'verification_id': 'v1',
      'parent_session_id': 'parent:one',
      'parent_grant_revision': 1,
      'generation': 2,
      'bundle_digest': 'c' * 64,
      'command_id': 'cmd1',
      'projection_digest': 'd' * 64,
      'projection_revision': 1
    };
    value['pending'] = {
      'kind': 'verification_approval',
      'identity': identity,
      'approval': {
        'tool': 'workspace_run',
        'surface': 'app-control',
        'call_digest': 'b' * 64,
        'call_id': 'b' * 16,
        'expires_at': 4102444800
      }
    };
    final decoded = AppManagedWork.decode(value);
    expect(decoded.verificationIdentity!['bundle_digest'], 'c' * 64);
    identity['bundle_digest'] = 'changed';
    expect(decoded.verificationIdentity!['bundle_digest'], 'c' * 64);
    expect(() => decoded.verificationIdentity!['generation'] = 9,
        throwsUnsupportedError);
    expect(
        () => AppManagedWork.decode(value), throwsA(isA<AppControlFailure>()));
    expect(() => AppManagedWork.decode({...work(), 'state': 'success'}),
        throwsA(isA<AppControlFailure>()));
  });
  for (final outcome in [
    'approval',
    'unknown',
    'invalid',
    'selection',
    'account'
  ]) {
    test('exact work scope and explicit $outcome recovery', () async {
      var scope = fixture.context();
      var epoch = 1;
      var executes = 0;
      final client = AppControlClient(
          context: () => scope,
          transportFactory: () => MockClient((request) async {
                expect(request.followRedirects, isFalse);
                if (request.url.path.endsWith('/enroll')) {
                  return http.Response(jsonEncode(fixture.enrollment()), 201);
                }
                if (request.url.path.endsWith('/selection')) {
                  return http.Response(
                      jsonEncode({
                        'ok': true,
                        'selection': {
                          'selection_id': 's1',
                          'binding_id': 'b1',
                          'binding_revision': 1,
                          'epoch': epoch
                        }
                      }),
                      200);
                }
                if (request.url.path.endsWith('/execute')) {
                  executes++;
                  expect(request.body, '{}');
                  expect(request.headers['Authorization'], 'Bearer deployment');
                  expect(request.headers['X-Sonder-Account-Token'], 'account');
                  if (outcome == 'approval' && executes == 1) {
                    return http.Response(
                        jsonEncode({
                          'ok': false,
                          'error': {'code': 'APP_WORK_APPROVAL_PENDING'},
                          'pending': {
                            'tool': 'workspace_run',
                            'surface': 'app-control',
                            'call_digest': 'b' * 64,
                            'call_id': 'b' * 16,
                            'expires_at': 4102444800
                          }
                        }),
                        409);
                  }
                  if (outcome == 'unknown') {
                    throw http.ClientException('private connection error');
                  }
                  if (outcome == 'invalid') {
                    return http.Response('{"ok":true,"work":{}}', 202);
                  }
                  return http.Response(
                      jsonEncode({
                        'ok': true,
                        'work': work(state: 'admitted', revision: 2)
                      }),
                      202);
                }
                if (request.method == 'GET') {
                  return http.Response(
                      jsonEncode({'ok': true, 'work': work(revision: 3)}), 200);
                }
                final body = jsonDecode(request.body) as Map;
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'work': work(),
                      'receipt': {
                        'command_id': body['command_id'],
                        'action': 'prepare_work',
                        'result_code': 'COMMITTED',
                        'entity_id': 'a' * 64,
                        'entity_revision': 1,
                        'selection_epoch': null
                      }
                    }),
                    200);
              }));
      addTearDown(client.dispose);
      await client.enroll(project: 'demo', password: 'disposable');
      await client.loadSelection();
      await client.prepareWork(prompt: 'Inspect only');
      expect(executes, 0);
      if (outcome == 'selection') {
        epoch = 2;
        await client.loadSelection();
      }
      if (outcome == 'account') {
        scope = fixture.context('different-account');
        client.synchronize();
      }
      await expectLater(
          client.executeWork(), throwsA(isA<AppControlFailure>()));
      if (outcome == 'selection' || outcome == 'account') {
        expect(executes, 0);
        if (outcome == 'account') {
          expect(client.work, isNull);
          expect(client.workPrompt, isEmpty);
        }
      } else if (outcome == 'approval') {
        expect(client.workApproval!.callId, 'b' * 16);
        expect(executes, 1);
        await client.executeWork();
        expect(executes, 2);
        expect(client.work!.state, 'admitted');
        await expectLater(
            client.refreshWork(), throwsA(isA<AppControlFailure>()));
        await expectLater(
            client.executeWork(), throwsA(isA<AppControlFailure>()));
        expect(executes, 2);
      } else {
        expect(client.workExecutionUnknown, isTrue);
        await client.refreshWork();
        await expectLater(
            client.executeWork(), throwsA(isA<AppControlFailure>()));
        expect(executes, 1);
      }
    });
  }
  test('prepare replay retains exact bytes and execution is explicit',
      () async {
    final calls = <http.Request>[];
    var failed = false;
    final client = AppControlClient(
        context: fixture.context,
        transportFactory: () => MockClient((request) async {
              if (request.url.path.endsWith('/enroll')) {
                return http.Response(jsonEncode(fixture.enrollment()), 201);
              }
              if (request.url.path.endsWith('/selection')) {
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'selection': {
                        'selection_id': 's1',
                        'binding_id': 'b1',
                        'binding_revision': 1,
                        'epoch': 1
                      }
                    }),
                    200);
              }
              calls.add(request);
              if (!failed) {
                failed = true;
                throw http.ClientException('lost response');
              }
              final body = jsonDecode(request.body) as Map;
              return http.Response(
                  jsonEncode({
                    'ok': true,
                    'work': work(),
                    'receipt': {
                      'command_id': body['command_id'],
                      'action': 'prepare_work',
                      'result_code': 'COMMITTED',
                      'entity_id': 'a' * 64,
                      'entity_revision': 1,
                      'selection_epoch': null
                    }
                  }),
                  200);
            }));
    addTearDown(client.dispose);
    await client.enroll(project: 'demo', password: 'disposable');
    await client.loadSelection();
    await expectLater(client.prepareWork(prompt: 'Inspect the repository'),
        throwsA(isA<AppControlFailure>()));
    expect(client.workPreparationPending, isTrue);
    await client.retryWorkPreparation();
    expect(calls.length, 2);
    expect(calls[0].body, calls[1].body);
    expect(calls[1].followRedirects, isFalse);
    expect(calls[1].headers['X-Sonder-App-Control'], fixture.controlToken);
    expect(client.work!.state, 'prepared');
    expect(client.workPrompt, 'Inspect the repository');
  });
}
