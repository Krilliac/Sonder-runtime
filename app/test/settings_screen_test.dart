// The categorized Settings surface: pages, the unsaved-changes bar, the
// instant theme, per-action progress, and the contracts that must survive
// the redesign (staged key policy, leave guard, shell chrome).
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings/context_size.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/kit.dart';
import 'package:sonder_runtime/workspace_ui.dart';

import 'settings_connect_test.dart'
    show
        FakeConnection,
        openSettingsPage,
        pumpSettings,
        saveSettings,
        settingsField;

String _text(WidgetTester tester, String name) =>
    tester.widget<TextField>(settingsField(name)).controller!.text;

/// Frames for a page switch or a reveal while an action's spinner runs
/// (pumpAndSettle would wait on the spinner forever).
Future<void> _frames(WidgetTester tester) async {
  for (var i = 0; i < 4; i++) {
    await tester.pump(const Duration(milliseconds: 150));
  }
}

void _surface(WidgetTester tester, Size size) {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));
  tearDown(() => CleartextKeyPolicy.allowOnly(const []));

  group('context size', () {
    test('reads sizes the way the runtime does', () {
      expect(parseContextTokens('8192'), 8192);
      expect(parseContextTokens(' 32k '), 32000);
      expect(parseContextTokens('1.5m'), 1500000);
      expect(parseContextTokens('2.125k'), 2125);
      expect(parseContextTokens('1.5'), isNull);
      expect(parseContextTokens('12345678'), isNull);
      expect(parseContextTokens('8 192'), isNull);
      expect(parseContextTokens(''), isNull);
    });

    test('clamps into what the runtime accepts', () {
      expect(canonicalContextSize(''), '8192');
      expect(canonicalContextSize('100'), '$contextSizeMin');
      expect(canonicalContextSize('2m'), '$contextSizeMax');
      expect(canonicalContextSize('32k'), '32000');
      expect(canonicalContextSize('abc'), 'abc');
      expect(contextSizeError('abc'), isNotNull);
      expect(contextSizeError(''), isNull);
      expect(contextSizeLabel(131072), '128k');
      expect(contextSizeLabel(32000), '32,000');
    });
  });

  testWidgets('one page at a time beside a category rail', (tester) async {
    await pumpSettings(tester, connection: FakeConnection());
    for (final id in [
      SettingsCategory.general,
      SettingsCategory.connection,
      SettingsCategory.account,
      SettingsCategory.appearance,
      SettingsCategory.privacy,
      SettingsCategory.desktop,
      SettingsCategory.observatory,
      SettingsCategory.about,
    ]) {
      expect(find.byKey(Key('category-$id')), findsOneWidget, reason: id);
    }
    expect(find.byKey(const Key('category-page-general')), findsOneWidget);
    expect(find.text('Default model or route'), findsOneWidget);
    expect(find.text('Server URL'), findsNothing);
    await openSettingsPage(tester, SettingsCategory.connection);
    expect(find.byKey(const Key('category-page-connection')), findsOneWidget);
    expect(find.text('Server URL'), findsOneWidget);
    expect(find.text('Default model or route'), findsNothing);

    await tester.enterText(find.byKey(const Key('category-search')), 'api key');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('category-connection')), findsOneWidget);
    expect(find.byKey(const Key('category-general')), findsNothing);
  });

  testWidgets('a phone lists the pages first; a deep link opens one',
      (tester) async {
    await pumpSettings(tester,
        connection: FakeConnection(), size: const Size(390, 844));
    expect(find.byKey(const Key('category-page-general')), findsNothing);
    expect(find.text('Sign in to this server.'), findsOneWidget);
    await openSettingsPage(tester, SettingsCategory.account);
    expect(settingsField('username'), findsOneWidget);
    await tester.tap(find.byTooltip('All settings sections'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('category-general')), findsOneWidget);

    // The /login intercept: Account, with the user name filled in.
    _surface(tester, const Size(390, 844));
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: SettingsScreen(
        key: const ValueKey('deep link'),
        settings: Settings(serverUrl: 'https://pc.test'),
        onChanged: (_) {},
        connection: FakeConnection(),
        initialCategory: SettingsCategory.account,
        initialUsername: 'bob',
      ),
    ));
    await tester.pumpAndSettle();
    expect(_text(tester, 'username'), 'bob');
    expect(_text(tester, 'password'), isEmpty);
  });

  testWidgets('the bar appears only with changes; Discard restores',
      (tester) async {
    await pumpSettings(tester,
        connection: FakeConnection(), category: SettingsCategory.connection);
    expect(find.byKey(const Key('settings-save')), findsNothing);
    expect(find.byTooltip('Changed'), findsNothing);

    await tester.enterText(settingsField('server-url'), 'https://new.test');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-save')), findsOneWidget);
    expect(find.textContaining('Unsaved changes'), findsOneWidget);
    // The row and its page in the rail both say what changed.
    expect(find.byTooltip('Changed'), findsOneWidget);
    expect(find.byTooltip('Unsaved changes'), findsOneWidget);

    // Typing the old value back is not a change.
    await tester.enterText(
        settingsField('server-url'), 'http://mypc.local:11435');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-save')), findsNothing);

    await tester.enterText(settingsField('server-url'), 'https://new.test');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-discard')));
    await tester.pumpAndSettle();
    expect(_text(tester, 'server-url'), 'http://mypc.local:11435');
    expect(find.byKey(const Key('settings-save')), findsNothing);
    expect(find.byTooltip('Unsaved changes'), findsNothing);
  });

  testWidgets('Save writes staged values, then the bar goes', (tester) async {
    final changes = <Settings>[];
    await pumpSettings(tester,
        connection: FakeConnection(),
        category: SettingsCategory.connection,
        onChanged: changes.add);
    await tester.enterText(settingsField('server-url'), 'https://new.test ');
    await saveSettings(tester);
    expect(changes.single.serverUrl, 'https://new.test');
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('sonder_server_url'), 'https://new.test');
    expect(find.text('Settings saved'), findsOneWidget);
    expect(find.byKey(const Key('settings-save')), findsNothing);
    expect(find.byTooltip('Changed'), findsNothing);
  });

  testWidgets('the theme applies at once and never carries staged values',
      (tester) async {
    final changes = <Settings>[];
    await pumpSettings(tester,
        connection: FakeConnection(),
        settings: Settings(serverUrl: 'http://mypc.local:11435', apiKey: 'k'),
        category: SettingsCategory.connection,
        onChanged: changes.add);
    // A staged edit elsewhere must not ride along with the theme.
    await tester.enterText(settingsField('server-url'), 'https://staged.test');
    await tester.enterText(settingsField('api-key'), 'staged-key');
    await openSettingsPage(tester, SettingsCategory.appearance);
    await tester.tap(find.text('Light'));
    await tester.pumpAndSettle();

    expect(changes, hasLength(1));
    expect(changes.single.themeMode, 'light');
    expect(changes.single.serverUrl, 'http://mypc.local:11435');
    expect(changes.single.apiKey, 'k');
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('sonder_theme_mode'), 'light');
    expect(preferences.getBool('sonder_dark_mode'), isFalse);
    expect(preferences.containsKey('sonder_server_url'), isFalse);
    // The connection edits are still pending; the theme is not.
    expect(find.textContaining('Unsaved changes · Connection'), findsOneWidget);
  });

  testWidgets('each network action has its own progress', (tester) async {
    final gate = Completer<void>();
    final connection = FakeConnection()
      ..gates.add(gate)
      ..launcher = const LauncherStatus(
          ok: true,
          launcher: 'ok',
          serverRunning: true,
          serverState: 'healthy',
          serverHost: '127.0.0.1',
          serverPort: 11435,
          lastAction: '',
          lastError: '');
    await pumpSettings(tester,
        connection: connection,
        settings: Settings(
            serverUrl: 'http://127.0.0.1:11435',
            launcherUrl: 'http://127.0.0.1:11436'),
        category: SettingsCategory.connection);
    await tester.tap(find.byKey(const Key('settings-test-connection')));
    await _frames(tester);
    expect(find.text('Testing…'), findsOneWidget);
    // Nothing else is locked while the connection test runs.
    final launcher = tester.widget<ButtonStyleButton>(
        find.byKey(const Key('settings-test-launcher')));
    expect(launcher.onPressed, isNotNull);
    await tester.ensureVisible(find.byKey(const Key('settings-test-launcher')));
    await tester.tap(find.byKey(const Key('settings-test-launcher')));
    await _frames(tester);
    expect(find.text('The launcher is ready; the main server is running.'),
        findsOneWidget);
    expect(find.text('Testing…'), findsOneWidget);
    gate.complete();
    await tester.pumpAndSettle();
    expect(find.text('Testing…'), findsNothing);
    expect(find.byKey(const Key('settings-connection-notice')), findsOneWidget);
  });

  testWidgets('Test host control explains its result under its own row',
      (tester) async {
    final connection = FakeConnection();
    await pumpSettings(tester,
        connection: connection, category: SettingsCategory.connection);
    final test = find.byKey(const Key('settings-test-launcher'));
    await tester.ensureVisible(test);
    await tester.tap(test);
    await tester.pumpAndSettle();
    expect(find.text('Enter the host launcher URL first.'), findsOneWidget);

    await tester.enterText(
        settingsField('launcher-url'), 'https://host.test:11436');
    await tester.enterText(settingsField('launcher-token'), 'short');
    await tester.tap(test);
    await tester.pumpAndSettle();
    expect(find.textContaining('at least 24 characters'), findsOneWidget);
    expect(connection.launcherCalls, isEmpty);

    await tester.enterText(settingsField('launcher-token'), 'x' * 24);
    await tester.tap(test);
    await tester.pumpAndSettle();
    expect(connection.launcherCalls, ['https://host.test:11436']);
    expect(find.textContaining('Cannot reach host launcher'), findsOneWidget);
    // The result is not filed under the account section.
    expect(find.byKey(const Key('category-page-connection')), findsOneWidget);
  });

  testWidgets(
      'overlapping checks use the unsaved choice for their host only, then '
      'restore the saved policy', (tester) async {
    // As Settings.load leaves it: another host is allowed and saved.
    CleartextKeyPolicy.allowOnly(['10.0.0.9:11435']);
    final gates = [Completer<void>(), Completer<void>()];
    final connection = FakeConnection()..gates.addAll(gates);
    await pumpSettings(tester,
        connection: connection,
        settings: Settings(
            serverUrl: 'http://192.168.1.20:11435',
            apiKey: 'secret-key',
            cleartextKeyHosts: const ['10.0.0.9:11435']),
        category: SettingsCategory.connection);
    await tester.tap(find.byKey(const Key('settings-cleartext-key-allow')));
    await tester.pumpAndSettle();
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isFalse,
        reason: 'staged only');

    await tester.tap(find.byKey(const Key('settings-test-connection')));
    await _frames(tester);
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isTrue,
        reason: 'the check uses the unsaved choice for its own host');
    expect(CleartextKeyPolicy.allows('http://10.0.0.9:11435'), isTrue,
        reason: 'other hosts keep the saved policy');

    // A second check overlaps: the model list for the same server.
    await tester.tap(find.byKey(const Key('category-general')));
    await _frames(tester);
    await tester.tap(find.byKey(const Key('settings-model')));
    await _frames(tester);
    expect(connection.probes, 2);

    gates[0].complete();
    await _frames(tester);
    expect(CleartextKeyPolicy.allows('http://192.168.1.20:11435'), isTrue,
        reason: 'one check still runs');
    gates[1].complete();
    await tester.pumpAndSettle();
    expect(CleartextKeyPolicy.allowedHosts, {'10.0.0.9:11435'});
  });

  testWidgets('Save during a check restores the newly saved policy',
      (tester) async {
    final gate = Completer<void>();
    final connection = FakeConnection()..gates.add(gate);
    await pumpSettings(tester,
        connection: connection,
        settings: Settings(
            serverUrl: 'http://192.168.1.20:11435', apiKey: 'secret-key'),
        category: SettingsCategory.connection);
    await tester.tap(find.byKey(const Key('settings-cleartext-key-allow')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-test-connection')));
    await _frames(tester);
    await tester.tap(find.byKey(const Key('settings-save')));
    await _frames(tester);
    gate.complete();
    await tester.pumpAndSettle();
    expect(CleartextKeyPolicy.allowedHosts, {'192.168.1.20:11435'});
  });

  testWidgets('context size: stops, an exact entry, and clamping',
      (tester) async {
    await pumpSettings(tester,
        connection: FakeConnection(), category: SettingsCategory.general);
    tester
        .widget<Slider>(find.byKey(const Key('settings-context-slider')))
        .onChanged!(3);
    await tester.pumpAndSettle();
    expect(_text(tester, 'context-size'), '32768');
    expect(find.byKey(const Key('settings-save')), findsOneWidget);

    Future<void> enterAndLeave(String value) async {
      await tester.enterText(settingsField('context-size'), value);
      await tester.pumpAndSettle();
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
    }

    await enterAndLeave('100');
    expect(_text(tester, 'context-size'), '512');
    expect(find.textContaining('Raised to 512'), findsOneWidget);
    await enterAndLeave('2m');
    expect(_text(tester, 'context-size'), '1000000');
    expect(find.textContaining('Lowered to'), findsOneWidget);

    await enterAndLeave('1.5');
    expect(find.textContaining('Use a whole number of tokens'),
        findsOneWidget);
    await tester.tap(find.byKey(const Key('settings-save')));
    await tester.pumpAndSettle();
    expect(find.textContaining('Context size: Use a whole number'),
        findsOneWidget);
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.containsKey('sonder_context_size'), isFalse);

    await enterAndLeave('16k');
    expect(_text(tester, 'context-size'), '16000');
    await saveSettings(tester);
    expect(preferences.getString('sonder_context_size'), '16000');
  });

  testWidgets('the model picker lists /v1/models, filters and picks',
      (tester) async {
    final connection = FakeConnection()
      ..models = const ['sonder', 'general', 'qwen3:14b', 'llama3:8b']
      ..origins = const {
        'sonder': ModelOrigin(kind: 'route', provider: 'ollama'),
        'general': ModelOrigin(kind: 'route', provider: 'ollama'),
        'qwen3:14b': ModelOrigin(kind: 'model', provider: 'ollama'),
        'llama3:8b': ModelOrigin(kind: 'model', provider: 'ollama'),
      };
    await pumpSettings(tester,
        connection: connection,
        serverUrl: 'http://127.0.0.1:11435',
        category: SettingsCategory.general);
    await tester.tap(find.byKey(const Key('settings-model')));
    await tester.pumpAndSettle();
    expect(find.text('ROUTES'), findsOneWidget);
    expect(find.byKey(const Key('settings-model-option-sonder')),
        findsOneWidget);
    expect(find.byKey(const Key('settings-model-option-qwen3:14b')),
        findsOneWidget);

    await tester.enterText(
        find.byKey(const Key('settings-model-search')), 'llama');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('settings-model-option-qwen3:14b')),
        findsNothing);
    expect(find.byKey(const Key('settings-model-free-text')), findsOneWidget);
    await tester.tap(find.byKey(const Key('settings-model-option-llama3:8b')));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(OutlinedButton, 'llama3:8b'), findsOneWidget);
    await saveSettings(tester);
    final preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('sonder_model'), 'llama3:8b');
    // One listing served the picker; reopening reuses it.
    await tester.tap(find.byKey(const Key('settings-model')));
    await tester.pumpAndSettle();
    expect(connection.probes, 1);
  });

  testWidgets('a server that cannot list models still takes a typed id',
      (tester) async {
    await pumpSettings(tester,
        connection: FakeConnection(
            testError: SonderException('Cannot reach server: refused')),
        category: SettingsCategory.general);
    await tester.tap(find.byKey(const Key('settings-model')));
    await tester.pumpAndSettle();
    expect(find.textContaining("Couldn't list the server's models"),
        findsOneWidget);
    await tester.enterText(
        find.byKey(const Key('settings-model-search')), 'my-model:7b');
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('settings-model-free-text')));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(OutlinedButton, 'my-model:7b'), findsOneWidget);
    expect(find.byKey(const Key('settings-save')), findsOneWidget);
  });

  testWidgets('inside the app shell: no back arrow, Chat or workspace menu',
      (tester) async {
    _surface(tester, const Size(1200, 900));
    final navigated = <WorkspaceDestination>[];
    var opened = 0;
    Widget shell({required bool sidebar}) => MaterialApp(
          theme: SonderTheme.dark,
          home: ShellScope(
            current: WorkspaceDestination.settings,
            sidebarVisible: sidebar,
            navigate: navigated.add,
            openNavigation: () => opened++,
            child: SettingsScreen(
              // A fresh screen per layout: a phone opens on the list, where
              // the menu button lives (pages have a back arrow instead).
              key: ValueKey(sidebar),
              settings: Settings(),
              onChanged: (_) {},
              onNavigate: (_) => fail('the shell navigates, not onNavigate'),
              connection: FakeConnection(),
            ),
          ),
        );
    await tester.pumpWidget(shell(sidebar: true));
    await tester.pumpAndSettle();
    expect(find.text('Settings'), findsOneWidget);
    expect(find.byTooltip('Back to chat'), findsNothing);
    expect(find.text('Chat'), findsNothing);
    expect(find.byTooltip('Workspace navigation'), findsNothing);
    expect(find.byTooltip('Open navigation'), findsNothing);

    // Cross-links go through the shell, after the unsaved-changes guard.
    await openSettingsPage(tester, SettingsCategory.observatory);
    await tester.enterText(
        settingsField('observatory-web-url'), 'http://127.0.0.1:4173/');
    await tester.pumpAndSettle();
    await tester.tap(find.text('Open Runtime'));
    await tester.pumpAndSettle();
    expect(find.text('Discard unsaved settings?'), findsOneWidget);
    await tester.tap(find.text('Discard changes'));
    await tester.pumpAndSettle();
    expect(navigated, [WorkspaceDestination.runtime]);
    expect(_text(tester, 'observatory-web-url'), isEmpty);

    _surface(tester, const Size(390, 844));
    await tester.pumpWidget(shell(sidebar: false));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Open navigation'));
    expect(opened, 1);
  });

  testWidgets('an app shell can run the unsaved-changes guard',
      (tester) async {
    _surface(tester, const Size(1200, 900));
    Future<bool> Function()? guard;
    await tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: SettingsScreen(
        settings: Settings(),
        onChanged: (_) {},
        connection: FakeConnection(),
        initialCategory: SettingsCategory.connection,
        registerLeaveGuard: (g) => guard = g,
      ),
    ));
    await tester.pumpAndSettle();
    expect(guard, isNotNull);
    expect(await guard!(), isTrue, reason: 'nothing unsaved');

    await tester.enterText(settingsField('server-url'), 'https://new.test');
    await tester.pumpAndSettle();
    var leaving = guard!();
    await tester.pumpAndSettle();
    expect(find.text('Discard unsaved settings?'), findsOneWidget);
    await tester.tap(find.text('Keep editing'));
    await tester.pumpAndSettle();
    expect(await leaving, isFalse);
    expect(_text(tester, 'server-url'), 'https://new.test');

    leaving = guard!();
    await tester.pumpAndSettle();
    await tester.tap(find.text('Discard changes'));
    await tester.pumpAndSettle();
    expect(await leaving, isTrue);
    // A shell may keep the screen alive: the discard is real.
    expect(_text(tester, 'server-url'), Settings.defaultServerUrl);
    expect(find.byKey(const Key('settings-save')), findsNothing);

    await tester.pumpWidget(const SizedBox());
    expect(guard, isNull);
  });
}
