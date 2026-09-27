@Tags(['golden'])
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';

import 'golden_fonts.dart';

/// The server refuses the phone's hostname (HTTP 421 HOST_NOT_ALLOWED).
class _RefusingConnection extends SettingsConnection {
  const _RefusingConnection();
  @override
  Future<List<String>> testServer(
          String serverUrl, String apiKey, AccountSession? account) async =>
      throw SonderException('host is not allowed for this listener',
          httpStatus: 421, code: 'HOST_NOT_ALLOWED');
}

void main() {
  setUpAll(loadGoldenFonts);
  setUp(() => SharedPreferences.setMockInitialValues({}));

  const sizes = {
    'phone': Size(390, 844),
    'desk': Size(1440, 900),
  };
  final themes = {'dark': SonderTheme.dark, 'light': SonderTheme.light};

  for (final size in sizes.entries) {
    for (final theme in themes.entries) {
      testWidgets('settings_connect_${size.key}_${theme.key}', (tester) async {
        setGoldenSurface(tester, size.value);
        await tester.pumpWidget(MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: theme.value,
          home: SettingsScreen(
            settings: Settings(serverUrl: 'http://mypc.local:11435'),
            onChanged: (_) {},
            connection: const _RefusingConnection(),
          ),
        ));
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(const Key('settings-test-connection')));
        await tester.pumpAndSettle();
        await expectLater(
            find.byType(MaterialApp),
            matchesGoldenFile(
                goldenPath('settings_connect_${size.key}_${theme.key}')));
      });
    }
  }
}
