import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/account_session.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/connection.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/links.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/local_manager_native.dart' as native_mgr;
import 'package:sonder_runtime/local_manager_web.dart' as web_mgr;
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/settings.dart';

import 'chat_fakes.dart';

class _MemStore implements CredentialStore {
  final Map<String, String> values = <String, String>{};
  @override
  Future<void> delete(String key) async => values.remove(key);
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> write(String key, String value) async => values[key] = value;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  tearDown(() {
    Settings.debugMemoryOnlyCredentials = null;
    Settings.debugPageOrigin = null;
    Settings.debugDefaultClientExecEnabled = null;
    ChatController.debugIsWeb = null;
  });

  group('Settings hardening & defaults', () {
    test('clientExecEnabled defaults to platform capability', () async {
      SharedPreferences.setMockInitialValues({});
      Settings.debugDefaultClientExecEnabled = false;
      expect(Settings().clientExecEnabled, isFalse);
      final webLoaded = await Settings.load(credentialStore: _MemStore());
      expect(webLoaded.clientExecEnabled, isFalse);

      Settings.debugDefaultClientExecEnabled = true;
      expect(Settings().clientExecEnabled, isTrue);
      final desktopLoaded = await Settings.load(credentialStore: _MemStore());
      expect(desktopLoaded.clientExecEnabled, isTrue);
    });

    test('launcherConfigurationError blocks 127.evil.com and query/fragment',
        () {
      for (final bad in [
        'http://127.evil.com:11436',
        'http://127.999.0.1:11436',
        'http://127.0.0.1:11436?a=1',
        'http://127.0.0.1:11436#frag',
      ]) {
        final s = Settings(launcherUrl: bad, launcherToken: 'short');
        expect(s.usesHostLauncher, isFalse, reason: bad);
        expect(s.launcherConfigurationError, isNotNull, reason: bad);
      }
      for (final ok in [
        'http://127.0.0.1:11436',
        'http://127.1.2.3:11436',
        'http://localhost:11436',
        'http://[::1]:11436',
      ]) {
        final s = Settings(launcherUrl: ok);
        expect(s.usesHostLauncher, isTrue, reason: ok);
      }
    });

    test('Settings.load respects memoryOnlyCredentials and purges web storage',
        () async {
      Settings.debugMemoryOnlyCredentials = true;
      Settings.debugPageOrigin = 'https://sonder.test';
      SharedPreferences.setMockInitialValues({
        'sonder_api_key': 'legacy-key',
        'sonder_launcher_token': 'legacy-launcher',
        'sonder_client_agent_token': 'legacy-agent',
      });
      final store = _MemStore()
        ..values['sonder_api_key'] = 'stored-key'
        ..values['sonder_launcher_token'] = 'stored-launcher'
        ..values['sonder_client_agent_token'] = 'stored-agent'
        ..values['sonder_account_session'] = jsonEncode({
          'token': 'stored-account',
          'origin': 'https://sonder.test',
        });

      final loaded = await Settings.load(credentialStore: store);
      expect(loaded.apiKey, isEmpty);
      expect(loaded.launcherToken, isEmpty);
      expect(loaded.clientAgentToken, isEmpty);
      expect(loaded.accountSession, isNull);
      expect(store.values, isEmpty);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.containsKey('sonder_api_key'), isFalse);
      expect(prefs.containsKey('sonder_launcher_token'), isFalse);
      expect(prefs.containsKey('sonder_client_agent_token'), isFalse);
    });
  });

  group('SonderLauncherApi NoRedirectClient & bounds', () {
    test('status and action disable followRedirects and reject 302 redirects',
        () async {
      final seenFollow = <bool>[];
      await http.runWithClient(() async {
        const api = SonderLauncherApi(
          baseUrl: 'https://host.test:11436',
          token: 'super-secret-launcher-token',
        );
        await expectLater(
          api.status(),
          throwsA(isA<SonderException>()
              .having((e) => e.httpStatus, 'status', 302)),
        );
        await expectLater(
          api.action('start'),
          throwsA(isA<SonderException>()
              .having((e) => e.httpStatus, 'status', 302)),
        );
      }, () {
        return MockClient((req) async {
          seenFollow.add(req.followRedirects);
          return http.Response(
            '',
            302,
            headers: {'location': 'https://evil.example/steal'},
          );
        });
      });
      expect(seenFollow, [false, false]);
    });

    test('status rejects oversized launcher responses', () async {
      await http.runWithClient(() async {
        const api = SonderLauncherApi(
          baseUrl: 'https://host.test:11436',
          token: 'super-secret-launcher-token',
        );
        await expectLater(
          api.status(),
          throwsA(isA<SonderException>()
              .having((e) => e.message, 'message', contains('too large'))),
        );
      }, () {
        return MockClient((req) async => http.Response('x' * (300 * 1024), 200));
      });
    });
  });

  group('Web companion URI validation', () {
    test('rejects invalid schemes, userInfo, query, fragment, and path',
        () async {
      for (final bad in [
        'ftp://127.0.0.1:11439',
        'http://user:pass@127.0.0.1:11439',
        'http://127.0.0.1:11439?x=1',
        'http://127.0.0.1:11439#frag',
        'http://127.0.0.1:11439/extra',
        'http://127.evil.com:11439',
        'http://127.999.0.1:11439',
      ]) {
        final health = await web_mgr.LocalManager.checkClientAgent(
          companionUrl: bad,
          token: 'tok',
        );
        expect(health.reachable, isFalse, reason: bad);
        expect(health.message, contains('loopback'), reason: bad);
      }
    });
  });

  group('Native local_manager hardening', () {
    test('file_read and text_search block credential stores and .git/config',
        () async {
      final root = await Directory.systemTemp.createTemp('sonder_read_guard_');
      addTearDown(() => root.delete(recursive: true));
      final sep = Platform.pathSeparator;
      await File('${root.path}$sep.env').writeAsString('SECRET_NEEDLE=topsecret');
      await Directory('${root.path}$sep.git').create();
      await File('${root.path}$sep.git${sep}config')
          .writeAsString('[core]\n\turl = SECRET_NEEDLE\n');
      await File('${root.path}${sep}credentials.json')
          .writeAsString('{"token":"SECRET_NEEDLE"}');
      await File('${root.path}${sep}ok.txt')
          .writeAsString('public line with SECRET_NEEDLE');
      await File('${root.path}${sep}malformed.bin')
          .writeAsBytes([0x68, 0x69, 0xFF, 0xFE, 0x0A]);

      for (final secretPath in [
        '.env',
        '.git/config',
        'credentials.json',
      ]) {
        for (final mode in ['manual', 'bypass']) {
          final res = await native_mgr.LocalManager.executeClientTool(
            callId: 'r1',
            tool: 'file_read',
            args: {'path': secretPath},
            mode: mode,
            workspaceRoot: root.path,
            approved: true,
          );
          expect(res.ok, isFalse, reason: '$secretPath ($mode)');
          expect(res.denied, isTrue, reason: '$secretPath ($mode)');
        }
      }

      final malformed = await native_mgr.LocalManager.executeClientTool(
        callId: 'r2',
        tool: 'file_read',
        args: const {'path': 'malformed.bin'},
        mode: 'manual',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(malformed.ok, isTrue);
      expect(malformed.output, contains('hi'));

      final search = await native_mgr.LocalManager.executeClientTool(
        callId: 's1',
        tool: 'text_search',
        args: const {'query': 'SECRET_NEEDLE'},
        mode: 'manual',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(search.ok, isTrue);
      expect(search.output, contains('ok.txt'));
      expect(search.output, isNot(contains('.env')));
      expect(search.output, isNot(contains('.git')));
      expect(search.output, isNot(contains('credentials.json')));
    });

    test('nonexistent Windows drive path does not collapse to CWD', () async {
      final root = await Directory.systemTemp.createTemp('sonder_canon_');
      addTearDown(() => root.delete(recursive: true));
      final outsideNonexistent = Platform.isWindows
          ? r'C:\sonder_definitely_missing_9f8e7d\nested.txt'
          : '/sonder_definitely_missing_9f8e7d/nested.txt';
      final res = await native_mgr.LocalManager.executeClientTool(
        callId: 'c1',
        tool: 'file_read',
        args: {'path': outsideNonexistent},
        mode: 'manual',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(res.ok, isFalse);
      expect(res.error, contains('escapes workspace root'));
    });

    test('clientProcessEnvironment strips secret and SONDER_ env vars', () {
      final scrubbed = native_mgr.LocalManager.clientProcessEnvironment({
        'PATH': r'C:\Windows\System32',
        'SONDER_AUTH_SECRET': 'shh',
        'SONDER_HOME': r'C:\sonder',
        'OPENAI_API_KEY': 'sk-123',
        'MY_TOKEN': 'tok-456',
        'NORMAL_VAR': 'ok',
      });
      expect(scrubbed.containsKey('PATH'), isTrue);
      expect(scrubbed.containsKey('NORMAL_VAR'), isTrue);
      expect(scrubbed.containsKey('SONDER_AUTH_SECRET'), isFalse);
      expect(scrubbed.containsKey('SONDER_HOME'), isFalse);
      expect(scrubbed.containsKey('OPENAI_API_KEY'), isFalse);
      expect(scrubbed.containsKey('MY_TOKEN'), isFalse);
    });

    test('run_program rejects unclosed quotes and reserved device names',
        () async {
      final root = await Directory.systemTemp.createTemp('sonder_prog_');
      addTearDown(() => root.delete(recursive: true));
      final unclosed = await native_mgr.LocalManager.executeClientTool(
        callId: 'p1',
        tool: 'run_program',
        args: const {'command': 'echo "unterminated'},
        mode: 'bypass',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(unclosed.ok, isFalse);
      expect(unclosed.error, contains('Unclosed quote'));

      final reserved = await native_mgr.LocalManager.executeClientTool(
        callId: 'p2',
        tool: 'file_write',
        args: const {'path': 'CON.txt', 'content': 'x'},
        mode: 'bypass',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(reserved.ok, isFalse);
      expect(reserved.error, contains('reserved device name'));
    });
  });

  group('ChatController Web gating, sync off-by-one, live card cleanup & 401 backoff',
      () {
    setUp(() {
      SharedPreferences.setMockInitialValues(<String, Object>{});
      ChatStore.backend = PrefsChatStoreBackend();
      ChatStore.resetCache();
    });

    test(
        'Web client does not send X-Sonder-Client-Exec without companion token for non-restricted user',
        () async {
      ChatController.debugIsWeb = true;
      final backend = FakeChatBackend()..autoReply = 'server handled it';
      final c = ChatController(backend, model: 'sonder')
        ..clientExecEnabled = true
        ..clientAgentToken = '';
      await c.loadThreads();
      expect(c.isClientExecActive, isFalse);
      await c.send('run tool');
      expect(backend.turns.last.request.clientExec, isFalse);

      // With a companion token configured on Web, clientExec activates.
      c.clientAgentToken = 'companion-tok-123';
      expect(c.isClientExecActive, isTrue);

      // Restricted users still keep clientExec active on Web even without token.
      c.clientAgentToken = '';
      backend.whoami = const WhoamiInfo(
        role: 'user',
        publicHosting: true,
        allowedFeatures: AllowedFeatures(hostAdmin: false),
        clientExecSupported: true,
      );
      await c.refreshWhoami();
      expect(c.isRestrictedUser, isTrue);
      expect(c.isClientExecActive, isTrue);
      c.dispose();
    });

    test(
        'syncServerSessions replays server-completed reply when local turn ended in error',
        () async {
      final backend = FakeChatBackend();
      final c = ChatController(backend, model: 'sonder');
      await c.loadThreads();
      final tid = c.currentThreadId;

      final sendFut = c.send('check weather');
      backend.lastTurn.fail(
        SonderException('Stream dropped', code: 'STREAM_INTERRUPTED', retryable: true),
      );
      await sendFut;
      expect(c.messages.length, 2);
      expect(c.messages.last.error, isTrue);

      // Server actually finished turn #1 (turns == 1 == localUserTurns).
      backend.serverSessions = SessionPage(sessions: [
        SessionSummary(
          id: tid,
          title: 'check weather',
          turns: 1,
          updatedAt: DateTime.now().add(const Duration(seconds: 5)),
        ),
      ]);
      backend.serverReplays[tid] = SessionReplay(
        sessionId: tid,
        integrityValid: true,
        crashSafe: true,
        transcript: const [
          ReplayMessage(role: 'user', content: 'check weather', sequence: 1),
          ReplayMessage(
              role: 'assistant',
              content: 'It is sunny and 72F.',
              sequence: 2),
        ],
      );

      await c.syncServerSessions(force: true);
      expect(c.messages.length, 2);
      expect(c.messages.last.error, isFalse);
      expect(c.messages.last.content, 'It is sunny and 72F.');
      c.dispose();
    });

    test('approveClientTool and denyClientTool update live.value.toolCalls',
        () async {
      final backend = FakeChatBackend()
        ..clientExecHandler = (callId, tool, args, risk, mode, approved) async =>
            const ClientToolExecResult(ok: true, output: 'ran ok');
      final c = ChatController(backend, model: 'sonder')
        ..clientExecEnabled = true
        ..clientPermissionMode = 'manual';
      await c.loadThreads();

      final sendFut = c.send('run script');
      backend.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'cafe0001deadbeef',
        'call_id': 'cafe0001deadbeef',
        'execution_target': 'client',
        'tool': 'run_code',
        'risk': 'execute',
        'phase': 'awaiting_client_result',
        'args': {'code': 'print(1)'},
      }));
      await Future<void>.delayed(Duration.zero);
      expect(c.live.value?.phase, 'awaiting approval');
      expect(c.live.value?.toolCalls.single.isAwaitingClientApproval, isTrue);

      final outcome =
          await c.approveClientTool('cafe0001deadbeef', 'run_code', '');
      expect(outcome.status, ApprovalStatus.approved);
      expect(c.live.value?.toolCalls.single.state, 'done');
      expect(c.live.value?.toolCalls.single.isAwaitingClientApproval, isFalse);
      expect(c.live.value?.phase, 'running');

      backend.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'cafe0002deadbeef',
        'call_id': 'cafe0002deadbeef',
        'execution_target': 'client',
        'tool': 'file_delete',
        'risk': 'write',
        'phase': 'awaiting_client_result',
        'args': {'path': 'a.txt'},
      }));
      await Future<void>.delayed(Duration.zero);
      expect(c.live.value?.phase, 'awaiting approval');
      final denyOut =
          await c.denyClientTool('cafe0002deadbeef', 'file_delete', '');
      expect(denyOut.status, ApprovalStatus.approved);
      final denied = c.live.value!.toolCalls
          .firstWhere((t) => t.callId == 'cafe0002deadbeef');
      expect(denied.state, 'denied');
      expect(denied.denied, isTrue);
      expect(c.live.value?.phase, 'running');

      backend.lastTurn.done('all done');
      await sendFut;
      c.dispose();
    });

    testWidgets('unauthorized 401 status pauses automatic background polls',
        (tester) async {
      final backend = FakeChatBackend()
        ..statusError = SonderException('Unauthorized', httpStatus: 401);
      final c = ChatController(backend, model: 'sonder');
      await c.start();
      await tester.pump();
      expect(c.connection.value.state, ConnState.unauthorized);
      final afterFirst = backend.statusCalls;
      await tester.pump(const Duration(seconds: 30));
      expect(backend.statusCalls, afterFirst);
      c.dispose();
    });
  });

  group('Markdown link IDN homograph & mailto checks', () {
    test('detects Cyrillic/Unicode IDN homographs even when Uri percent-encodes',
        () {
      expect(decideLink('apple.com', 'https://\u0430pple.com/login'),
          LinkDecision.confirm);
      expect(decideLink('docs', 'https://\u0430pple.com/'),
          LinkDecision.confirm);
      expect(decideLink('docs', 'https://%d0%b0pple.com/'),
          LinkDecision.confirm);
    });

    test('confirms deceptive mailto links and query params while allowing plain mailto',
        () {
      expect(
          decideLink('mail me', 'mailto:ops@example.com'), LinkDecision.open);
      expect(decideLink('ops@example.com', 'mailto:ops@example.com'),
          LinkDecision.open);
      expect(
          decideLink('https://docs.python.org', 'mailto:attacker@evil.com'),
          LinkDecision.confirm);
      expect(decideLink('ops@good.com', 'mailto:attacker@evil.com'),
          LinkDecision.confirm);
      expect(decideLink('mail me', 'mailto:ops@example.com?body=secret'),
          LinkDecision.confirm);
    });
  });
}
