import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/account_session.dart';
void main() {
 test('account token rejects oversized whitespace and controls without truncation', () {
  for(final token in ['', 'x' * 513, 'a b', 'a\t', 'a\u0000', 'é']) {
   expect(()=>AccountSession(token:token,origin:'https://host.test'),throwsArgumentError);
  }
  expect(AccountSession(token:'x' * 512,origin:'https://host.test').token.length,512);
 });

 test('remote HTTP account calls fail before any request', () async {
  var requests=0;
  await http.runWithClient(() async {
   final api=SonderApi(baseUrl:'http://remote.example',apiKey:'deployment');
   await expectLater(api.login('alice','fake-password'),throwsA(anything));
   await expectLater(api.register('alice','fake-password'),throwsA(anything));
  }, () => MockClient((r) async {requests++;return http.Response('{"ok":true,"token":"fake"}',200);}));
  expect(requests,0);
 });
 test('only numeric canonical loopback permits HTTP account origins', () {
  for(final url in ['http://remote.example','http://localhost','http://127.example','http://127.00.0.1','http://127.0.0.999']) {
   expect(()=>AccountSession(token:'fake',origin:url),throwsArgumentError);
  }
  for(final url in ['http://127.0.0.1:1234','http://127.1.2.3','http://[::1]:1234','https://remote.example']) {
   expect(AccountSession(token:'fake',origin:url).matches(url),isTrue);
  }
 });
}
