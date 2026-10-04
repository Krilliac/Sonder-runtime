// The deployment API key never crosses the network in cleartext unless the
// person explicitly allowed that host:port in Settings.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart' show SettingsCategory;

import 'settings_connect_test.dart'
    show FakeConnection, pumpSettings, saveSettings, settingsField;

Future<String?> _authSentBy(Future<void> Function() call) async {
  String? auth = 'no request';
  final client = MockClient((request) async {
    auth = request.headers['Authorization'];
    return http.Response('{"data": []}', 200,
        headers: {'content-type': 'application/json'});
  });
  await http.runWithClient(() async {
    try {
      await call();
    } catch (_) {}
  }, () => client);
  return auth;
}

void main() {
  setUp(() => CleartextKeyPolicy.allowOnly(const []));
  tearDown(() => CleartextKeyPolicy.allowOnly(const []));

  group('policy', () {
    test('https and loopback always carry the key', () {
      expect(CleartextKeyPolicy.allows('https://pc.example:8443'), isTrue);
      expect(CleartextKeyPolicy.allows('http://127.0.0.1:11435'), isTrue);
      expect(CleartextKeyPolicy.allows('http://localhost:11435'), isTrue);
      expect(CleartextKeyPolicy.allows('http://[::1]:11435'), isTrue);
    });

    test('plain http off this device needs the exact host:port allowed', () {
      expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isFalse);
      CleartextKeyPolicy.allowOnly(['192.168.1.20:11435']);
      expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isTrue);
      expect(CleartextKeyPolicy.allows('http://192.168.1.20:8080'), isFalse);
      expect(CleartextKeyPolicy.allows('http://192.168.1.21:11435'), isFalse);
      expect(
          CleartextKeyPolicy.allows('http://127.evil.example:11435'), isFalse);
    });
  });

  test('SonderApi withholds the key from an unconfirmed LAN http host',
      () async {
    final api =
        SonderApi(baseUrl: 'http://192.168.1.20:11435', apiKey: 'secret-key');
    expect(await _authSentBy(api.listModels), isNull);

    CleartextKeyPolicy.allowOnly(['192.168.1.20:11435']);
    expect(await _authSentBy(api.listModels), 'Bearer secret-key');

    final https =
        SonderApi(baseUrl: 'https://pc.example:8443', apiKey: 'secret-key');
    CleartextKeyPolicy.allowOnly(const []);
    expect(await _authSentBy(https.listModels), 'Bearer secret-key');
  });

  test('runtime panel reads follow the same rule', () async {
    const source = HttpRuntimeDataSource(
        baseUrl: 'http://192.168.1.20:11435', apiKey: 'secret-key');
    expect(await _authSentBy(source.jobs), isNull);
    CleartextKeyPolicy.allowOnly(['192.168.1.20:11435']);
    expect(await _authSentBy(source.jobs), 'Bearer secret-key');
  });

  testWidgets('Settings asks per host and persists only an explicit yes',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    await pumpSettings(tester,
        connection: FakeConnection(),
        serverUrl: 'http://192.168.1.20:11435',
        category: SettingsCategory.connection);
    final allow = find.byKey(const Key('settings-cleartext-key-allow'));
    // No key typed: nothing to warn about.
    expect(allow, findsNothing);

    await tester.enterText(settingsField('api-key'), 'secret-key');
    // The warning and the choice slide open under the field.
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-cleartext-key-warning')),
        findsOneWidget);
    expect(find.textContaining('is not sent to 192.168.1.20:11435'),
        findsOneWidget);
    expect(tester.widget<CheckboxListTile>(allow).value, isFalse);

    await tester.ensureVisible(allow);
    await tester.tap(allow);
    await tester.pump();
    expect(tester.widget<CheckboxListTile>(allow).value, isTrue);
    // Not applied until saved.
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isFalse);

    await saveSettings(tester);
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getStringList('sonder_cleartext_key_hosts'),
        ['192.168.1.20:11435']);
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isTrue);

    CleartextKeyPolicy.allowOnly(const []);
    final loaded = await Settings.load();
    expect(loaded.cleartextKeyHosts, ['192.168.1.20:11435']);
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isTrue);
  });
}
