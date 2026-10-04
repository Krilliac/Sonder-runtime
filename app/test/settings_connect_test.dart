import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/status_row.dart';

import 'runtime_fixtures.dart';

class MemoryCredentials implements CredentialStore {
  final values = <String, String>{};
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> write(String key, String value) async => values[key] = value;
  @override
  Future<void> delete(String key) async => values.remove(key);
}

class ThrowingCredentials implements CredentialStore {
  @override
  Future<String?> read(String key) async => throw StateError('keyring down');
  @override
  Future<void> write(String key, String value) async =>
      throw StateError('keyring down');
  @override
  Future<void> delete(String key) async => throw StateError('keyring down');
}

/// Scripted [SettingsConnection]: no network.
class FakeConnection extends SettingsConnection {
  Object? testError;
  List<String> models = const ['sonder'];
  final registerSecrets = <String?>[];
  bool requireBootstrap = true;

  /// When set, each `/v1/models` probe waits for the next completer here.
  final gates = <Completer<void>>[];
  int probes = 0;

  FakeConnection({this.testError});

  /// Per-row routing fields of `/v1/models`.
  Map<String, ModelOrigin> origins = const {};

  @override
  Future<ModelCatalog> testServer(
      String serverUrl, String apiKey, AccountSession? account) async {
    final gate = probes < gates.length ? gates[probes] : null;
    probes++;
    if (gate != null) await gate.future;
    if (testError != null) throw testError!;
    return ModelCatalog(ids: models, origins: origins);
  }

  /// The provider bindings the runtime reports; null reads as unknown.
  EcosystemStatus? routing;

  @override
  Future<EcosystemStatus?> routingStatus(
          String serverUrl, String apiKey, AccountSession? account) async =>
      routing;

  /// What the host launcher answers; null throws "cannot reach".
  LauncherStatus? launcher;
  final launcherCalls = <String>[];

  @override
  Future<LauncherStatus> launcherStatus(String launcherUrl, String token) async {
    launcherCalls.add(launcherUrl);
    final status = launcher;
    if (status == null) {
      throw SonderException('Cannot reach host launcher: refused');
    }
    return status;
  }

  @override
  Future<String> register(
      String serverUrl, String apiKey, String username, String password,
      {String? bootstrapSecret}) async {
    registerSecrets.add(bootstrapSecret);
    if (requireBootstrap && (bootstrapSecret ?? '').isEmpty) {
      throw const BootstrapSecretRequired(
          'first-admin bootstrap is not authorized');
    }
    return 'Account $username created (role admin).';
  }
}

Future<void> pumpSettings(
  WidgetTester tester, {
  required SettingsConnection connection,
  String serverUrl = 'http://mypc.local:11435',
  Settings? settings,
  Size size = const Size(1000, 1800),
  ThemeData? theme,
  String? category,
  ValueChanged<Settings>? onChanged,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(MaterialApp(
    theme: theme ?? SonderTheme.dark,
    home: SettingsScreen(
      settings: settings ?? Settings(serverUrl: serverUrl),
      onChanged: onChanged ?? (_) {},
      connection: connection,
      initialCategory: category,
    ),
  ));
  await tester.pumpAndSettle();
}

/// Opens a Settings page: from the rail on a wide window, or back to the
/// list first on a phone.
Future<void> openSettingsPage(WidgetTester tester, String id) async {
  final back = find.byTooltip('All settings sections');
  if (back.evaluate().isNotEmpty) {
    await tester.tap(back);
    await tester.pumpAndSettle();
  }
  await tester.tap(find.byKey(Key('category-$id')));
  await tester.pumpAndSettle();
}

/// A Settings text field by its key suffix: `settingsField('server-url')`.
Finder settingsField(String name) => find.byKey(Key('settings-$name'));

/// Saves through the unsaved-changes bar, once it has risen.
Future<void> saveSettings(WidgetTester tester) async {
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('settings-save')));
  await tester.pumpAndSettle();
}

