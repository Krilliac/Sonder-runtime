// P0-9 (API part): register accepts 201, explains the bootstrap refusal, and
// sends the bootstrap secret only when given, only as a header.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';

import 'fixtures/server_fixtures.dart';

void main() {
  test('201 with ok:true is success with the created role', () async {
    final text = await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .register('parityuser', 'pw'),
        () => MockClient(
            (r) async => fixtureResponse('register_created_201.json', 201)));
    expect(text, 'Account parityuser created (role admin).');
  });

  test('403 first-admin bootstrap -> needsBootstrapSecret', () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .register('parityuser', 'pw'),
          () => MockClient((r) async {
                expect(r.headers.containsKey('X-Sonder-Bootstrap-Secret'),
                    isFalse);
                return fixtureResponse('register_bootstrap_403.json', 403);
              })),
      throwsA(isA<SonderException>()
          .having((e) => e.needsBootstrapSecret, 'needs secret', isTrue)
          .having((e) => e.status, 'status', 403)
          .having((e) => e.message, 'message', contains('bootstrap secret'))),
    );
  });

  test('the secret travels only as X-Sonder-Bootstrap-Secret', () async {
    late http.Request seen;
    await http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
            .register('parityuser', 'pw', bootstrapSecret: ' s3cret '),
        () => MockClient((r) async {
              seen = r;
              return fixtureResponse('register_created_201.json', 201);
            }));
    expect(seen.headers['X-Sonder-Bootstrap-Secret'], 's3cret');
    expect(seen.body, isNot(contains('s3cret')));
    expect(jsonDecode(seen.body), {'username': 'parityuser', 'password': 'pw'});
  });

  test('409 account exists keeps the server message', () async {
    await expectLater(
      http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435')
              .register('parityuser', 'pw'),
          () => MockClient((r) async => http.Response(
              '{"ok": false, "message": "account already exists"}', 409))),
      throwsA(isA<SonderException>()
          .having((e) => e.message, 'message', 'account already exists')),
    );
  });

  test('login 401 and 429 are readable', () async {
    var n = 0;
    await http.runWithClient(() async {
      final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
      await expectLater(
          api.login('bob', 'bad'),
          throwsA(isA<SonderException>()
              .having((e) => e.message, 'message', 'Login was not accepted.')));
      await expectLater(
          api.login('bob', 'bad'),
          throwsA(isA<SonderException>()
              .having((e) => e.code, 'code', 'AUTH_RATE_LIMITED')
              .having((e) => e.message, 'message',
                  'Too many failed sign-ins from this network. Try again in 2 s.')));
    }, () {
      return MockClient((r) async => ++n == 1
          ? http.Response(
              '{"ok": false, "message": "invalid username or password"}', 401)
          : fixtureResponse('auth_rate_limited_429.json', 429,
              headers: {...jsonHeaders, 'retry-after': '2'}));
    });
  });
}
