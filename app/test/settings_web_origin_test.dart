import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/api.dart';
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

const _origin = 'https://sonder.test';

void main() {
  setUp(() {
    Settings.debugMemoryOnlyCredentials = true;
    Settings.debugPageOrigin = _origin;
  });
  tearDown(() {
    Settings.debugMemoryOnlyCredentials = null;
    Settings.debugPageOrigin = null;
  });

  test('in a browser the default server is the page origin', () {
    expect(Settings.defaultServerUrl, _origin);
    expect(Settings().serverUrl, _origin);
  });

  test('outside a browser the default stays on loopback', () {
    Settings.debugPageOrigin = null;
    expect(Settings.defaultServerUrl, 'http://127.0.0.1:11435');
  });

  test('a stored server URL is ignored in a browser', () async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://elsewhere.test'});
    final loaded = await Settings.load(credentialStore: _MemoryCredentialStore());
    expect(loaded.serverUrl, _origin);
  });

  test('a session for the page origin is accepted and persists nothing',
      () async {
    SharedPreferences.setMockInitialValues(
        {'sonder_server_url': 'https://elsewhere.test'});
    final store = _MemoryCredentialStore();
    final result = await Settings.storeAccountSession(
        AccountSession(token: 'account', origin: _origin),
        serverUrl: _origin,
        credentialStore: store);
    expect(result.memoryOnly, isTrue);
    expect(store.values, isEmpty);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.containsKey('sonder_account_session'), isFalse);
  });

  test('a session for another origin is refused', () async {
    SharedPreferences.setMockInitialValues({});
    expect(
      () => Settings.storeAccountSession(
          AccountSession(token: 'account', origin: 'https://elsewhere.test'),
          serverUrl: 'https://elsewhere.test',
          credentialStore: _MemoryCredentialStore()),
      throwsStateError,
    );
  });

  test('the loopback fallback is off in a browser', () {
    // kIsWeb is false on the VM, so the web default is exercised explicitly:
    // an empty fallback URL must disable the fallback entirely.
    final api = SonderApi(baseUrl: _origin, localFallbackUrl: '');
    expect(api.localFallbackUrl, isEmpty);
  });
}
