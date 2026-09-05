import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';

class MemoryCredentials implements CredentialStore {
  final values = <String, String>{};
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

void main() {
  testWidgets(
      'login keeps deployment key; failed signout retries exact session',
      (tester) async {
    SharedPreferences.setMockInitialValues({});
    Settings.testingCredentialStore = MemoryCredentials();
    addTearDown(() {
      Settings.testingCredentialStore = null;
    });
    tester.view.physicalSize = const Size(1200, 2200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    Settings? saved;
    var revokes = 0;
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
          home: SettingsScreen(
              settings: Settings(
                  serverUrl: 'https://host.test', apiKey: 'deployment'),
              onChanged: (s) {
                saved = s;
              })));
      await tester.enterText(
          find.widgetWithText(TextField, 'Username'), 'alice');
      await tester.enterText(
          find.widgetWithText(TextField, 'Password'), 'password123');
      await tester.ensureVisible(find.text('Login'));
      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();
      expect(find.text('deployment'), findsOneWidget);
      expect(find.textContaining('Account session bound to https://host.test'),
          findsOneWidget);
      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();
      expect(find.textContaining('before switching accounts.'), findsOneWidget);
      await tester.enterText(
          find.widgetWithText(TextField, 'Server URL'), 'https://other.test');
      await tester.ensureVisible(find.text('Save'));
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(saved, isNull);
      expect(find.textContaining('before switching servers.'), findsOneWidget);
      await tester.enterText(
          find.widgetWithText(TextField, 'Server URL'), 'https://host.test');
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Sign out'));
      await tester.tap(find.text('Sign out'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Revocation not confirmed.'), findsOneWidget);
      await tester.tap(find.text('Sign out'));
      await tester.pumpAndSettle();
      expect(saved!.apiKey, 'deployment');
      expect(saved!.accountSession, isNull);
      expect(revokes, 2);
    },
        () => MockClient((r) async {
              expect(r.followRedirects, isFalse);
              expect(r.headers['Authorization'], 'Bearer deployment');
              if (r.url.path.endsWith('/login')) {
                expect(
                    r.headers.containsKey('X-Sonder-Account-Token'), isFalse);
                return http.Response('{"ok":true,"token":"account"}', 200);
              }
              expect(r.headers['X-Sonder-Account-Token'], 'account');
              expect(r.body, '{}');
              revokes++;
              return http.Response(revokes == 1 ? '{}' : '{"ok":true}',
                  revokes == 1 ? 503 : 200);
            }));
  });
}
