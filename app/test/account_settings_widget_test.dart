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

Future<void> _open(WidgetTester tester, String id) async {
  await tester.tap(find.byKey(Key('category-$id')));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
      'login keeps deployment key; failed signout retries exact session',
      (tester) async {
    // As the app loads it: the saved server is the one being signed in to.
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final credentials = MemoryCredentials();
    Settings.testingCredentialStore = credentials;
    addTearDown(() {
      Settings.testingCredentialStore = null;
    });
    tester.view.physicalSize = const Size(1200, 2200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final changes = <Settings>[];
    var revokes = 0;
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
          home: SettingsScreen(
              settings: Settings(
                  serverUrl: 'https://host.test', apiKey: 'deployment'),
              onChanged: changes.add)));
      await tester.pumpAndSettle();
      await _open(tester, SettingsCategory.account);
      await tester.enterText(
          find.byKey(const Key('settings-username')), 'alice');
      await tester.enterText(
          find.byKey(const Key('settings-password')), 'password123');
      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();

      // Stored at once, origin-bound, in the secure store only: no second
      // Save, and the deployment key is untouched.
      expect(changes, hasLength(1));
      expect(changes.last.accountSession!.token, 'account');
      expect(changes.last.accountSession!.origin, 'https://host.test');
      expect(changes.last.apiKey, 'deployment');
      expect(credentials.values['sonder_account_session'],
          contains('"origin":"https://host.test"'));
      final preferences = await SharedPreferences.getInstance();
      for (final key in preferences.getKeys()) {
        expect('${preferences.get(key)}', isNot(contains('"token"')));
      }
      expect(find.byKey(const Key('settings-save')), findsNothing);
      expect(
          find.textContaining('Sign out revokes this session'), findsOneWidget);
      expect(find.text('Signed-in server'), findsOneWidget);
      expect(find.text('https://host.test'), findsOneWidget);
      // Switching accounts needs Sign out or Forget first: no login form.
      expect(find.text('Login'), findsNothing);

      await _open(tester, SettingsCategory.connection);
      expect(find.text('deployment'), findsOneWidget);
      await tester.enterText(
          find.byKey(const Key('settings-server-url')), 'https://other.test');
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('settings-save')));
      await tester.pumpAndSettle();
      expect(changes, hasLength(1));
      expect(find.textContaining('before switching servers.'), findsOneWidget);
      await tester.enterText(
          find.byKey(const Key('settings-server-url')), 'https://host.test');
      await tester.pumpAndSettle();

      await _open(tester, SettingsCategory.account);
      await tester.tap(find.text('Sign out'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Revocation not confirmed.'), findsOneWidget);
      expect(credentials.values['sonder_account_session'], isNotNull);
      await tester.tap(find.text('Sign out'));
      await tester.pumpAndSettle();
      expect(changes.last.apiKey, 'deployment');
      expect(changes.last.accountSession, isNull);
      expect(credentials.values.containsKey('sonder_account_session'), isFalse);
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

  testWidgets('a sign-in to an unsaved server is stored only by Save',
      (tester) async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final credentials = MemoryCredentials();
    Settings.testingCredentialStore = credentials;
    addTearDown(() => Settings.testingCredentialStore = null);
    tester.view.physicalSize = const Size(1200, 2200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final changes = <Settings>[];
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(
          home: SettingsScreen(
              settings: Settings(serverUrl: 'https://host.test'),
              onChanged: changes.add)));
      await tester.pumpAndSettle();
      await _open(tester, SettingsCategory.connection);
      await tester.enterText(
          find.byKey(const Key('settings-server-url')), 'https://new.test');
      await _open(tester, SettingsCategory.account);
      await tester.enterText(
          find.byKey(const Key('settings-username')), 'alice');
      await tester.enterText(
          find.byKey(const Key('settings-password')), 'password123');
      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();
      // Staged with the server URL it belongs to: nothing stored yet.
      expect(changes, isEmpty);
      expect(credentials.values, isEmpty);
      expect(find.textContaining('Save to keep this session'), findsOneWidget);

      await tester.tap(find.byKey(const Key('settings-save')));
      await tester.pumpAndSettle();
      expect(changes.single.serverUrl, 'https://new.test');
      expect(changes.single.accountSession!.origin, 'https://new.test');
      expect(credentials.values['sonder_account_session'],
          contains('"origin":"https://new.test"'));
    },
        () => MockClient((r) async {
              expect(r.url.origin, 'https://new.test');
              return http.Response('{"ok":true,"token":"account"}', 200);
            }));
  });
}
