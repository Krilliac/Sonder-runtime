import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:trilobite/settings.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('launcher URL is never derived from the configured chat host', () {
    final settings = Settings(serverUrl: 'https://trilobite.example:11435/v1');
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
    expect(embedded.launcherConfigurationError, contains('without credentials'));

    final weak = Settings(
      launcherUrl: 'https://host.test:11436',
      launcherToken: 'short',
    );
    expect(weak.usesHostLauncher, isFalse);
    expect(weak.launcherConfigurationError, contains('at least 24'));

    final loopback = Settings(launcherUrl: 'http://127.0.0.1:11436');
    expect(loopback.usesHostLauncher, isTrue);
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
