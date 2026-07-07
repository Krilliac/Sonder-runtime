import 'package:shared_preferences/shared_preferences.dart';

/// Persisted connection settings (server URL, API key, theme).
class Settings {
  static const _kServer = 'server_url';
  static const _kKey = 'api_key';
  static const _kDark = 'dark_mode';

  String serverUrl;
  String apiKey;
  bool darkMode;

  Settings({
    this.serverUrl = 'http://127.0.0.1:11435',
    this.apiKey = '',
    this.darkMode = true,
  });

  bool get isConfigured => serverUrl.trim().isNotEmpty;

  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance();
    return Settings(
      serverUrl: p.getString(_kServer) ?? 'http://127.0.0.1:11435',
      apiKey: p.getString(_kKey) ?? '',
      darkMode: p.getBool(_kDark) ?? true,
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kServer, serverUrl.trim());
    await p.setString(_kKey, apiKey.trim());
    await p.setBool(_kDark, darkMode);
  }
}
