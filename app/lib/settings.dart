import 'package:shared_preferences/shared_preferences.dart';

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
    if (!loopback && launcherToken.trim().length < 24) {
      return 'A non-loopback host launcher requires a token of at least 24 characters.';
    }
    return null;
  }

  bool get usesHostLauncher =>
      hasHostLauncher && launcherConfigurationError == null;

  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance();
    return Settings(
      serverUrl: p.getString(_kServer) ?? 'http://127.0.0.1:11435',
      apiKey: p.getString(_kKey) ?? '',
      darkMode: p.getBool(_kDark) ?? true,
      model: p.getString(_kModel) ?? defaultModel,
      allowHosted: p.getBool(_kAllowHosted) ?? false,
      contextSize: p.getString(_kContextSize) ?? '8192',
      keepServerRunning: p.getBool(_kKeepServerRunning) ?? false,
      allowApproximateLocation: p.getBool(_kAllowApproximateLocation) ?? false,
      launcherUrl: p.getString(_kLauncherUrl) ?? '',
      launcherToken: p.getString(_kLauncherToken) ?? '',
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kServer, serverUrl.trim());
    await p.setString(_kKey, apiKey.trim());
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
    await p.setString(_kLauncherToken, launcherToken.trim());
  }
}
