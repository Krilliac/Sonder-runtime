import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Persistent storage for bearer credentials.
///
/// Keeping this small interface lets the settings model be tested without
/// emulating a platform keychain. Production uses [FlutterSecureStorage],
/// backed by the OS-specific credential store.
abstract interface class CredentialStore {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

class PlatformCredentialStore implements CredentialStore {
  const PlatformCredentialStore([this._storage = const FlutterSecureStorage()]);

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

/// Persisted connection settings (server URL, API key, theme).
class Settings {
  static const _kServer = 'sonder_server_url';
  static const _kKey = 'sonder_api_key';
  static const _kDark = 'sonder_dark_mode';
  static const _kModel = 'sonder_model';
  static const _kAllowHosted = 'sonder_allow_hosted';
  static const _kContextSize = 'sonder_context_size';
  static const _kKeepServerRunning = 'sonder_keep_server_running';
  static const _kAllowApproximateLocation =
      'sonder_allow_approximate_location';
  static const _kLauncherUrl = 'sonder_launcher_url';
  static const _kLauncherToken = 'sonder_launcher_token';
  static const _credentials = PlatformCredentialStore();

  /// Test-only override: widget tests do not load desktop/mobile plugins, and
  /// must never wait indefinitely on an unimplemented credential channel.
  @visibleForTesting
  static CredentialStore? testingCredentialStore;

  static const defaultModel = 'sonder';

  String serverUrl;
  String apiKey;
  bool darkMode;
  // Inference route/model identifier exposed by the Sonder Runtime server.
  String model;
  bool allowHosted;
  String contextSize;
  bool keepServerRunning;
  bool allowApproximateLocation;
  String launcherUrl;
  String launcherToken;

  Settings({
    this.serverUrl = 'http://127.0.0.1:11435',
    this.apiKey = '',
    this.darkMode = true,
    this.model = defaultModel,
    this.allowHosted = false,
    this.contextSize = '8192',
    this.keepServerRunning = false,
    this.allowApproximateLocation = false,
    this.launcherUrl = '',
    this.launcherToken = '',
  });

  bool get isConfigured => serverUrl.trim().isNotEmpty;

  /// Host control is intentionally explicit. Deriving this from [serverUrl]
  /// could send a persisted launcher credential to a newly selected server.
  String get effectiveLauncherUrl =>
      launcherUrl.trim().replaceAll(RegExp(r'/+$'), '');

  bool get hasHostLauncher => effectiveLauncherUrl.isNotEmpty;

  String? get launcherConfigurationError {
    if (!hasHostLauncher) return null;
    final uri = Uri.tryParse(effectiveLauncherUrl);
    if (uri == null ||
        !const {'http', 'https'}.contains(uri.scheme.toLowerCase()) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.path.isNotEmpty && uri.path != '/') {
      return 'Host launcher URL must be an http(s) origin without credentials or a path.';
    }
    final host = uri.host.toLowerCase();
    final loopback = host == 'localhost' ||
        host == '::1' ||
        host == '0:0:0:0:0:0:0:1' ||
        host.startsWith('127.');
    if (!loopback && uri.scheme.toLowerCase() != 'https') {
      return 'A non-loopback host launcher requires an HTTPS endpoint.';
    }
    if (!loopback && launcherToken.trim().length < 24) {
      return 'A non-loopback host launcher requires a token of at least 24 characters.';
    }
    return null;
  }

  bool get usesHostLauncher =>
      hasHostLauncher && launcherConfigurationError == null;

