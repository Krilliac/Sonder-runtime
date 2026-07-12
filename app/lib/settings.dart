import 'package:shared_preferences/shared_preferences.dart';

/// Persisted connection settings (server URL, API key, theme).
class Settings {
  static const _kServer = 'server_url';
  static const _kKey = 'api_key';
  static const _kDark = 'dark_mode';
  static const _kModel = 'model';
  static const _kAllowHosted = 'allow_hosted';
  static const _kContextSize = 'context_size';
  static const _kKeepServerRunning = 'keep_server_running';
  static const _kAllowApproximateLocation = 'allow_approximate_location';
  static const _kLauncherUrl = 'launcher_url';
  static const _kLauncherToken = 'launcher_token';

  static const defaultModel = 'trilobite';

  String serverUrl;
  String apiKey;
  bool darkMode;
  String model; // which server tier/model to use ("trilobite" = local student)
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

  String get effectiveLauncherUrl {
    if (launcherUrl.trim().isNotEmpty) return launcherUrl.trim();
    final server = Uri.tryParse(serverUrl.trim());
    if (server == null || server.host.isEmpty) return '';
    return server
        .replace(port: 11436, path: '', query: null, fragment: null)
        .toString()
        .replaceAll(RegExp(r'/$'), '');
  }

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
