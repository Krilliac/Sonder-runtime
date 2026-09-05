import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/account_session.dart';

void main() {
  test('automatic local fallback never receives account credential', () async {
    var calls = 0;
    await http.runWithClient(
        () => SonderApi(
                baseUrl: 'https://host.test',
                apiKey: 'deployment',
                accountSession: AccountSession(
                    token: 'account', origin: 'https://host.test'))
            .chatDetailed([]),
        () => MockClient((r) async {
              calls++;
              if (calls == 1) {
                expect(r.headers['X-Sonder-Account-Token'], 'account');
                throw http.ClientException('unreachable');
              }
              expect(r.url.host, '127.0.0.1');
              expect(r.headers.containsKey('X-Sonder-Account-Token'), isFalse);
              expect(r.headers.containsKey('Authorization'), isFalse);
              return http.Response(
                  '{"choices":[{"message":{"content":"ok"}}]}', 200);
            }));
    expect(calls, 2);
  });

  test(
      'unknown revocation retains exact retry and foreign origin cannot revoke',
      () async {
    var calls = 0;
    final account =
        AccountSession(token: 'account', origin: 'https://host.test');
    final api = SonderApi(
        baseUrl: account.origin, apiKey: 'deployment', accountSession: account);
    await http.runWithClient(() async {
      await expectLater(api.logout(), throwsA(isA<SonderException>()));
      await api.logout();
      await expectLater(
          SonderApi(baseUrl: 'https://foreign.test', accountSession: account)
              .logout(),
          throwsA(isA<SonderException>()));
    },
        () => MockClient((r) async {
              expect(r.headers['X-Sonder-Account-Token'], 'account');
              calls++;
              return http.Response(
                  calls == 1 ? '{}' : '{"ok":true}', calls == 1 ? 503 : 200);
            }));
    expect(calls, 2);
  });
  test('foreign origin omits account header', () async {
    await http.runWithClient(
        () => SonderApi(
                baseUrl: 'https://foreign.test',
                accountSession: AccountSession(
                    token: 'account', origin: 'https://host.test'))
            .listModels(),
        () => MockClient((r) async {
              expect(r.headers.containsKey('X-Sonder-Account-Token'), isFalse);
              return http.Response('{"data":[]}', 200);
            }));
  });

  test('logout separates credentials and refuses redirects', () async {
    await http.runWithClient(
        () => SonderApi(
                baseUrl: 'https://host.test',
                apiKey: 'deployment',
                accountSession: AccountSession(
                    token: 'account', origin: 'https://host.test'))
            .logout(),
        () => MockClient((r) async {
              expect(r.headers['Authorization'], 'Bearer deployment');
              expect(r.headers['X-Sonder-Account-Token'], 'account');
              expect(r.followRedirects, isFalse);
              expect(r.body, '{}');
              return http.Response('{"ok":true}', 200);
            }));
  });
}
