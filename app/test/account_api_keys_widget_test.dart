import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';

import 'account_settings_widget_test.dart' show MemoryCredentials;

void main() {
  testWidgets('a generated key is shown once with a copy action, and revoke '
      'names one key', (tester) async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    Settings.testingCredentialStore = MemoryCredentials();
    addTearDown(() => Settings.testingCredentialStore = null);
    tester.view.physicalSize = const Size(1200, 3000);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    String? copied;
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') {
        copied = (call.arguments as Map)['text'] as String?;
      }
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null));
    final revokeBodies = <Map<String, dynamic>>[];
    const keyId = '0123456789abcdef';
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
          home: SettingsScreen(
              settings:
                  Settings(serverUrl: 'https://host.test', apiKey: 'dep'),
              onChanged: (_) {})));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('category-${SettingsCategory.account}')));
      await tester.pumpAndSettle();
      await tester.enterText(
          find.byKey(const Key('settings-username')), 'alice');
      await tester.enterText(
          find.byKey(const Key('settings-password')), 'password123');
      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Generate New Key'));
      await tester.pumpAndSettle();
      expect(find.text('sk-secret-once'), findsOneWidget);
      await tester.tap(find.byKey(const Key('settings-new-api-key-copy')));
      await tester.pumpAndSettle();
      expect(copied, 'sk-secret-once');

      final revoke = find.byKey(const Key('settings-revoke-key-$keyId'));
      await tester.ensureVisible(revoke);
      await tester.tap(revoke);
      await tester.pumpAndSettle();
      expect(revokeBodies, [
        {'key_id': keyId}
      ]);
      expect(find.byKey(const Key('settings-revoke-key-$keyId')), findsNothing);
    },
        () => MockClient((r) async {
              if (r.url.path.endsWith('/login')) {
                return http.Response('{"ok":true,"token":"account"}', 200);
              }
              if (r.url.path.endsWith('/generate')) {
                return http.Response(
                    jsonEncode({
                      'ok': true,
                      'key': 'sk-secret-once',
                      'key_id': keyId,
                      'created_ts': 1
                    }),
                    200);
              }
              if (r.url.path.endsWith('/revoke')) {
                revokeBodies.add(jsonDecode(r.body) as Map<String, dynamic>);
                return http.Response('{"ok":true,"revoked":1}', 200);
              }
              return http.Response('{"ok":true}', 200);
            }));
  });
}
