import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/tool_run_card.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/local_manager_native.dart' as native_mgr;
import 'package:sonder_runtime/local_manager_web.dart' as web_mgr;
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/ui/raise_mode_sheet.dart';

import 'chat_fakes.dart';

Future<ChatController> _startController(
  WidgetTester tester,
  FakeChatBackend backend,
) async {
  SharedPreferences.setMockInitialValues(<String, Object>{});
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  final c = ChatController(backend, model: 'sonder');
  await c.start();
  await tester.pump();
  return c;
}

void main() {
  group('Wire headers and receipt handoff parsing', () {
    test(
        'SonderApi.chatStream sends X-Sonder-Client-Exec header matching setting',
        () async {
      final seenHeaders = <String?>[];
      await http.runWithClient(() async {
        final api = SonderApi(baseUrl: 'https://host.test');
        await api
            .chatStream(
              const [ChatMessage(role: Role.user, content: 'hi')],
              model: 'sonder',
              clientExec: true,
            )
            .toList();
        await api
            .chatStream(
              const [ChatMessage(role: Role.user, content: 'hi')],
              model: 'sonder',
              clientExec: false,
            )
            .toList();
      }, () {
        return MockClient.streaming((req, bodyStream) async {
          seenHeaders.add(req.headers['X-Sonder-Client-Exec']);
          const payload = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
              'data: [DONE]\n\n';
          return http.StreamedResponse(
            Stream.value(utf8.encode(payload)),
            200,
            headers: {'content-type': 'text/event-stream'},
          );
        });
      });
      expect(seenHeaders, ['1', '0']);
    });

    test('ChatResponseMetadata parses client_tool_handoff receipt entries', () {
      final meta = ChatResponseMetadata.fromJson({
        'receipt': {
          'client_tool_handoff': [
            {
              'call_id': 'a1b2c3d4',
              'tool': 'file_read',
              'risk': 'read_only',
              'session': 'sess-99',
              'args': {'path': 'notes.txt'},
            },
          ],
        },
      });
      expect(meta.clientToolHandoffs, hasLength(1));
      expect(meta.clientToolHandoffs.first.callId, 'a1b2c3d4');
      expect(meta.clientToolHandoffs.first.tool, 'file_read');
      expect(meta.clientToolHandoffs.first.risk, 'read_only');
      expect(meta.clientToolHandoffs.first.session, 'sess-99');
      expect(meta.clientToolHandoffs.first.args, {'path': 'notes.txt'});
    });

    test('SonderApi.submitClientToolResult posts result payload', () async {
      late http.Request seen;
      await http.runWithClient(() async {
        final api = SonderApi(baseUrl: 'https://host.test', apiKey: 'k');
        await api.submitClientToolResult(
          'deadbeef1234',
          ok: true,
          output: 'hello world',
          denied: false,
          session: 'sess-42',
        );
      }, () {
        return MockClient((r) async {
          seen = r;
          return http.Response('{"ok":true,"accepted":true}', 200);
        });
      });
      expect(seen.method, 'POST');
      expect(seen.url.path, '/v1/client-tools/deadbeef1234/result');
      expect(jsonDecode(seen.body), {
        'ok': true,
        'output': 'hello world',
        'error': '',
        'denied': false,
        'session': 'sess-42',
      });
    });
  });

  group('Web companion transport (127.0.0.1:11439)', () {
    test('checkClientAgent reports missing companion cleanly', () async {
      final health = await http.runWithClient(
        () => web_mgr.LocalManager.checkClientAgent(
          companionUrl: 'http://127.0.0.1:11439',
        ),
        () => MockClient((r) async => throw http.ClientException('refused')),
      );
      expect(health.reachable, isFalse);
      expect(
        clientCompanionLaunchCommand(),
        contains('python -m sonder_runtime.interfaces.client_agent'),
      );
    });

    test(
        'web LocalManager talks to companion health, status, mode, and execute endpoints',
        () async {
      final seen = <String>[];
      await http.runWithClient(() async {
        final health = await web_mgr.LocalManager.checkClientAgent(
          companionUrl: 'http://127.0.0.1:11439',
          token: 'tok-local',
        );
        expect(health.reachable, isTrue);
        expect(health.authenticated, isTrue);
        expect(health.mode, 'manual');
        expect(health.workspace, r'C:\work');

        final switched = await web_mgr.LocalManager.setClientAgentMode(
          mode: 'bypass',
          confirmBypass: true,
          companionUrl: 'http://127.0.0.1:11439',
          token: 'tok-local',
        );
        expect(switched, isTrue);

        final res = await web_mgr.LocalManager.executeClientTool(
          callId: 'cafe1234',
          tool: 'file_read',
          args: const {'path': 'README.md'},
          risk: 'read_only',
          mode: 'bypass',
          workspaceRoot: r'C:\work',
          approved: true,
          companionUrl: 'http://127.0.0.1:11439',
          companionToken: 'tok-local',
        );
        expect(res.ok, isTrue);
        expect(res.output, 'readme contents');
      }, () {
        return MockClient((r) async {
          seen.add('${r.method} ${r.url.path}');
          if (r.url.path == '/health') {
            return http.Response(
              jsonEncode({'ok': true, 'service': 'sonder-client-agent'}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          expect(r.headers['X-Sonder-Client-Agent-Token'], 'tok-local');
          if (r.url.path == '/v1/client-agent/status') {
            return http.Response(
              jsonEncode({
                'ok': true,
                'mode': 'manual',
                'workspace': r'C:\work',
                'can_bypass': true,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/client-agent/mode') {
            expect(jsonDecode(r.body),
                {'mode': 'bypass', 'confirm_bypass': true});
            return http.Response(
              jsonEncode({'ok': true, 'mode': 'bypass'}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (r.url.path == '/v1/client-agent/execute') {
            final body = jsonDecode(r.body) as Map<String, dynamic>;
            expect(body['call_id'], 'cafe1234');
            expect(body['tool'], 'file_read');
            expect(body['approved'], isTrue);
            return http.Response(
              jsonEncode({'ok': true, 'output': 'readme contents'}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response('{}', 404);
        });
      });

      expect(seen, [
        'GET /health',
        'GET /v1/client-agent/status',
        'POST /v1/client-agent/mode',
        'POST /v1/client-agent/execute',
      ]);
    });
  });

  group('Native desktop client tool execution', () {
    test(
        'enforces workspace root outside bypass and executes file/directory tools',
        () async {
      final root = await Directory.systemTemp.createTemp('sonder_client_exec_');
      addTearDown(() => root.delete(recursive: true));

      final outside =
          await Directory.systemTemp.createTemp('sonder_outside_exec_');
      addTearDown(() => outside.delete(recursive: true));
      final outsideFile = File('${outside.path}${Platform.pathSeparator}x.txt');
      await outsideFile.writeAsString('secret');

      // Write inside workspace.
      final writeRes = await native_mgr.LocalManager.executeClientTool(
        callId: 'c1',
        tool: 'file_write',
        args: const {'path': 'hello.txt', 'content': 'alpha\nbeta'},
        mode: 'auto',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(writeRes.ok, isTrue);

      // Read inside workspace.
      final readRes = await native_mgr.LocalManager.executeClientTool(
        callId: 'c2',
        tool: 'file_read',
        args: const {'path': 'hello.txt'},
        mode: 'auto',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(readRes.ok, isTrue);
      expect(readRes.output, contains('alpha'));

      // Directory tree inside workspace.
      final listRes = await native_mgr.LocalManager.executeClientTool(
        callId: 'c3',
        tool: 'directory_tree',
        args: const {'path': '.'},
        mode: 'auto',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(listRes.ok, isTrue);
      expect(listRes.output, contains('hello.txt'));

      // Outside workspace denied in auto mode, allowed in bypass mode.
      final blocked = await native_mgr.LocalManager.executeClientTool(
        callId: 'c4',
        tool: 'file_read',
        args: {'path': outsideFile.path},
        mode: 'auto',
        workspaceRoot: root.path,
        approved: true,
      );
      expect(blocked.ok, isFalse);
      expect(blocked.error, contains('workspace'));

      final bypassed = await native_mgr.LocalManager.executeClientTool(
        callId: 'c5',
        tool: 'file_read',
        args: {'path': outsideFile.path},
        mode: 'bypass',
        workspaceRoot: root.path,
        approved: false,
      );
      expect(bypassed.ok, isTrue);
      expect(bypassed.output, contains('secret'));
    });
  });

  group('ChatController + ToolRunCard client approval and bypass confirmation',
      () {
    testWidgets(
        'manual mode prompts "Approve on my PC" / "Deny" and submits user decision',
        (tester) async {
      final backend = FakeChatBackend();
      backend.clientExecHandler = (callId, tool, args, risk, mode, approved) async =>
          const ClientToolExecResult(ok: true, output: 'local pc ok');

      final c = await _startController(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'manual';
      c.clientWorkspaceRoot = r'C:\proj';

      final sendFuture = c.send('run locally');
      await tester.pump();

      final event = ChatStreamToolEvent.fromJson({
        'id': 'cafe0001deadbeef',
        'call_id': 'cafe0001deadbeef',
        'tool': 'file_write',
        'phase': 'awaiting_client_result',
        'execution_target': 'client',
        'risk': 'workspace_write',
        'session': 'sess-pc-1',
        'args': {'path': 'notes.txt', 'content': 'hi'},
      });
      backend.lastTurn.tool(event);
      await tester.pump();

      // Did not auto-execute while in manual mode, and live event awaits PC approval.
      expect(backend.clientToolExecs, isEmpty);
      final liveEvent = c.live.value!.toolCalls.single;
      expect(liveEvent.isAwaitingClientApproval, isTrue);

      // Render ToolRunCard in client awaiting approval state and tap "Approve on my PC".
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: ToolRunCard(
            event: liveEvent,
            onApprove: (callId, tool, digest) =>
                c.approveClientTool(callId, tool, digest),
            onDeny: (callId, tool, digest) =>
                c.denyClientTool(callId, tool, digest),
          ),
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('tool-run-backend-badge')), findsOneWidget);
      expect(find.text('client PC'), findsOneWidget);
      expect(find.text('Approve on my PC'), findsOneWidget);
      expect(find.text('Deny'), findsOneWidget);

      await tester.tap(find.byKey(const Key('tool-run-approve')));
      await tester.pumpAndSettle();

      expect(backend.clientToolExecs, hasLength(1));
      expect(backend.clientToolResults, [
        ('cafe0001deadbeef', true, 'local pc ok', '', false, 'sess-pc-1'),
      ]);

      backend.lastTurn.done('finished');
      await sendFuture;
      c.dispose();
    });

    testWidgets(
        'manual mode Deny submits denied=true without executing locally',
        (tester) async {
      final backend = FakeChatBackend();
      final c = await _startController(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'manual';

      final sendFuture = c.send('deny locally');
      await tester.pump();

      backend.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'dead0002beefcafe',
        'call_id': 'dead0002beefcafe',
        'tool': 'run_program',
        'phase': 'awaiting_client_result',
        'execution_target': 'client',
        'risk': 'host_exec',
        'session': 'sess-pc-2',
        'args': {'command': 'whoami'},
      }));
      await tester.pump();

      final liveEvent = c.live.value!.toolCalls.single;
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: ToolRunCard(
            event: liveEvent,
            onApprove: (callId, tool, digest) =>
                c.approveClientTool(callId, tool, digest),
            onDeny: (callId, tool, digest) =>
                c.denyClientTool(callId, tool, digest),
          ),
        ),
      ));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('tool-run-deny')));
      await tester.pumpAndSettle();

      expect(backend.clientToolExecs, isEmpty);
      expect(backend.clientToolResults, hasLength(1));
      expect(backend.clientToolResults.single.$1, 'dead0002beefcafe');
      expect(backend.clientToolResults.single.$2, isFalse);
      expect(backend.clientToolResults.single.$5, isTrue);

      backend.lastTurn.done('ok');
      await sendFuture;
      c.dispose();
    });

    testWidgets(
        'client-local PermissionMode.bypass requires explicit confirmation to enable and auto-runs non-stream handoffs',
        (tester) async {
      final backend = FakeChatBackend();
      backend.clientExecHandler = (callId, tool, args, risk, mode, approved) async =>
          ClientToolExecResult(ok: true, output: 'ran $tool in $mode');

      final c = await _startController(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'manual';
      c.clientWorkspaceRoot = r'C:\proj';

      // Cancelling confirmation leaves clientPermissionMode unchanged.
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () {
              c.requestClientModeChange(
                'bypass',
                confirm: (from, to) => showRaiseModeSheet(
                  context,
                  from: from,
                  to: to,
                  host: 'your PC',
                ),
              );
            },
            child: const Text('Raise'),
          ),
        ),
      ));

      await tester.tap(find.text('Raise'));
      await tester.pumpAndSettle();
      expect(find.byType(RaiseModeSheet), findsOneWidget);
      await tester.tap(find.byKey(const Key('raise-mode-cancel')));
      await tester.pumpAndSettle();
      expect(c.clientPermissionMode, 'manual');

      // Accepting confirmation (including bypass ack checkbox) enables client-local bypass.
      await tester.tap(find.text('Raise'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('raise-mode-bypass-ack')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('raise-mode-confirm')));
      await tester.pumpAndSettle();
      expect(c.clientPermissionMode, 'bypass');

      // Non-stream client_tool_handoff executes immediately under client bypass.
      final sendFuture = c.send('run handoff');
      await tester.pump();
      backend.lastTurn.done(
        'handing off',
        metadata: const ChatResponseMetadata(
          clientToolHandoffs: [
            ClientToolHandoff(
              callId: 'feedface0001',
              tool: 'run_program',
              risk: 'host_exec',
              session: 'sess-h1',
              args: {'command': 'echo hi'},
            ),
          ],
        ),
      );
      await sendFuture;
      await tester.pumpAndSettle();

      expect(backend.clientToolExecs, hasLength(1));
      final call = backend.clientToolExecs.single;
      expect(call.$1, 'run_program');
      expect(call.$2, {'command': 'echo hi'});
      expect(call.$3, isTrue);
      expect(call.$4, 'bypass');

      expect(backend.clientToolResults, [
        (
          'feedface0001',
          true,
          'ran run_program in bypass',
          '',
          false,
          'sess-h1',
        ),
      ]);
      c.dispose();
    });

    test('P2: native executor lexically refuses UNC, device, extended, NT, NUL, and drive-relative paths in all modes',
        () async {
      final tmp = await Directory.systemTemp.createTemp('sonder_p2_paths_');
      try {
        const hostilePaths = <String>[
          r'\\nohost.invalid\share\secret.txt',
          '//nohost.invalid/share/secret.txt',
          r'\\.\C:\Windows\win.ini',
          r'\\?\UNC\nohost.invalid\share\a.txt',
          r'\??\C:\Windows\win.ini',
          r'\??',
          '/??',
          '/??/C:/Windows/win.ini',
          'C:relative.txt',
          'C:',
          'ok.txt\x00evil',
          'bad\npath.txt',
        ];
        for (final mode in const ['manual', 'acceptEdits', 'bypass']) {
          for (final bad in hostilePaths) {
            final res = await native_mgr.LocalManager.executeClientTool(
              callId: 'p2-path',
              tool: 'file_read',
              args: {'path': bad},
              risk: 'read_only',
              mode: mode,
              workspaceRoot: tmp.path,
              approved: true,
            );
            expect(res.ok, isFalse, reason: 'mode=$mode path=$bad');
          }
          // Hostile workspaceRoot is also refused lexically.
          final rootRes = await native_mgr.LocalManager.executeClientTool(
            callId: 'p2-root',
            tool: 'file_read',
            args: {'path': 'a.txt'},
            risk: 'read_only',
            mode: mode,
            workspaceRoot: r'\\nohost.invalid\share',
            approved: true,
          );
          expect(rootRes.ok, isFalse, reason: 'mode=$mode hostile root');
        }
      } finally {
        await tmp.delete(recursive: true);
      }
    });

    testWidgets(
        'P3: clientExecEnabled=false never executes streamed or non-stream client tools (even read_only or acceptEdits/auto)',
        (tester) async {
      final backend = FakeChatBackend();
      final c = await _startController(tester, backend);
      c.clientExecEnabled = false;
      c.clientPermissionMode = 'auto';
      c.clientWorkspaceRoot = r'C:\proj';

      // 1. Streamed server-emitted read_only + write tools must NEVER call executeClientTool.
      final sendFuture = c.send('check files');
      await tester.pump();
      backend.lastTurn.tool(ChatStreamToolEvent.fromJson(const {
        'id': 'cafe00000001',
        'call_id': 'cafe00000001',
        'execution_target': 'client',
        'backend': 'client',
        'phase': 'awaiting_client_result',
        'tool': 'file_read',
        'risk': 'read_only',
        'session': 'sess-off',
        'args': {'path': 'secret.txt'},
      }));
      backend.lastTurn.tool(ChatStreamToolEvent.fromJson(const {
        'id': 'cafe00000002',
        'call_id': 'cafe00000002',
        'execution_target': 'client',
        'backend': 'client',
        'phase': 'awaiting_client_result',
        'tool': 'file_write',
        'risk': 'write',
        'session': 'sess-off',
        'args': {'path': 'x.txt', 'content': 'pwn'},
      }));
      await tester.pump();
      await tester.pump();
      expect(backend.clientToolExecs, isEmpty);
      expect(backend.clientToolResults, hasLength(2));
      for (final r in backend.clientToolResults) {
        expect(r.$2, isFalse);
        expect(r.$5, isTrue); // denied=true
        expect(r.$4, contains('disabled'));
      }

      // 2. Non-stream handoffs in _finishOk must ALSO skip execution when disabled.
      backend.lastTurn.done(
        'done',
        metadata: const ChatResponseMetadata(
          clientToolHandoffs: [
            ClientToolHandoff(
              callId: 'cafe00000003',
              tool: 'file_read',
              risk: 'read_only',
              session: 'sess-off',
              args: {'path': 'secret.txt'},
            ),
            ClientToolHandoff(
              callId: 'cafe00000004',
              tool: 'file_write',
              risk: 'write',
              session: 'sess-off',
              args: {'path': 'x.txt', 'content': 'pwn'},
            ),
          ],
        ),
      );
      await sendFuture;
      await tester.pumpAndSettle();
      expect(backend.clientToolExecs, isEmpty);

      // 3. Direct approveClientTool must refuse immediately when disabled.
      final approveOutcome =
          await c.approveClientTool('cafe00000003', 'file_read', '');
      expect(approveOutcome.status, ApprovalStatus.failed);
      expect(approveOutcome.message, contains('disabled'));
      expect(backend.clientToolExecs, isEmpty);

      c.dispose();
    });
  });
}
