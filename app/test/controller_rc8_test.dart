import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

Future<ChatController> _startController(
  WidgetTester tester,
  FakeChatBackend backend,
) async {
  SharedPreferences.setMockInitialValues(<String, Object>{});
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  final controller = ChatController(backend, model: 'sonder');
  await controller.start();
  await tester.pump();
  return controller;
}

class _UnclosedLateTurn implements ChatTurn {
  final StreamController<TurnEvent> controller =
      StreamController<TurnEvent>.broadcast();
  int cancels = 0;

  @override
  Stream<TurnEvent> get events => controller.stream;

  @override
  void cancel() {
    cancels++;
    // Intentionally leave controller open so we can verify generation guards
    // reject late events emitted after identity switch.
  }
}

class _UnclosedLateStreamBackend extends FakeChatBackend {
  _UnclosedLateStreamBackend({super.serverUrl});

  _UnclosedLateTurn? lateTurn;

  @override
  ChatTurn startTurn(TurnRequest request) {
    final turn = _UnclosedLateTurn();
    lateTurn = turn;
    return turn;
  }
}

void main() {
  group('RC8: switching server or signing in/out discards active turn', () {
    testWidgets(
        'active stream is cancelled and partial pending reply is discarded on identity switch',
        (tester) async {
      final serverA = FakeChatBackend(serverUrl: 'https://server-a.example');
      final controller = await _startController(tester, serverA);

      final sendFuture = controller.send('question on server A');
      await tester.pump();

      final oldTurn = serverA.lastTurn;
      oldTurn.delta('partial leak from A');
      await tester.pump();
      expect(
        controller.messages.map((m) => m.content),
        contains('partial leak from A'),
      );

      // Identity/server changes while server A is mid-stream.
      final serverB = FakeChatBackend(serverUrl: 'https://server-b.example');
      controller.updateBackend(serverB, identityChanged: true);
      await tester.pump();
      await sendFuture;

      expect(oldTurn.cancels, greaterThanOrEqualTo(1));
      expect(controller.sending, isFalse);
      expect(
        controller.messages.any((m) => m.content.contains('partial leak')),
        isFalse,
      );

      // Sending on server B still works normally and never contains server A's reply.
      final sendB = controller.send('question on server B');
      await tester.pump();
      serverB.lastTurn.done('answer from B');
      await sendB;
      await tester.pump();

      expect(
        controller.messages.map((m) => m.content),
        ['question on server A', 'question on server B', 'answer from B'],
      );
      controller.dispose();
    });

    testWidgets(
        'late deltas and done arriving after identity switch never bleed or persist into new server',
        (tester) async {
      final serverA =
          _UnclosedLateStreamBackend(serverUrl: 'https://server-a.example');
      final controller = await _startController(tester, serverA);

      final sendFuture = controller.send('question on server A');
      await tester.pump();

      serverA.lateTurn!.controller.add(const TurnDelta('partial from A'));
      await tester.pump();
      expect(
        controller.messages.map((m) => m.content),
        contains('partial from A'),
      );

      final serverB = FakeChatBackend(serverUrl: 'https://server-b.example');
      controller.updateBackend(serverB, identityChanged: true);
      await tester.pump();

      // Even if an underlying transport pushes buffered chunks after switch,
      // generation guard prevents them from bleeding or persisting.
      serverA.lateTurn!.controller.add(const TurnDelta(' late leak'));
      serverA.lateTurn!.controller.add(
        const TurnDone(
          ChatReply(
            text: 'full answer from A',
            metadata: ChatResponseMetadata(),
          ),
        ),
      );
      await serverA.lateTurn!.controller.close();
      await sendFuture;
      await tester.pump();

      expect(
        controller.messages.any((m) => m.content.contains('from A')),
        isFalse,
      );
      final stored = await ChatStore.load();
      expect(
        stored.expand((t) => t.messages).any((m) => m.content.contains('from A')),
        isFalse,
      );
      controller.dispose();
    });

    testWidgets(
        'in-flight async client tool execution is invalidated when identity changes',
        (tester) async {
      final serverA = FakeChatBackend(serverUrl: 'https://server-a.example');
      final execCompleter = Completer<ClientToolExecResult>();
      serverA.clientExecHandler = (callId, tool, args, risk, mode, approved) =>
          execCompleter.future;

      final controller = await _startController(tester, serverA);
      controller.clientExecEnabled = true;
      controller.clientPermissionMode = 'bypass';
      controller.clientWorkspaceRoot = r'C:\proj';

      final sendFuture = controller.send('read file on my pc');
      await tester.pump();

      serverA.lastTurn.tool(ChatStreamToolEvent.fromJson({
        'id': 'deadbeef12345678',
        'call_id': 'deadbeef12345678',
        'tool': 'file_read',
        'phase': 'awaiting_client_result',
        'execution_target': 'client',
        'risk': 'read_only',
        'session': 'sess-rc8',
        'args': {'path': 'README.md'},
      }));
      await tester.pump();
      expect(serverA.clientToolExecs, hasLength(1));

      // Sign out / switch account while local tool execution is still running.
      final serverB = FakeChatBackend(serverUrl: 'https://server-b.example');
      controller.updateBackend(serverB, identityChanged: true);
      await tester.pump();

      // Finish the old tool call after the switch.
      execCompleter.complete(const ClientToolExecResult(
        ok: true,
        output: 'secret content',
      ));
      await sendFuture;
      await tester.pump();

      expect(serverA.clientToolResults, isEmpty);
      expect(serverB.clientToolResults, isEmpty);
      expect(
        controller.messages.any((m) => m.content.contains('secret content')),
        isFalse,
      );
      controller.dispose();
    });
  });
}
