import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/local_manager_models.dart';

import 'chat_fakes.dart';

Future<ChatController> _start(WidgetTester tester, FakeChatBackend b) async {
  SharedPreferences.setMockInitialValues(<String, Object>{});
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  final c = ChatController(b, model: 'sonder');
  await c.start();
  await tester.pump();
  return c;
}

Map<String, dynamic> _frame(String tool, String risk, {String id = 'cafe0001deadbeef'}) => {
      'id': id,
      'call_id': id,
      'tool': tool,
      'phase': 'awaiting_client_result',
      'execution_target': 'client',
      'risk': risk,
      'session': 'sess-1',
      'args': {'command': 'whoami'},
    };

void main() {
  group('effective client tool risk', () {
    test('the stricter of the declared and the local risk wins', () {
      expect(effectiveClientToolRisk('read_only', 'run_program'), 'execute');
      expect(effectiveClientToolRisk('read_only', 'file_write'), 'write');
      expect(effectiveClientToolRisk('execute', 'file_read'), 'execute');
      expect(effectiveClientToolRisk('', 'file_read'), 'read_only');
      expect(effectiveClientToolRisk('host_exec', 'file_read'), 'host_exec');
    });

    test('an unknown declared risk is the strictest and never auto-runs', () {
      final r = effectiveClientToolRisk('bogus', 'file_read');
      expect(r, 'bogus');
      expect(clientModeAllowsWithoutPrompt('auto', r), isFalse);
      expect(clientModeAllowsWithoutPrompt('acceptEdits', r), isFalse);
    });

    test('one shared ranking orders every risk value', () {
      expect(clientRiskRank(''), lessThan(clientRiskRank('read_only')));
      expect(clientRiskRank(' read_only '), clientRiskRank('read_only'));
      expect(clientRiskRank('read_only'), lessThan(clientRiskRank('write')));
      expect(clientRiskRank('write'), clientRiskRank('workspace_write'));
      expect(clientRiskRank('write'), lessThan(clientRiskRank('execute')));
      expect(clientRiskRank('execute'), clientRiskRank('host_exec'));
      expect(clientRiskRank('execute'), lessThan(clientRiskRank('bogus')));
    });
  });

  group('ChatController client tool handling', () {
    testWidgets('a server that declares read_only cannot downgrade run_program',
        (tester) async {
      final backend = FakeChatBackend();
      final c = await _start(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'acceptEdits';
      final send = c.send('go');
      await tester.pump();

      backend.lastTurn.tool(ChatStreamToolEvent.fromJson(_frame('run_program', 'read_only')));
      await tester.pump();

      expect(backend.clientToolExecs, isEmpty);
      expect(c.live.value!.toolCalls.single.isAwaitingClientApproval, isTrue);
      backend.lastTurn.done('x');
      await send;
      c.dispose();
    });

    testWidgets('a non-stream handoff declared read_only still prompts for run_program',
        (tester) async {
      final backend = FakeChatBackend();
      backend.autoReply = 'done';
      backend.autoMetadata = const ChatResponseMetadata(
        clientToolHandoffs: [
          ClientToolHandoff(
            callId: 'feedface0002',
            tool: 'run_program',
            args: {'command': 'whoami'},
            risk: 'read_only',
          ),
        ],
      );
      final c = await _start(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'manual';
      await c.send('go');
      await tester.pump();
      await tester.pump();

      expect(backend.clientToolExecs, isEmpty);
      c.dispose();
    });

    test('a later finished frame replaces an earlier error and keeps the stricter risk', () {
      final first = ChatStreamToolEvent.fromJson({
        'id': 'abc2',
        'tool': 'run_program',
        'risk': 'read_only',
        'ok': false,
        'error': 'companion unavailable',
      });
      final later = ChatStreamToolEvent.fromJson({
        'id': 'abc2',
        'tool': 'run_program',
        'risk': 'execute',
        'state': 'finished',
        'ok': true,
      });
      final merged = later.mergedOnto(first);
      expect(merged.ok, isTrue);
      expect(merged.error, isNull);
      expect(merged.risk, 'execute');
      final lowered = ChatStreamToolEvent.fromJson({'id': 'abc2', 'risk': 'read_only'})
          .mergedOnto(merged);
      expect(lowered.risk, 'execute');
    });

    testWidgets('a later frame without arguments keeps the arguments',
        (tester) async {
      final backend = FakeChatBackend();
      final c = await _start(tester, backend);
      c.clientExecEnabled = true;
      c.clientPermissionMode = 'manual';
      final send = c.send('go');
      await tester.pump();

      backend.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'abc1',
        'tool': 'run_code',
        'state': 'awaiting_approval',
        'language': 'python',
        'code': 'print(1)',
        'arguments': {'timeout': 5},
      }));
      await tester.pump();
      backend.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'abc1',
        'tool': 'run_code',
        'state': 'started',
      }));
      await tester.pump();

      final live = c.live.value!.toolCalls.single;
      expect(live.state, 'started');
      expect(live.code, 'print(1)');
      expect(live.language, 'python');
      expect(live.args, {'timeout': '5'});
      backend.lastTurn.done('x');
      await send;
      c.dispose();
    });
  });
}
