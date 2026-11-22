import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';

void main() {
  test('account API methods post to their app-control routes', () async {
    final seen = <String>[];
    await http.runWithClient(() async {
      final api = SonderApi(baseUrl: 'http://127.0.0.1:1');
      expect(await api.listApiKeys(), {'keys': []});
      await api.generateApiKey();
      await api.revokeApiKey('0123456789abcdef');
      await api.getSwarmStatus();
    },
        () => MockClient((request) async {
              expect(request.method, 'POST');
              seen.add(request.url.path);
              return http.Response(jsonEncode({'keys': []}), 200,
                  headers: {'content-type': 'application/json'});
            }));
    expect(seen, [
      '/v1/app-control/api-keys/list',
      '/v1/app-control/api-keys/generate',
      '/v1/app-control/api-keys/revoke',
      '/v1/app-control/swarm/status',
    ]);
  });

  test('a refused account call throws instead of returning the error body',
      () async {
    await expectLater(
      http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:1').getSwarmStatus(),
        () => MockClient((_) async => http.Response(
            jsonEncode({'error': {'message': 'admin only'}}), 403,
            headers: {'content-type': 'application/json'})),
      ),
      throwsA(isA<SonderException>()),
    );
  });

  test('revoke names the key it revokes and never revokes all', () async {
    final bodies = <Map<String, dynamic>>[];
    await http.runWithClient(
      () => SonderApi(baseUrl: 'http://127.0.0.1:1')
          .revokeApiKey('0123456789abcdef'),
      () => MockClient((request) async {
        bodies.add(jsonDecode(request.body) as Map<String, dynamic>);
        return http.Response(jsonEncode({'ok': true, 'revoked': 1}), 200,
            headers: {'content-type': 'application/json'});
      }),
    );
    expect(bodies, [
      {'key_id': '0123456789abcdef'}
    ]);
    await expectLater(
      http.runWithClient(
        () => SonderApi(baseUrl: 'http://127.0.0.1:1').revokeApiKey('  '),
        () => MockClient((_) async => fail('must not reach the network')),
      ),
      throwsA(isA<SonderException>()),
    );
  });

  test('a 200 that is not a JSON object, or says ok:false, is an error',
      () async {
    for (final body in ['not json', '[1]', jsonEncode({'ok': false})]) {
      await expectLater(
        http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:1').listApiKeys(),
          () => MockClient((_) async => http.Response(body, 200,
              headers: {'content-type': 'application/json'})),
        ),
        throwsA(isA<SonderException>()),
        reason: body,
      );
    }
  });
}
