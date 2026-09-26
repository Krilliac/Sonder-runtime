// LocalManager.launchObservatory (integration contract section 10): the
// executable resolution order, one --connect per URL, the URL-encoded web
// fallback, the loopback-only rule, and that no credential ever reaches the
// Observatory process.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/local_manager_native.dart';
import 'package:sonder_runtime/local_manager_web.dart' as web;
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';

const _runtime = 'http://127.0.0.1:11435';
const _urls = ['http://127.0.0.1:11435', 'http://127.0.0.1:11437'];
const _secret = 'sk-live-0123456789abcdef';

class _Recorder {
  final starts = <(String, List<String>, Map<String, String>)>[];
  final opens = <(String, Map<String, String>)>[];
  final lookups = <String>[];
  Set<String> files = {};
  String? onPath;
  bool openSucceeds = true;

  Future<ObservatoryLaunchResult> launch({
    List<String> urls = _urls,
    String runtimeUrl = _runtime,
    String executable = '',
    String webUrl = '',
    Map<String, String> environment = const {'PATH': '/usr/bin'},
  }) =>
      LocalManager.launchObservatory(
        urls,
        runtimeUrl: runtimeUrl,
        executable: executable,
        webUrl: webUrl,
        environment: environment,
        start: (exe, args, env) async => starts.add((exe, args, env)),
        fileExists: files.contains,
        findOnPath: (name, env) {
          lookups.add(name);
          return onPath;
        },
        open: (url, env) async {
          opens.add((url, env));
          return openSucceeds;
        },
      );
}

