import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/app_control.dart';
import 'package:sonder_runtime/api.dart';
import 'app_control_test.dart' as fixture;

void main() {
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
