import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/runtime/runtime_screen.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';

import 'runtime_fixtures.dart';

class _MemoryCredentials implements CredentialStore {
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

Future<void> _openSettingsCategory(WidgetTester tester, String id) async {
  await tester.tap(find.byKey(Key('category-$id')));
  await tester.pumpAndSettle();
}

void main() {
  group('capabilities and whoami DTOs', () {
    test('parses public_hosting capabilities and restricted user whoami', () {
      final caps = ServerCapabilities.fromJson({
        'public_hosting': true,
        'auth_mode': 'account',
        'registration_enabled': true,
        'client_exec_supported': true,
        'client_owns_tool_policy': true,
        'allowed_features': {
          'chat': true,
          'registration': true,
          'client_exec': true,
          'host_tools': false,
          'host_bypass': false,
          'host_admin': false,
        },
      });
      expect(caps.publicHosting, isTrue);
      expect(caps.authMode, 'account');
      expect(caps.registrationEnabled, isTrue);
      expect(caps.clientExecSupported, isTrue);
      expect(caps.clientOwnsToolPolicy, isTrue);
      expect(caps.allowedFeatures.hostAdmin, isFalse);
      expect(caps.allowedFeatures.hostBypass, isFalse);
      expect(caps.allowedFeatures.clientExec, isTrue);

      final who = WhoamiInfo.fromJson({
        'ok': true,
        'username': 'alice',
        'role': 'user',
        'public_hosting': true,
        'auth_mode': 'account',
        'client_exec_supported': true,
        'host_bypass_allowed': false,
        'allowed_features': {
          'chat': true,
          'registration': true,
          'client_exec': true,
          'host_tools': false,
          'host_bypass': false,
          'host_admin': false,
        },
      });
      expect(who.ok, isTrue);
      expect(who.username, 'alice');
      expect(who.role, 'user');
      expect(who.isRestrictedUser, isTrue);
      expect(who.hostBypassAllowed, isFalse);
      expect(who.allowedFeatures.clientExec, isTrue);
    });

    test('SystemInfo exposes role, publicHosting, and allowedFeatures', () {
      final info = SystemInfo.fromJson({
        'status': 'ready',
        'role': 'user',
        'public_hosting': true,
        'client_exec_supported': true,
        'host_bypass_allowed': false,
        'allowed_features': {
          'chat': true,
          'client_exec': true,
          'host_tools': false,
          'host_bypass': false,
          'host_admin': false,
        },
      });
      expect(info.role, 'user');
      expect(info.publicHosting, isTrue);
      expect(info.clientExecSupported, isTrue);
      expect(info.hostBypassAllowed, isFalse);
      expect(info.allowedFeatures?.hostAdmin, isFalse);
      expect(info.allowedFeatures?.clientExec, isTrue);
    });

    test('SonderApi.fetchCapabilities and fetchWhoami hit expected endpoints',
        () async {
      final paths = <String>[];
      await http.runWithClient(() async {
        final api = SonderApi(baseUrl: 'https://public.example');
        final caps = await api.fetchCapabilities();
        final who = await api.fetchWhoami();
        expect(caps?.publicHosting, isTrue);
        expect(caps?.authMode, 'account');
        expect(who?.username, 'bob');
        expect(who?.role, 'user');
      }, () {
        return MockClient((r) async {
          paths.add(r.url.path);
          if (r.url.path == '/v1/sonder/capabilities') {
            return http.Response(
              jsonEncode({
                'public_hosting': true,
                'auth_mode': 'account',
                'registration_enabled': true,
                'client_exec_supported': true,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/sonder/whoami') {
            return http.Response(
              jsonEncode({
                'ok': true,
                'username': 'bob',
                'role': 'user',
                'public_hosting': true,
                'client_exec_supported': true,
                'host_bypass_allowed': false,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{}', 404);
        });
      });
      expect(paths, ['/v1/sonder/capabilities', '/v1/sonder/whoami']);
    });

    test('validatePublicHostingPassword checks length, classes, and username',
        () {
      expect(validatePublicHostingPassword('short1A!'), isNotNull);
      expect(validatePublicHostingPassword('alllowercaseonly'), isNotNull);
      expect(validatePublicHostingPassword('onlylowercase1234'), isNotNull);
      expect(
        validatePublicHostingPassword('AlicePass123!', username: 'alice'),
        isNotNull,
      );
      expect(
        validatePublicHostingPassword('CorrectHorse99!', username: 'alice'),
        isNull,
      );
    });
  });

  group('SettingsScreen public hosting auth & role UI', () {
    testWidgets(
        'public hosting Account registers without API key or bootstrap secret and hides host-admin UI for user role',
        (tester) async {
      SharedPreferences.setMockInitialValues(
          {'sonder_server_url': 'https://public.example'});
      final credentials = _MemoryCredentials();
      Settings.testingCredentialStore = credentials;
      addTearDown(() => Settings.testingCredentialStore = null);

      tester.view.physicalSize = const Size(1280, 1400);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      var signedInUser = '';
      Map<String, dynamic>? registerPayload;
      var registerHeaders = <String, String>{};

      await http.runWithClient(() async {
        await tester.pumpWidget(MaterialApp(
          theme: SonderTheme.dark,
          home: SettingsScreen(
            settings: Settings(serverUrl: 'https://public.example'),
            onChanged: (_) {},
          ),
        ));
        await tester.pumpAndSettle();

        // Host-only settings categories stay hidden for restricted user role.
        expect(find.byKey(const Key('category-${SettingsCategory.desktop}')),
            findsNothing);
        expect(
            find.byKey(const Key('category-${SettingsCategory.observatory}')),
            findsNothing);

        // Connection page hides Host launcher when host_admin == false.
        await _openSettingsCategory(tester, SettingsCategory.connection);
        expect(find.text('Host launcher'), findsNothing);
        expect(find.byKey(const Key('settings-launcher-url')), findsNothing);

        // Account page enforces public hosting password hint, hides bootstrap
        // secret, and registers without requiring a shared SONDER_API_KEY.
        await _openSettingsCategory(tester, SettingsCategory.account);
        expect(find.byKey(const Key('settings-bootstrap-secret')), findsNothing);

        await tester.enterText(
            find.byKey(const Key('settings-username')), 'alice');
        await tester.enterText(
            find.byKey(const Key('settings-password')), 'weakpw');
        await tester.pump();
        expect(
          find.byKey(const Key('settings-public-password-hint')),
          findsOneWidget,
        );

        await tester.enterText(
            find.byKey(const Key('settings-password')), 'StrongPass99!');
        await tester.pump();
        expect(
          find.byKey(const Key('settings-public-password-hint')),
          findsNothing,
        );

        final regBtn = find.byKey(const Key('settings-register'));
        await tester.ensureVisible(regBtn);
        await tester.tap(regBtn);
        await tester.pumpAndSettle();

        expect(registerPayload,
            {'username': 'alice', 'password': 'StrongPass99!'});
        expect(registerHeaders.containsKey('X-Sonder-Bootstrap-Secret'),
            isFalse);
        expect(registerHeaders.containsKey('Authorization'), isFalse);
        expect(find.textContaining('Account alice created'), findsOneWidget);

        // Login with the newly registered account without a host SONDER_API_KEY.
        final loginBtn = find.byKey(const Key('settings-login'));
        await tester.ensureVisible(loginBtn);
        await tester.tap(loginBtn);
        await tester.pumpAndSettle();

        expect(
          find.byKey(const Key('settings-account-role')),
          findsOneWidget,
        );
        expect(find.textContaining('user'), findsWidgets);
        expect(
          find.byKey(const Key('settings-account-client-exec')),
          findsOneWidget,
        );
        expect(signedInUser, 'alice');
      }, () {
        return MockClient((r) async {
          if (r.url.path == '/v1/sonder/capabilities') {
            return http.Response(
              jsonEncode({
                'public_hosting': true,
                'auth_mode': 'account',
                'registration_enabled': true,
                'client_exec_supported': true,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/sonder/whoami') {
            return http.Response(
              jsonEncode({
                'ok': signedInUser.isNotEmpty,
                'username': signedInUser,
                'role': 'user',
                'public_hosting': true,
                'auth_mode': 'account',
                'client_exec_supported': true,
                'host_bypass_allowed': false,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              signedInUser.isEmpty ? 401 : 200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/sonder/login') {
            signedInUser = 'alice';
            return http.Response(
              jsonEncode({
                'ok': true,
                'username': 'alice',
                'role': 'user',
                'token': 'tok-alice-1',
                'public_hosting': true,
                'client_exec_supported': true,
                'host_bypass_allowed': false,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/sonder/register') {
            registerHeaders = Map<String, String>.from(r.headers);
            registerPayload =
                Map<String, dynamic>.from(jsonDecode(r.body) as Map);
            signedInUser = 'alice';
            return http.Response(
              jsonEncode({
                'ok': true,
                'username': 'alice',
                'role': 'user',
                'token': 'tok-alice-1',
                'public_hosting': true,
                'client_exec_supported': true,
                'host_bypass_allowed': false,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              201,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/sonder/status') {
            return http.Response(
              jsonEncode({
                'status': 'ready',
                'role': 'user',
                'public_hosting': true,
                'client_exec_supported': true,
                'host_bypass_allowed': false,
                'allowed_features': {
                  'host_admin': false,
                  'host_bypass': false,
                  'client_exec': true,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/models') {
            return http.Response(
              jsonEncode({
                'data': [
                  {'id': 'sonder:latest', 'owned_by': 'local'}
                ]
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{}', 404);
        });
      });
    });
  });

  group('RuntimeScreen role gating', () {
    testWidgets(
        'hides host-admin categories and panels for user accounts while showing client PC execution',
        (tester) async {
      final userInfo = SystemInfo.fromJson({
        'status': 'ready · v2026.11.17 · up 1h 5m',
        'stats': '0 errors',
        'learn_tiers': 'local tiers: code',
        'improvements': '',
        'role': 'user',
        'public_hosting': true,
        'client_exec_supported': true,
        'host_bypass_allowed': false,
        'allowed_features': {
          'host_admin': false,
          'host_bypass': false,
          'client_exec': true,
        },
        'models': [
          {'id': 'sonder:latest', 'owned_by': 'local'},
        ],
        'agents': {'active_agents': 0, 'total_agents': 0, 'agents': const []},
        'autopilot': {
          'active_runs': 0,
          'resumable_runs': 0,
          'total_runs': 0,
          'runs': const [],
        },
        'execution': {'known': false},
      });

      tester.view.physicalSize = const Size(1280, 1000);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });

      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: RuntimeScreen(
          settings: Settings(
            serverUrl: 'https://public.example',
            clientExecEnabled: true,
            clientPermissionMode: 'auto',
          ),
          initialInfo: userInfo,
          liveUpdates: false,
          dataSource: FakeRuntimeData(),
          now: runtimeNow,
          initialCategory: 'permissions',
        ),
      ));
      await tester.pumpAndSettle();

      // Host-admin categories hidden.
      for (final hidden in [
        'server',
        'observatory',
        'updates',
        'cluster',
        'developer',
        'memory',
      ]) {
        expect(find.byKey(Key('category-$hidden')), findsNothing,
            reason: '$hidden must be hidden when host_admin=false');
      }

      // User categories visible.
      for (final visible in [
        'overview',
        'activity',
        'models',
        'permissions',
        'about',
      ]) {
        expect(find.byKey(Key('category-$visible')), findsOneWidget,
            reason: '$visible must stay visible for user accounts');
      }

      // Client PC execution section is shown; host bypass explicitly disabled.
      expect(find.byKey(const Key('client-exec-mode')), findsOneWidget);
      expect(find.byKey(const Key('client-exec-host-bypass')), findsOneWidget);
      expect(find.text('Host tools'), findsNothing);
    });
  });
}