void main() {
  group('PATH lookup and OS commands', () {
    late Directory temp;
    setUp(() => temp = Directory.systemTemp.createTempSync('obs-path-'));
    tearDown(() => temp.deleteSync(recursive: true));

    test('findExecutableOnPath finds a real file on PATH, in PATH order', () {
      final first = Directory('${temp.path}/first')..createSync();
      final second = Directory('${temp.path}/second')..createSync();
      File('${second.path}/sonder-observatory').writeAsStringSync('');
      final env = {'PATH': '/does/not/exist::${first.path}:${second.path}'};
      expect(
          LocalManager.findExecutableOnPath('sonder-observatory', env,
              operatingSystem: 'linux'),
          '${second.path}/sonder-observatory');
      File('${first.path}/sonder-observatory').writeAsStringSync('');
      expect(
          LocalManager.findExecutableOnPath('sonder-observatory', env,
              operatingSystem: 'linux'),
          '${first.path}/sonder-observatory');
      expect(
          LocalManager.findExecutableOnPath(
              'sonder-observatory', const {'PATH': ''},
              operatingSystem: 'linux'),
          isNull);
    });

    test('on Windows it splits on ; and tries each PATHEXT suffix', () {
      final tried = <String>[];
      final found = LocalManager.findExecutableOnPath(
        'sonder-observatory',
        const {r'Path': r'C:\Tools;C:\Obs\', 'PATHEXT': '.COM;.EXE'},
        operatingSystem: 'windows',
        fileExists: (path) {
          tried.add(path);
          return path == r'C:\Obs\sonder-observatory.exe';
        },
      );
      expect(found, r'C:\Obs\sonder-observatory.exe');
      expect(tried, [
        r'C:\Tools\sonder-observatory',
        r'C:\Tools\sonder-observatory.com',
        r'C:\Tools\sonder-observatory.exe',
        r'C:\Obs\sonder-observatory',
        r'C:\Obs\sonder-observatory.com',
        r'C:\Obs\sonder-observatory.exe',
      ]);
    });

    test('launchObservatory looks up PATH with the injected fileExists',
        () async {
      final starts = <(String, List<String>)>[];
      final result = await LocalManager.launchObservatory(
        _urls,
        runtimeUrl: _runtime,
        environment: const {'PATH': '/usr/bin:/opt/obs/bin'},
        operatingSystem: 'linux',
        fileExists: {'/opt/obs/bin/sonder-observatory'}.contains,
        start: (exe, args, env) async => starts.add((exe, args)),
      );
      expect(result.ok, isTrue);
      expect(starts.single.$1, '/opt/obs/bin/sonder-observatory');
    });

    test('a macOS .app bundle starts through open -n -a with --args', () async {
      const bundle = '/Applications/Sonder Observatory.app';
      final starts = <(String, List<String>)>[];
      final result = await LocalManager.launchObservatory(
        _urls,
        runtimeUrl: _runtime,
        executable: bundle,
        environment: const {'PATH': '/usr/bin'},
        operatingSystem: 'macos',
        fileExists: {bundle}.contains,
        start: (exe, args, env) async => starts.add((exe, args)),
      );
      expect(result.ok, isTrue);
      expect(result.executable, bundle);
      expect(starts.single.$1, 'open');
      expect(starts.single.$2, [
        '-n',
        '-a',
        bundle,
        '--args',
        '--connect',
        'http://127.0.0.1:11435',
        '--connect',
        'http://127.0.0.1:11437',
      ]);
      // Elsewhere the path is run as it is.
      final (program, arguments) = observatoryProcessCommand(
          '/opt/Obs.app', const ['--connect', 'u'],
          operatingSystem: 'linux');
      expect(program, '/opt/Obs.app');
      expect(arguments, ['--connect', 'u']);
    });

    test('an .app bundle directory exists only on macOS', () {
      final bundle = Directory('${temp.path}/Sonder Observatory.app')
        ..createSync();
      expect(
          LocalManager.observatoryPathExists(bundle.path,
              operatingSystem: 'macos'),
          isTrue);
      expect(
          LocalManager.observatoryPathExists(bundle.path,
              operatingSystem: 'linux'),
          isFalse);
      expect(
          LocalManager.observatoryPathExists('${temp.path}/missing.app',
              operatingSystem: 'macos'),
          isFalse);
      final binary = File('${temp.path}/sonder-observatory')
        ..writeAsStringSync('');
      expect(
          LocalManager.observatoryPathExists(binary.path,
              operatingSystem: 'linux'),
          isTrue);
    });

    test('the Windows opener escapes cmd metacharacters', () {
      const url = 'http://127.0.0.1:4173/?fixture=0'
          '&connect=http%3A%2F%2F127.0.0.1%3A11435'
          '&connect=http%3A%2F%2F127.0.0.1%3A11437';
      final (program, arguments) =
          observatoryOpenerCommand(url, operatingSystem: 'windows');
      expect(program, 'cmd.exe');
      expect(arguments.sublist(0, 3), ['/c', 'start', '']);
      expect(
          arguments[3],
          'http://127.0.0.1:4173/?fixture=0'
          '^&connect=http%3A%2F%2F127.0.0.1%3A11435'
          '^&connect=http%3A%2F%2F127.0.0.1%3A11437');
      // Every cmd metacharacter is escaped, and nothing else changes.
      expect(
          observatoryOpenerCommand('a|b<c>d^e(f)g', operatingSystem: 'windows')
              .$2[3],
          'a^|b^<c^>d^^e^(f^)g');
      final mac = observatoryOpenerCommand(url, operatingSystem: 'macos');
      expect(mac.$1, 'open');
      expect(mac.$2, [url]);
      final linux = observatoryOpenerCommand(url, operatingSystem: 'linux');
      expect(linux.$1, 'xdg-open');
      expect(linux.$2, [url]);
    });
  });

  test('Settings executable wins, with one --connect per URL', () async {
    final r = _Recorder()
      ..files = {'/opt/obs/sonder-observatory', '/env/obs'}
      ..onPath = '/usr/bin/sonder-observatory';
    final result = await r.launch(
      executable: '/opt/obs/sonder-observatory',
      environment: const {'SONDER_OBSERVATORY_BIN': '/env/obs'},
    );
    expect(result.ok, isTrue);
    expect(result.mode, ObservatoryLaunchMode.executable);
    expect(r.starts.single.$1, '/opt/obs/sonder-observatory');
    expect(r.starts.single.$2, [
      '--connect',
      'http://127.0.0.1:11435',
      '--connect',
      'http://127.0.0.1:11437',
    ]);
    expect(r.lookups, isEmpty);
    expect(r.opens, isEmpty);
  });

  test('then SONDER_OBSERVATORY_BIN, then sonder-observatory on PATH',
      () async {
    final fromEnv = _Recorder()
      ..files = {'/env/obs'}
      ..onPath = '/usr/bin/sonder-observatory';
    await fromEnv
        .launch(environment: const {'SONDER_OBSERVATORY_BIN': '/env/obs'});
    expect(fromEnv.starts.single.$1, '/env/obs');
    expect(fromEnv.lookups, isEmpty);

    final fromPath = _Recorder()..onPath = '/usr/bin/sonder-observatory';
    final result = await fromPath.launch();
    expect(fromPath.lookups, ['sonder-observatory']);
    expect(fromPath.starts.single.$1, '/usr/bin/sonder-observatory');
    expect(result.message, contains('2 producers'));
  });

  test('a configured executable that is missing is reported, not skipped',
      () async {
    final r = _Recorder()..onPath = '/usr/bin/sonder-observatory';
    final settings = await r.launch(executable: '/nope/obs');
    expect(settings.ok, isFalse);
    expect(settings.message, contains('/nope/obs'));
    final env = await r
        .launch(environment: const {'SONDER_OBSERVATORY_BIN': '/also/nope'});
    expect(env.ok, isFalse);
    expect(env.message, contains('SONDER_OBSERVATORY_BIN'));
    expect(r.starts, isEmpty);
  });

  test('without an executable, opens the URL-encoded web URL', () async {
    final r = _Recorder();
    final result = await r.launch(webUrl: 'http://127.0.0.1:4173/');
    expect(result.ok, isTrue);
    expect(result.mode, ObservatoryLaunchMode.webUrl);
    const expected = 'http://127.0.0.1:4173/?fixture=0'
        '&connect=http%3A%2F%2F127.0.0.1%3A11435'
        '&connect=http%3A%2F%2F127.0.0.1%3A11437';
    expect(result.url, expected);
    expect(r.opens.single.$1, expected);
    final parsed = Uri.parse(expected);
    expect(parsed.queryParametersAll['connect'], _urls);
    expect(parsed.queryParameters['fixture'], '0');
  });

  test('a failed opener still hands back the link to copy', () async {
    final r = _Recorder()..openSucceeds = false;
    final result = await r.launch(webUrl: 'http://localhost:4173');
    expect(result.ok, isFalse);
    expect(result.mode, ObservatoryLaunchMode.unavailable);
    expect(result.url, startsWith('http://localhost:4173?fixture=0&connect='));
  });

  test('with nothing configured, returns guidance', () async {
    final r = _Recorder();
    final result = await r.launch();
    expect(result.ok, isFalse);
    expect(result.mode, ObservatoryLaunchMode.unavailable);
    expect(result.message, observatoryGuidance);
    expect(r.starts, isEmpty);
    expect(r.opens, isEmpty);
  });

  test('never passes a token or API key', () async {
    final r = _Recorder()..onPath = '/usr/bin/sonder-observatory';
    final environment = {
      'PATH': '/usr/bin',
      'HOME': '/home/me',
      'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
      'SONDER_API_KEY': _secret,
      'SONDER_LAUNCHER_TOKEN': _secret,
      'SONDER_INFERENCE_API_KEY': _secret,
      'GITHUB_TOKEN': _secret,
      'MY_SECRET': _secret,
      'DB_PASSWORD': _secret,
    };
    // Connect URLs carrying credentials or a query are dropped.
    final urls = [
      ..._urls,
      'http://user:$_secret@127.0.0.1:9000',
      'http://127.0.0.1:9001/?token=$_secret',
      'http://127.0.0.1:9002/#$_secret',
      'file:///etc/passwd',
    ];
    await r.launch(urls: urls, environment: environment);
    final (exe, args, env) = r.starts.single;
    expect(args.where((a) => a != '--connect'), _urls);
    final everything = [exe, ...args, ...env.keys, ...env.values].join(' ');
    expect(everything, isNot(contains(_secret)));
    expect(env.keys, containsAll(['PATH', 'HOME', 'DBUS_SESSION_BUS_ADDRESS']));
    expect(env.keys, isNot(contains('SONDER_API_KEY')));

    final web = _Recorder();
    await web.launch(
        urls: urls, environment: environment, webUrl: 'http://127.0.0.1:4173');
    final (url, webEnv) = web.opens.single;
    expect(url, isNot(contains(_secret)));
    expect(webEnv.values.join(' '), isNot(contains(_secret)));
  });

  test('is disabled for a non-loopback runtime, with an explanation', () async {
    final r = _Recorder()..onPath = '/usr/bin/sonder-observatory';
    for (final runtime in [
      'http://192.168.1.20:11435',
      'https://sonder.example.com',
      '',
    ]) {
      final result = await r.launch(runtimeUrl: runtime);
      expect(result.ok, isFalse, reason: runtime);
      expect(result.mode, ObservatoryLaunchMode.disabled);
      expect(result.message, observatoryRemoteExplanation);
    }
    for (final runtime in [
      'http://localhost:11435',
      'http://127.0.0.2:11435',
      'http://[::1]:11435',
    ]) {
      final result = await r.launch(runtimeUrl: runtime);
      expect(result.ok, isTrue, reason: runtime);
    }
    expect(r.starts, hasLength(3));
  });

  test('is disabled when the runtime published no connect URLs', () async {
    final r = _Recorder()..onPath = '/usr/bin/sonder-observatory';
    final result = await r.launch(urls: const []);
    expect(result.mode, ObservatoryLaunchMode.disabled);
    expect(result.message, contains('SONDER_OBSERVATORY_EXPORT'));
    expect(r.starts, isEmpty);
  });

  test('web: the unavailable result, with the link to copy', () async {
    final withUrl = await web.LocalManager.launchObservatory(_urls,
        runtimeUrl: _runtime, webUrl: 'http://127.0.0.1:4173/');
    expect(withUrl.ok, isFalse);
    expect(withUrl.mode, ObservatoryLaunchMode.unavailable);
    expect(Uri.parse(withUrl.url).queryParametersAll['connect'], _urls);
    final bare =
        await web.LocalManager.launchObservatory(_urls, runtimeUrl: _runtime);
    expect(bare.mode, ObservatoryLaunchMode.unavailable);
    expect(bare.url, isEmpty);
    final remote = await web.LocalManager.launchObservatory(_urls,
        runtimeUrl: 'https://sonder.example.com');
    expect(remote.mode, ObservatoryLaunchMode.disabled);
  });

  test('connect URLs are de-duplicated and lose trailing slashes', () {
    expect(
        observatoryConnectUrls(const [
          'http://127.0.0.1:11435/',
          'http://127.0.0.1:11435',
          ' https://127.0.0.1:11437 ',
          'ws://127.0.0.1:8765',
          'not a url',
        ]),
        ['http://127.0.0.1:11435', 'https://127.0.0.1:11437']);
  });

  group('Observatory settings', () {
    test('web URL validation and loopback rule', () {
      expect(observatoryWebUrlError(''), isNull);
      expect(observatoryWebUrlError('http://127.0.0.1:4173/'), isNull);
      expect(observatoryWebUrlError('https://obs.example.com/'), isNull);
      expect(
          observatoryWebUrlError('http://obs.example.com/'), contains('HTTPS'));
      expect(observatoryWebUrlError('ftp://127.0.0.1/'), isNotNull);
      expect(
          observatoryWebUrlError('http://127.0.0.1:4173/?token=x'), isNotNull);
      expect(observatoryWebUrlError('http://u:p@127.0.0.1:4173/'), isNotNull);
      expect(observatoryWebUrlError('127.0.0.1:4173'), isNotNull);
      expect(
          Settings(observatoryWebUrl: 'http://obs.example.com')
              .observatoryConfigurationError,
          isNotNull);
    });

    test('defaults to no executable and no web URL', () {
      final settings = Settings();
      expect(settings.observatoryExecutable, '');
      expect(settings.observatoryWebUrl, '');
      expect(settings.observatoryConfigurationError, isNull);
    });

    test('persist in shared preferences, not the credential store', () async {
      SharedPreferences.setMockInitialValues({});
      await Settings(
        observatoryExecutable: ' /opt/obs/sonder-observatory ',
        observatoryWebUrl: 'http://127.0.0.1:4173/',
      ).save();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('sonder_observatory_executable'),
          '/opt/obs/sonder-observatory');
      expect(prefs.getString('sonder_observatory_web_url'),
          'http://127.0.0.1:4173/');
      final loaded = await Settings.load();
      expect(loaded.observatoryExecutable, '/opt/obs/sonder-observatory');
      expect(loaded.observatoryWebUrl, 'http://127.0.0.1:4173/');
    });

    testWidgets('Settings screen validates the web URL before saving',
        (tester) async {
      SharedPreferences.setMockInitialValues({});
      tester.view.physicalSize = const Size(900, 1600);
      tester.view.devicePixelRatio = 1;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      final saved = <Settings>[];
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: SettingsScreen(settings: Settings(), onChanged: saved.add),
      ));
      await tester.pumpAndSettle();
      final webField = find.byKey(const Key('settings-observatory-web-url'));
      await tester.scrollUntilVisible(webField, 300,
          scrollable: find.byType(Scrollable).first);
      // Desktop test hosts can start processes: the executable field shows.
      expect(find.byKey(const Key('settings-observatory-executable')),
          findsOneWidget);

      await tester.enterText(webField, 'http://obs.example.com/');
      await tester.pumpAndSettle();
      expect(find.text('A non-loopback Observatory web URL requires HTTPS.'),
          findsOneWidget);
      await tester.tap(find.byKey(const Key('settings-save')));
      await tester.pumpAndSettle();
      expect(saved, isEmpty);
      // The refusal is also a snack bar; let it go before saving again.
      expect(find.text('A non-loopback Observatory web URL requires HTTPS.'),
          findsNWidgets(2));
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();

      await tester.enterText(webField, 'https://obs.example.com/');
      await tester.pumpAndSettle();
      expect(find.textContaining('A hosted Observatory opens in this browser'),
          findsOneWidget);
      expect(find.textContaining('SONDER_CORS_ORIGINS'), findsOneWidget);
      await tester.enterText(webField, 'http://127.0.0.1:4173/');
      await tester.enterText(
          find.byKey(const Key('settings-observatory-executable')),
          '/opt/obs/sonder-observatory');
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('settings-save')));
      await tester.pumpAndSettle();
      expect(saved.single.observatoryWebUrl, 'http://127.0.0.1:4173/');
      expect(saved.single.observatoryExecutable, '/opt/obs/sonder-observatory');
    });
  });
}