Future<void> tapTest(WidgetTester tester) async {
  await tester.tap(find.byKey(const Key('settings-test-connection')));
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('diagnoseConnectionError', () {
    test('421 from lane A transport (status + code)', () {
      final d = diagnoseConnectionError(
          SonderException('host is not allowed for this listener',
              httpStatus: 421, code: 'HOST_NOT_ALLOWED'),
          'http://mypc.local:11435');
      expect(d.state, ServerReachability.refused);
      expect(d.title, contains('Refused'));
      expect(d.title, contains('mypc.local'));
      expect(d.serverSetting, 'SONDER_ALLOWED_HOSTS=mypc.local');
      expect(d.adbHint, isNull);
    });

    test('421 from the older message-only transport', () {
      final d = diagnoseConnectionError(
          SonderException('Server returned HTTP 421.'), 'http://mypc:11435');
      expect(d.state, ServerReachability.refused);
      expect(d.detail, contains('SONDER_ALLOWED_HOSTS'));
    });

    test('Android emulator host gets the adb reverse hint', () {
      final d = diagnoseConnectionError(
          SonderException('x', httpStatus: 421), 'http://10.0.2.2:11461');
      expect(d.adbHint, contains('adb reverse tcp:11461 tcp:11461'));
    });

    test('429 carries the wait; transport failure is unreachable', () {
      expect(
          diagnoseConnectionError(
                  SonderException('x', httpStatus: 429, retryAfterSeconds: 42),
                  'https://pc.test')
              .title,
          contains('Try again in 42 s'));
      expect(
          diagnoseConnectionError(
                  SonderException('Cannot reach server: SocketException'),
                  'http://pc.test:11435')
              .state,
          ServerReachability.unreachable);
    });

    test('reachable over LAN http needs HTTPS for sign-in', () {
      expect(diagnoseReachable('http://192.168.1.20:11435').state,
          ServerReachability.needsHttps);
      expect(diagnoseReachable('http://127.0.0.1:11435').state,
          ServerReachability.reachable);
      expect(diagnoseReachable('https://pc.tailnet.ts.net').state,
          ServerReachability.reachable);
    });
  });

  testWidgets('421 shows Refused, the host, the setting, and copies it',
      (tester) async {
    String? copied;
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') {
        copied = (call.arguments as Map)['text'] as String;
      }
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null));
    await pumpSettings(tester,
        connection: FakeConnection(
            testError: SonderException('host is not allowed',
                httpStatus: 421, code: 'HOST_NOT_ALLOWED')));
    await openSettingsPage(tester, SettingsCategory.connection);
    await tapTest(tester);
    final notice = find.byKey(const Key('settings-connection-notice'));
    expect(
        find.descendant(of: notice, matching: find.textContaining('Refused')),
        findsOneWidget);
    expect(
        find.descendant(
            of: notice, matching: find.textContaining('mypc.local')),
        findsWidgets);
    expect(find.text('SONDER_ALLOWED_HOSTS=mypc.local'), findsOneWidget);
    expect(find.text('Connect to your PC'), findsOneWidget);
    expect(find.textContaining('refused (421)'), findsOneWidget);
    await tester.tap(find.text('Copy server setting'));
    await tester.pump();
    expect(copied, 'SONDER_ALLOWED_HOSTS=mypc.local');
  });

  testWidgets('Test connection names routes served by Sonder Inference',
      (tester) async {
    final connection = FakeConnection()
      ..models = const ['sonder', 'general', 'qwen3:14b', 'llama3:8b']
      ..routing = EcosystemReading.parse(
              ecosystemJson(inference: inferenceStatusJson()))
          .status;
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.connection);
    await tapTest(tester);
    expect(
        find.textContaining('Sonder Inference serves 2 routes (mock:tiny); '
            '2 exact models run directly on Ollama.'),
        findsOneWidget);
    expect(find.textContaining('4 models available'), findsNothing);
  });

  testWidgets('Test connection uses /v1/models routing without admin',
      (tester) async {
    final connection = FakeConnection()
      ..models = const ['sonder', 'general', 'llama3:8b']
      ..origins = const {
        'sonder': ModelOrigin(
            kind: 'route',
            provider: 'sonder_inference',
            servedModel: 'qwen3:14b'),
        'general': ModelOrigin(
            kind: 'route',
            provider: 'sonder_inference',
            servedModel: 'qwen3:14b'),
        'llama3:8b': ModelOrigin(kind: 'model', provider: 'ollama'),
      };
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.connection);
    await tapTest(tester);
    expect(
        find.textContaining('Sonder Inference serves 2 routes (qwen3:14b); '
            '1 exact model runs directly on Ollama.'),
        findsOneWidget);
  });

  testWidgets('connect card status words', (tester) async {
    final connection = FakeConnection();
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.connection);
    await tapTest(tester);
    // Lane B's StatusMark draws the glyph and the word as separate texts.
    final reachable = find.widgetWithText(StatusMark, 'reachable');
    expect(reachable, findsOneWidget);
    expect(find.descendant(of: reachable, matching: find.text('✓')),
        findsOneWidget);
    connection.testError = SonderException('Cannot reach server: refused');
    await tapTest(tester);
    expect(find.textContaining('unreachable'), findsOneWidget);
    expect(find.textContaining("Can't reach 127.0.0.1"), findsOneWidget);
  });

  testWidgets('a result for an address since edited is not shown',
      (tester) async {
    final gate = Completer<void>();
    final connection = FakeConnection()..gates.add(gate);
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.connection);
    await tester.tap(find.byKey(const Key('settings-test-connection')));
    await tester.pump();
    await tester.enterText(settingsField('server-url'), 'http://127.0.0.1:1');
    gate.complete();
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-connection-notice')), findsNothing);
  });

  testWidgets('bootstrap secret appears after 403, is sent once, never saved',
      (tester) async {
    final credentials = MemoryCredentials();
    Settings.testingCredentialStore = credentials;
    addTearDown(() => Settings.testingCredentialStore = null);
    final connection = FakeConnection();
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'https://pc.test',
        category: SettingsCategory.account);
    expect(settingsField('bootstrap-secret'), findsNothing);
    await tester.enterText(settingsField('username'), 'alice');
    await tester.enterText(settingsField('password'), 'password123');
    await tester.tap(find.text('Register'));
    await tester.pumpAndSettle();
    expect(settingsField('bootstrap-secret'), findsOneWidget);
    expect(connection.registerSecrets, [null]);
    await tester.enterText(settingsField('bootstrap-secret'), 's3cr3t-boot');
    await tester.tap(find.text('Register'));
    await tester.pumpAndSettle();
    expect(connection.registerSecrets, [null, 's3cr3t-boot']);
    expect(find.text('Account alice created (role admin).'), findsOneWidget);
    expect(settingsField('bootstrap-secret'), findsNothing);
    // Sign-in fields are not settings: nothing is waiting to be saved.
    expect(find.byKey(const Key('settings-save')), findsNothing);
    // Save a real change, then check nothing at rest holds the secret.
    await openSettingsPage(tester, SettingsCategory.privacy);
    await tester.tap(find.byKey(const Key('settings-approximate-location')));
    await saveSettings(tester);
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('sonder_server_url'), 'https://pc.test');
    for (final key in preferences.getKeys()) {
      expect('${preferences.get(key)}', isNot(contains('s3cr3t-boot')));
    }
    expect(credentials.values.values.join(), isNot(contains('s3cr3t-boot')));
  });

  testWidgets('editing the server URL forgets a bootstrap secret',
      (tester) async {
    final connection = FakeConnection();
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'https://pc.test',
        category: SettingsCategory.account);
    await tester.enterText(settingsField('username'), 'alice');
    await tester.enterText(settingsField('password'), 'password123');
    await tester.tap(find.text('Register'));
    await tester.pumpAndSettle();
    await tester.enterText(settingsField('bootstrap-secret'), 's3cr3t-boot');
    await openSettingsPage(tester, SettingsCategory.connection);
    await tester.enterText(settingsField('server-url'), 'https://other.test');
    await tester.pumpAndSettle();
    await openSettingsPage(tester, SettingsCategory.account);
    expect(settingsField('bootstrap-secret'), findsNothing);
    await tester.tap(find.text('Register'));
    await tester.pumpAndSettle();
    expect(connection.registerSecrets, [null, null]);
  });

  group('SettingsConnection.register', () {
    test('201 is success and names the role', () async {
      await http.runWithClient(() async {
        final message = await const SettingsConnection()
            .register('https://pc.test', '', 'alice', 'password123');
        expect(message, 'Account alice created (role admin).');
      },
          () => MockClient((request) async => http.Response(
              '{"ok": true, "account": {"username": "alice", "role": "admin"}}',
              201)));
    });

    test('403 bootstrap asks for the secret; the header carries it', () async {
      final headers = <String?>[];
      await http.runWithClient(() async {
        await expectLater(
            const SettingsConnection()
                .register('https://pc.test', '', 'alice', 'password123'),
            throwsA(isA<BootstrapSecretRequired>()));
        await const SettingsConnection().register(
            'https://pc.test', '', 'alice', 'password123',
            bootstrapSecret: 'boot');
      },
          () => MockClient((request) async {
                expect(request.followRedirects, isFalse);
                final secret = request.headers['X-Sonder-Bootstrap-Secret'];
                headers.add(secret);
                return secret == null
                    ? http.Response(
                        '{"ok": false, "message": "first-admin bootstrap is not authorized"}',
                        403)
                    : http.Response(
                        '{"ok": true, "account": {"username": "alice", "role": "admin"}}',
                        201);
              }));
      expect(headers, [null, 'boot']);
    });
  });

  testWidgets(
      'keyring failure saves preferences and says the key was not saved',
      (tester) async {
    Settings.testingCredentialStore = ThrowingCredentials();
    addTearDown(() => Settings.testingCredentialStore = null);
    await pumpSettings(tester,
        connection: FakeConnection(),
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.connection);
    await tester.enterText(settingsField('api-key'), 'deploy-key');
    await saveSettings(tester);
    expect(find.textContaining('System keyring unavailable: key not saved'),
        findsOneWidget);
    final preferences = await SharedPreferences.getInstance();
    expect(
        preferences.getString('sonder_server_url'), 'http://127.0.0.1:11435');
    expect(preferences.containsKey('sonder_api_key'), isFalse);
  });

  test('web keeps keys in memory only', () async {
    Settings.debugMemoryOnlyCredentials = true;
    addTearDown(() => Settings.debugMemoryOnlyCredentials = null);
    final credentials = MemoryCredentials();
    // A key an older build left at rest must not come back after a reload.
    credentials.values['sonder_api_key'] = 'old-key';
    SharedPreferences.setMockInitialValues({'sonder_api_key': 'old-plain'});
    final result =
        await Settings(apiKey: 'k').save(credentialStore: credentials);
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.containsKey('sonder_api_key'), isFalse);
    expect(result.memoryOnly, isTrue);
    expect(result.warning, contains('in memory'));
    expect(credentials.values, isEmpty);
    final nothingToStore = await Settings().save(credentialStore: credentials);
    expect(nothingToStore.warning, isNull);
  });

  testWidgets('eye icons say what they do; forget needs a session',
      (tester) async {
    await pumpSettings(tester,
        connection: FakeConnection(), category: SettingsCategory.connection);
    expect(find.byTooltip('Show API key'), findsOneWidget);
    await tester.tap(find.byTooltip('Show API key'));
    await tester.pump();
    expect(find.byTooltip('Hide API key'), findsOneWidget);
    expect(find.byTooltip('Show launcher token'), findsOneWidget);
    // Without a session there is nothing to sign out of or forget: the
    // page offers the sign-in form instead.
    await openSettingsPage(tester, SettingsCategory.account);
    expect(find.text('Forget local session'), findsNothing);
    expect(find.text('Sign out'), findsNothing);
    expect(find.text('Login'), findsOneWidget);
    // One return control.
    expect(find.byTooltip('Back to chat'), findsOneWidget);
    expect(find.byIcon(Icons.arrow_back), findsOneWidget);
  });

  testWidgets('phone settings fit at text scale 1.0, 1.5 and 2.0',
      (tester) async {
    for (final scale in [1.0, 1.5, 2.0]) {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      await tester.pumpWidget(MediaQuery(
        data: MediaQueryData(
            size: const Size(390, 844), textScaler: TextScaler.linear(scale)),
        child: MaterialApp(
          theme: SonderTheme.dark,
          home: SettingsScreen(
            settings: Settings(serverUrl: 'http://mypc.local:11435'),
            onChanged: (_) {},
            connection: FakeConnection(
                testError: SonderException('x', httpStatus: 421)),
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: 'list at $scale');
      for (final page in [
        SettingsCategory.general,
        SettingsCategory.connection,
        SettingsCategory.account,
        SettingsCategory.appearance,
        SettingsCategory.privacy,
        SettingsCategory.desktop,
        SettingsCategory.observatory,
        SettingsCategory.about,
      ]) {
        // The list's own scrollable (the search field has one too).
        await tester.scrollUntilVisible(
            find.byKey(Key('category-$page')), 120,
            scrollable: find
                .descendant(
                    of: find.byKey(const Key('settings-categories')),
                    matching: find.byType(Scrollable))
                .first);
        // Fully in view, not just peeking in at an edge.
        await tester.ensureVisible(find.byKey(Key('category-$page')));
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(Key('category-$page')));
        await tester.pumpAndSettle();
        if (page == SettingsCategory.connection) {
          await tester.ensureVisible(
              find.byKey(const Key('settings-test-connection')));
          await tester.pumpAndSettle();
          await tapTest(tester);
          // A pending change raises the bar: it has to fit too.
          await tester.enterText(settingsField('server-url'), 'http://pc:1');
          await tester.pumpAndSettle();
        }
        expect(tester.takeException(), isNull, reason: '$page at $scale');
        await tester.tap(find.byTooltip('All settings sections'));
        await tester.pumpAndSettle();
      }
    }
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });

  testWidgets('Settings meets tap-target and label guidelines on a phone',
      (tester) async {
    final handle = tester.ensureSemantics();
    await pumpSettings(tester,
        connection: FakeConnection(), size: const Size(390, 844));
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    for (final page in [
      SettingsCategory.general,
      SettingsCategory.connection,
      SettingsCategory.account,
      SettingsCategory.appearance,
      SettingsCategory.privacy,
      SettingsCategory.desktop,
      SettingsCategory.observatory,
      SettingsCategory.about,
    ]) {
      await openSettingsPage(tester, page);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline),
          reason: page);
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline),
          reason: page);
    }
    handle.dispose();
  });
}
