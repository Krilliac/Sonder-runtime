import 'dart:async';

import 'package:sonder_runtime/settings.dart';

/// Widget tests have no generated native runner, so platform-plugin method
/// channels are intentionally unavailable. Keep credential behavior in the
/// same interface while providing an immediate in-memory keychain substitute.
class _TestCredentialStore implements CredentialStore {
  final Map<String, String> _values = <String, String>{};

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String value) async {
    _values[key] = value;
  }
}

Future<void> testExecutable(FutureOr<void> Function() testMain) async {
  Settings.testingCredentialStore = _TestCredentialStore();
  await testMain();
  Settings.testingCredentialStore = null;
}
