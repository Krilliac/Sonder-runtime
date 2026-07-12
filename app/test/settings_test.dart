import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/settings.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('launcher URL is derived from the configured host', () {
    final settings = Settings(serverUrl: 'https://trilobite.example:11435/v1');
    expect(settings.effectiveLauncherUrl, 'https://trilobite.example:11436');

    settings.launcherUrl = 'https://control.example:443/';
    expect(settings.effectiveLauncherUrl, 'https://control.example:443/');
  });

  test('launcher credentials persist independently from the API key', () async {
    SharedPreferences.setMockInitialValues({});
    final settings = Settings(
      apiKey: 'main-api-key',
      launcherUrl: 'https://host.test:11436',
      launcherToken: 'launcher-token',
    );
    await settings.save();
    final restored = await Settings.load();

    expect(restored.apiKey, 'main-api-key');
    expect(restored.launcherUrl, 'https://host.test:11436');
    expect(restored.launcherToken, 'launcher-token');
  });
}
