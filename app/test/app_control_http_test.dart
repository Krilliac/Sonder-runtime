import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/app_control.dart';
import 'package:sonder_runtime/api.dart';
import 'app_control_test.dart' as fixture;
import 'app_work_test.dart' as work_data;

void main() {
  test(
      'real work HTTP preserves exact headers body and forbids redirect execution',
      () async {
    final target = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    var leaked = 0, executions = 0;
    target.listen((request) {
      leaked++;
      request.response.close();
    });
    final origin = 'http://127.0.0.1:${server.port}';
    server.listen((request) async {
      expect(request.headers.value('authorization'), 'Bearer deployment');
      expect(request.headers.value('x-sonder-account-token'), 'account');
      final body = await utf8.decoder.bind(request).join();
      if (request.uri.path.endsWith('/enroll')) {
        request.response.statusCode = 201;
        request.response.write(jsonEncode(fixture.enrollment()));
      } else {
        expect(request.headers.value('x-sonder-app-control'),
            fixture.controlToken);
        if (request.uri.path.endsWith('/selection')) {
          request.response.write(
              '{"ok":true,"selection":{"selection_id":"s1","binding_id":"b1","binding_revision":1,"epoch":1}}');
        } else if (request.uri.path.endsWith('/execute')) {
          executions++;
          expect(body, '{}');
          request.response.statusCode = 307;
          request.response.headers
              .set('location', 'http://127.0.0.1:${target.port}/leak');
          request.response.write('{}');
        } else {
          expect(request.uri.path, '/v1/app-control/work');
          final value = jsonDecode(body) as Map;
          expect(value.keys.toSet(), {
            'command_id',
            'prompt',
            'tier',
            'max_steps',
            'allow_web',
            'allow_location'
          });
          request.response.write(jsonEncode({
            'ok': true,
              'work': work_data.work(),
            'receipt': {
              'command_id': value['command_id'],
              'action': 'prepare_work',
              'result_code': 'COMMITTED',
              'entity_id': 'a' * 64,
              'entity_revision': 1,
              'selection_epoch': null
            }
          }));
        }
      }
      await request.response.close();
    });
    final client = AppControlClient(
        context: () => AppControlContext(
            serverUrl: origin,
            deploymentKey: 'deployment',
            account: AccountSession(token: 'account', origin: origin)));
    try {
      await client.enroll(project: 'demo', password: 'disposable');
      await client.loadSelection();
      await client.prepareWork(prompt: 'Inspect repository');
      await expectLater(
          client.executeWork(), throwsA(isA<AppControlFailure>()));
      await expectLater(
          client.executeWork(), throwsA(isA<AppControlFailure>()));
      expect(client.workExecutionUnknown, isTrue);
      expect(executions, 1);
      expect(leaked, 0);
    } finally {
      client.dispose();
      await server.close(force: true);
      await target.close(force: true);
    }
  });
  test(
      'real HTTP dedicated bearer never reaches redirect target or generic API',
      () async {
    final target = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final origin = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    var leaked = 0, generic = 0;
    target.listen((request) {
      leaked++;
      request.response.close();
    });
    final url = 'http://127.0.0.1:${origin.port}';
    final account = AccountSession(token: 'disposable-account', origin: url);
    origin.listen((request) async {
      expect(request.headers.value('x-sonder-account-token'),
          'disposable-account');
      expect(request.headers.value('authorization'),
          'Bearer disposable-deployment');
      if (request.uri.path.endsWith('/enroll')) {
        expect(request.headers.value('x-sonder-app-control'), isNull);
        await request.drain<void>();
        request.response.statusCode = 201;
        request.response.write(jsonEncode(fixture.enrollment()));
      } else if (request.uri.path == '/v1/models') {
        generic++;
        expect(request.headers.value('x-sonder-app-control'), isNull);
        request.response.write('{"data":[]}');
      } else {
        expect(request.headers.value('x-sonder-app-control'),
            fixture.controlToken);
        request.response.statusCode = 302;
        request.response.headers
            .set('location', 'http://127.0.0.1:${target.port}/receive');
        request.response.write('{}');
      }
      await request.response.close();
    });
    final client = AppControlClient(
        context: () => AppControlContext(
            serverUrl: url,
            deploymentKey: 'disposable-deployment',
            account: account));
    try {
      await client.enroll(project: 'demo', password: 'disposable-password');
      await expectLater(
          client.loadBindings(), throwsA(isA<AppControlFailure>()));
      await SonderApi(
              baseUrl: url,
              apiKey: 'disposable-deployment',
              accountSession: account)
          .listModels();
      expect(leaked, 0);
      expect(generic, 1);
    } finally {
      client.dispose();
      await origin.close(force: true);
      await target.close(force: true);
    }
  });
}
