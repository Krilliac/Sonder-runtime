import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/account_session.dart';

void main() {
  test('real HTTP logout does not follow redirect or leak credentials',
      () async {
    final target = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final origin = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    var leaked = 0;
    target.listen((r) {
      leaked++;
      r.response.close();
    });
    var requests = 0;
    final url = 'http://127.0.0.1:${origin.port}';
    origin.listen((r) async {
      expect(r.headers.value('authorization'), 'Bearer deployment');
      expect(r.headers.value('x-sonder-account-token'), 'account');
      expect(await r.fold<List<int>>([], (a, b) => a..addAll(b)), [123, 125]);
      requests++;
      r.response.statusCode = requests == 1 ? 302 : 200;
      if (requests == 1) {
        r.response.headers
            .set('location', 'http://127.0.0.1:${target.port}/stolen');
      } else {
        r.response.write('{"ok":true}');
      }
      await r.response.close();
    });
    try {
      final api = SonderApi(
          baseUrl: url,
          apiKey: 'deployment',
          accountSession: AccountSession(token: 'account', origin: url));
      await expectLater(api.logout(), throwsA(isA<SonderException>()));
      await api.logout();
      expect(requests, 2);
      expect(leaked, 0);
    } finally {
      await origin.close(force: true);
      await target.close(force: true);
    }
  });
}
