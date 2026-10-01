import 'dart:convert';
import 'package:sonder_runtime/account_session.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/settings.dart';

class _MemoryCredentialStore implements CredentialStore {
  final Map<String, String> values = <String, String>{};

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }
}

class _FailingCredentialStore implements CredentialStore {
  @override
  Future<void> delete(String key) async => throw StateError('keychain down');

  @override
  Future<String?> read(String key) async => throw StateError('keychain down');

  @override
  Future<void> write(String key, String value) async =>
      throw StateError('keychain down');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('malformed restored account tokens are never activated or rewritten',
      () async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final store = _MemoryCredentialStore();
    for (final token in ['x' * 513, 'has space', 'has\u0000control']) {
      final raw = jsonEncode({'token': token, 'origin': 'https://host.test'});
      store.values['sonder_account_session'] = raw;
      final loaded = await Settings.load(credentialStore: store);
      expect(loaded.accountSession, isNull);
      expect(store.values['sonder_account_session'], raw);
    }
  });
  test('account record is secure, origin-bound and independent', () async {
    SharedPreferences.setMockInitialValues({});
    final store = _MemoryCredentialStore();
    final settings = Settings(
        serverUrl: 'https://host.test',
        apiKey: 'deployment',
        accountSession:
            AccountSession(token: 'account', origin: 'https://host.test'));
    await settings.save(credentialStore: store);
    final restored = await Settings.load(credentialStore: store);
    expect(restored.apiKey, 'deployment');
    expect(restored.accountSession!.token, 'account');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.containsKey('sonder_account_session'), isFalse);
    await prefs.setString('sonder_server_url', 'https://foreign.test');
    final mismatched = await Settings.load(credentialStore: store);
    expect(mismatched.accountSession!.origin, 'https://host.test');
    final before = Map<String, String>.from(store.values);
    await expectLater(
        mismatched.save(credentialStore: store), throwsStateError);
    expect(store.values, before);
    mismatched.serverUrl = 'http://remote.test';
    await expectLater(
        mismatched.save(credentialStore: store), throwsStateError);
    expect(store.values, before);
    await Settings(serverUrl: 'https://foreign.test')
        .save(credentialStore: store);
    expect(store.values['sonder_account_session'],
        before['sonder_account_session']);
    await Settings.clearAccountSession(credentialStore: store);
    expect(store.values['sonder_api_key'], 'deployment');
  });
  test('launcher URL is never derived from the configured chat host', () {
    final settings = Settings(serverUrl: 'https://sonder.example:11435/v1');
    expect(settings.effectiveLauncherUrl, '');
    expect(settings.usesHostLauncher, isFalse);

    settings.launcherUrl = 'https://control.example:443/';
    settings.launcherToken = 'xxxxxxxxxxxxxxxxxxxxxxxx';
    expect(settings.effectiveLauncherUrl, 'https://control.example:443');
    expect(settings.usesHostLauncher, isTrue);
  });

  test('launcher configuration rejects unsafe or weak remote origins', () {
    final embedded = Settings(
      launcherUrl: 'https://user:secret@host.test:11436',
      launcherToken: 'xxxxxxxxxxxxxxxxxxxxxxxx',
    );
    expect(embedded.usesHostLauncher, isFalse);
    expect(
        embedded.launcherConfigurationError, contains('without credentials'));

    final weak = Settings(
      launcherUrl: 'https://host.test:11436',
      launcherToken: 'short',
    );
    expect(weak.usesHostLauncher, isFalse);
    expect(weak.launcherConfigurationError, contains('at least 24'));

    final plaintextRemote = Settings(
      launcherUrl: 'http://host.test:11436',
      launcherToken: 'xxxxxxxxxxxxxxxxxxxxxxxx',
    );
    expect(plaintextRemote.usesHostLauncher, isFalse);
    expect(
      plaintextRemote.launcherConfigurationError,
      contains('requires an HTTPS endpoint'),
    );

    final loopback = Settings(launcherUrl: 'http://127.0.0.1:11436');
    expect(loopback.usesHostLauncher, isTrue);
  });

  test('credentials use the secure store, not plaintext preferences', () async {
    SharedPreferences.setMockInitialValues({});
    final credentials = _MemoryCredentialStore();
    final settings = Settings(
      apiKey: 'main-api-key',
      launcherUrl: 'https://host.test:11436',
      launcherToken: 'launcher-token',
    );
    await settings.save(credentialStore: credentials);
    final restored = await Settings.load(credentialStore: credentials);

    expect(restored.apiKey, 'main-api-key');
    expect(restored.launcherUrl, 'https://host.test:11436');
    expect(restored.launcherToken, 'launcher-token');
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.containsKey('sonder_api_key'), isFalse);
    expect(preferences.containsKey('sonder_launcher_token'), isFalse);
    expect(credentials.values, {
      'sonder_api_key': 'main-api-key',
      'sonder_launcher_token': 'launcher-token',
    });
  });

  test('legacy plaintext credentials migrate only after secure persistence',
      () async {
    SharedPreferences.setMockInitialValues({
      'sonder_api_key': 'legacy-api-key',
      'sonder_launcher_token': 'legacy-launcher-token',
    });
    final credentials = _MemoryCredentialStore();

    final restored = await Settings.load(credentialStore: credentials);
    final preferences = await SharedPreferences.getInstance();

    expect(restored.apiKey, 'legacy-api-key');
    expect(restored.launcherToken, 'legacy-launcher-token');
    expect(preferences.containsKey('sonder_api_key'), isFalse);
    expect(preferences.containsKey('sonder_launcher_token'), isFalse);
    expect(credentials.values.length, 2);
  });

  test('clearing credentials deletes secure and legacy copies', () async {
    SharedPreferences.setMockInitialValues({
      'sonder_api_key': 'legacy-api-key',
      'sonder_launcher_token': 'legacy-launcher-token',
    });
    final credentials = _MemoryCredentialStore()
      ..values['sonder_api_key'] = 'active-api-key'
      ..values['sonder_launcher_token'] = 'active-launcher-token';

    await Settings.clearApiKey(credentialStore: credentials);
    await Settings.clearLauncherToken(credentialStore: credentials);
    final preferences = await SharedPreferences.getInstance();

    expect(credentials.values, isEmpty);
    expect(preferences.containsKey('sonder_api_key'), isFalse);
    expect(preferences.containsKey('sonder_launcher_token'), isFalse);
  });

  test('unavailable secure storage never reloads a plaintext token', () async {
    SharedPreferences.setMockInitialValues({
      'sonder_api_key': 'legacy-api-key',
      'sonder_launcher_token': 'legacy-launcher-token',
    });

    final restored = await Settings.load(
      credentialStore: _FailingCredentialStore(),
    );
    final preferences = await SharedPreferences.getInstance();

    expect(restored.apiKey, isEmpty);
    expect(restored.launcherToken, isEmpty);
    // Do not destroy an old value when the attempted migration did not finish.
    expect(preferences.getString('sonder_api_key'), 'legacy-api-key');
    expect(
      preferences.getString('sonder_launcher_token'),
      'legacy-launcher-token',
    );
  });

  test('copyWith keeps every field and drops the session only on request',
      () {
    final session =
        AccountSession(token: 'account', origin: 'https://host.test');
    final settings = Settings(
      serverUrl: 'https://host.test',
      apiKey: 'deployment',
      accountSession: session,
      themeMode: 'light',
      model: 'code',
      allowHosted: true,
      contextSize: '32768',
      keepServerRunning: true,
      allowApproximateLocation: true,
      launcherUrl: 'https://host.test:11436',
      launcherToken: 'launcher-token',
      observatoryExecutable: '/opt/obs',
      observatoryWebUrl: 'http://127.0.0.1:4173/',
      cleartextKeyHosts: const ['192.168.1.20:11435'],
    );
    final copy = settings.copyWith();
    expect(copy.serverUrl, settings.serverUrl);
    expect(copy.apiKey, 'deployment');
    expect(copy.accountSession, same(session));
    expect(copy.themeMode, 'light');
    expect(copy.model, 'code');
    expect(copy.allowHosted, isTrue);
    expect(copy.contextSize, '32768');
    expect(copy.keepServerRunning, isTrue);
    expect(copy.allowApproximateLocation, isTrue);
    expect(copy.launcherUrl, 'https://host.test:11436');
    expect(copy.launcherToken, 'launcher-token');
    expect(copy.observatoryExecutable, '/opt/obs');
    expect(copy.observatoryWebUrl, 'http://127.0.0.1:4173/');
    expect(copy.cleartextKeyHosts, ['192.168.1.20:11435']);
    // The host list is a copy, not shared.
    expect(identical(copy.cleartextKeyHosts, settings.cleartextKeyHosts),
        isFalse);
    expect(settings.copyWith(themeMode: 'dark').accountSession, same(session));
    expect(
        settings.copyWith(withoutAccountSession: true).accountSession, isNull);
  });

  test('the theme persists alone, with the legacy boolean', () async {
    SharedPreferences.setMockInitialValues({'sonder_server_url': 'https://a'});
    await Settings.saveThemeMode('light');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('sonder_theme_mode'), 'light');
    expect(prefs.getBool('sonder_dark_mode'), isFalse);
    await Settings.saveThemeMode('system');
    expect(prefs.getString('sonder_theme_mode'), 'system');
    expect(prefs.getBool('sonder_dark_mode'), isTrue);
    await Settings.saveThemeMode('neon');
    expect(prefs.getString('sonder_theme_mode'), 'dark');
    // Nothing else is written.
    expect(prefs.getKeys(),
        {'sonder_server_url', 'sonder_theme_mode', 'sonder_dark_mode'});
  });

  test('a fresh session is stored only for the saved server', () async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final store = _MemoryCredentialStore();
    final session =
        AccountSession(token: 'account', origin: 'https://host.test');
    // A server URL the caller has not saved: refused, nothing written.
    await expectLater(
        Settings.storeAccountSession(session,
            serverUrl: 'https://other.test', credentialStore: store),
        throwsStateError);
    expect(store.values, isEmpty);
    // The caller's server matches, but the persisted one does not.
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://other.test'});
    await expectLater(
        Settings.storeAccountSession(session,
            serverUrl: 'https://host.test', credentialStore: store),
        throwsStateError);
    expect(store.values, isEmpty);

    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final result = await Settings.storeAccountSession(session,
        serverUrl: 'https://host.test', credentialStore: store);
    expect(result.credentialsStored, isTrue);
    expect(jsonDecode(store.values['sonder_account_session']!),
        {'token': 'account', 'origin': 'https://host.test'});
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.containsKey('sonder_account_session'), isFalse);
    final loaded = await Settings.load(credentialStore: store);
    expect(loaded.accountSession!.origin, 'https://host.test');
  });

  test('a fresh session is never stored at rest on the web', () async {
    Settings.debugMemoryOnlyCredentials = true;
    addTearDown(() => Settings.debugMemoryOnlyCredentials = null);
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final store = _MemoryCredentialStore()
      ..values['sonder_account_session'] = 'stale';
    final result = await Settings.storeAccountSession(
        AccountSession(token: 'account', origin: 'https://host.test'),
        serverUrl: 'https://host.test',
        credentialStore: store);
    expect(result.memoryOnly, isTrue);
    expect(store.values, isEmpty);
  });

  test('a keyring failure while storing a session is reported', () async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://host.test'});
    final result = await Settings.storeAccountSession(
        AccountSession(token: 'account', origin: 'https://host.test'),
        serverUrl: 'https://host.test',
        credentialStore: _FailingCredentialStore());
    expect(result.credentialsStored, isFalse);
    expect(result.keyringUnavailable, isTrue);
  });

  test('new installs use the Sonder route and preference namespace', () async {
    SharedPreferences.setMockInitialValues({
      'server_url': 'https://old.example:11435',
      'model': 'old-route',
    });

    final settings = await Settings.load();

    expect(settings.serverUrl, 'http://127.0.0.1:11435');
    expect(settings.model, 'sonder');
  });
}