  /// Loads non-sensitive preferences and credentials from separate stores.
  ///
  /// Earlier releases stored bearer tokens in SharedPreferences. Migrate an
  /// existing value only after it was successfully written to secure storage;
  /// if the keychain is unavailable, do not fall back to that plaintext value.
  static Future<Settings> load({CredentialStore? credentialStore}) async {
    final p = await SharedPreferences.getInstance();
    final credentials = credentialStore ?? testingCredentialStore ?? _credentials;
    final apiKey = await _readCredential(
      credentials: credentials,
      preferences: p,
      key: _kKey,
    );
    final launcherToken = await _readCredential(
      credentials: credentials,
      preferences: p,
      key: _kLauncherToken,
    );
    return Settings(
      serverUrl: p.getString(_kServer) ?? 'http://127.0.0.1:11435',
      apiKey: apiKey,
      darkMode: p.getBool(_kDark) ?? true,
      model: p.getString(_kModel) ?? defaultModel,
      allowHosted: p.getBool(_kAllowHosted) ?? false,
      contextSize: p.getString(_kContextSize) ?? '8192',
      keepServerRunning: p.getBool(_kKeepServerRunning) ?? false,
      allowApproximateLocation: p.getBool(_kAllowApproximateLocation) ?? false,
      launcherUrl: p.getString(_kLauncherUrl) ?? '',
      launcherToken: launcherToken,
    );
  }

  /// Writes bearer credentials only to the platform credential store.
  Future<void> save({CredentialStore? credentialStore}) async {
    final p = await SharedPreferences.getInstance();
    final credentials = credentialStore ?? testingCredentialStore ?? _credentials;
    // Empty fields are not enough evidence that an existing credential should
    // be erased: Settings is also constructed for first-run preferences and
    // platform/test environments that do not have a credential provider.
    // The UI uses the explicit clear methods below when a user removes a
    // previously saved credential or signs out.
    if (apiKey.trim().isNotEmpty) {
      await _writeCredential(
        credentials: credentials,
        preferences: p,
        key: _kKey,
        value: apiKey,
      );
    } else {
      await p.remove(_kKey);
    }
    if (launcherToken.trim().isNotEmpty) {
      await _writeCredential(
        credentials: credentials,
        preferences: p,
        key: _kLauncherToken,
        value: launcherToken,
      );
    } else {
      await p.remove(_kLauncherToken);
    }
    await p.setString(_kServer, serverUrl.trim());
    await p.setBool(_kDark, darkMode);
    await p.setString(_kModel, model);
    await p.setBool(_kAllowHosted, allowHosted);
    await p.setString(
      _kContextSize,
      contextSize.trim().isEmpty ? '8192' : contextSize.trim(),
    );
    await p.setBool(_kKeepServerRunning, keepServerRunning);
    await p.setBool(_kAllowApproximateLocation, allowApproximateLocation);
    await p.setString(_kLauncherUrl, launcherUrl.trim());
  }

  /// Forgets the local account/API credential. This is a local sign-out: the
  /// server has no logout route, so a separately copied bearer token remains
  /// valid until its normal expiry or server-side revocation.
  static Future<void> clearApiKey({CredentialStore? credentialStore}) =>
      _deleteCredential(
        credentialStore ?? testingCredentialStore ?? _credentials,
        _kKey,
      );

  /// Removes the distinct launcher-control credential from the device.
  static Future<void> clearLauncherToken({CredentialStore? credentialStore}) =>
      _deleteCredential(
        credentialStore ?? testingCredentialStore ?? _credentials,
        _kLauncherToken,
      );

  static Future<String> _readCredential({
    required CredentialStore credentials,
    required SharedPreferences preferences,
    required String key,
  }) async {
    try {
      final stored = await credentials.read(key);
      if (stored != null) return stored;
      final legacy = preferences.getString(key)?.trim() ?? '';
      if (legacy.isEmpty) return '';
      await credentials.write(key, legacy);
      await preferences.remove(key);
      return legacy;
    } catch (_) {
      // A missing/unavailable credential store must not silently re-enable an
      // old plaintext token. The user can retry saving after fixing it.
      return '';
    }
  }

  static Future<void> _writeCredential({
    required CredentialStore credentials,
    required SharedPreferences preferences,
    required String key,
    required String value,
  }) async {
    final trimmed = value.trim();
    await credentials.write(key, trimmed);
    // Remove a legacy plaintext copy only after the secure operation above
    // completed. This also covers explicit logout/credential clearing.
    await preferences.remove(key);
  }

  static Future<void> _deleteCredential(
    CredentialStore credentials,
    String key,
  ) async {
    await credentials.delete(key);
    final preferences = await SharedPreferences.getInstance();
    await preferences.remove(key);
  }
}
