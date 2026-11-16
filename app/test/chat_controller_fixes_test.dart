import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

Future<ChatController> _start(
    WidgetTester tester, FakeChatBackend backend) async {
  SharedPreferences.setMockInitialValues(<String, Object>{});
  ChatStore.backend = PrefsChatStoreBackend();
  ChatStore.resetCache();
  final c = ChatController(backend, model: 'sonder');
  await c.start();
  await tester.pump();
  return c;
}

void main() {
  testWidgets('W1: deleting another thread keeps the live turn',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final other = c.currentThread;
    c.newChat();
    await tester.pump();
    final sent = c.send('hello');
    await tester.pump();
    backend.lastTurn.delta('partial');
    await tester.pump();
    await c.deleteThread(other);
    await tester.pump();
    expect(c.messages.map((m) => m.content), contains('partial'));
    backend.lastTurn.done('FULL ANSWER');
    await sent;
    await tester.pump();
    expect(c.messages.map((m) => m.content), ['hello', 'FULL ANSWER']);
    final stored = await ChatStore.load();
    expect(
        stored.single.messages.map((m) => m.content), ['hello', 'FULL ANSWER']);
    c.dispose();
  });

  testWidgets('W1: deleting the current thread, next one has the live turn',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final first = c.currentThread;
    final sent = c.send('hello');
    await tester.pump();
    backend.lastTurn.delta('partial');
    await tester.pump();
    c.newChat();
    await tester.pump();
    final fresh = c.currentThread;
    expect(c.currentThreadId, isNot(first.id));
    await c.deleteThread(fresh);
    await tester.pump();
    expect(c.currentThreadId, first.id);
    // The pending row is back: the next delta fills it with the whole reply.
    backend.lastTurn.delta(' more');
    await tester.pump();
    expect(c.messages.map((m) => m.content), contains('partial more'));
    backend.lastTurn.done('FULL ANSWER');
    await sent;
    await tester.pump();
    expect(c.messages.map((m) => m.content), ['hello', 'FULL ANSWER']);
    c.dispose();
  });

  testWidgets('W8: stopping a turn in a background thread rotates and cleans',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final a = c.currentThreadId;
    final first = c.send('start something');
    await tester.pump();
    c.newChat();
    await tester.pump();
    expect(c.currentThreadId, isNot(a));
    c.cancel();
    await tester.pump();
    await first;
    expect(
        c.threads
            .firstWhere((t) => t.id == a)
            .messages
            .where((m) => m.role == Role.user),
        isEmpty,
        reason: 'dangling user message removed');
    c.switchThread(c.threads.firstWhere((t) => t.id == a));
    await tester.pump();
    final second = c.send('something else');
    await tester.pump();
    final request = backend.lastTurn.request;
    expect(request.sessionId, '$a-1');
    expect(request.historyMode, 'client');
    expect(request.history.map((m) => m.content), ['something else']);
    backend.lastTurn.done('ok');
    await tester.pump();
    await second;
    c.dispose();
  });

  testWidgets(
      'W8: cancelling a later turn in a background thread keeps history',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final a = c.currentThreadId;
    final one = c.send('q1');
    await tester.pump();
    backend.lastTurn.done('a1');
    await one;
    await tester.pump();
    final two = c.send('q2');
    await tester.pump();
    c.newChat();
    await tester.pump();
    c.cancel();
    await tester.pump();
    await two;
    expect(
        c.threads.firstWhere((t) => t.id == a).messages.map((m) => m.content),
        ['q1', 'a1']);
    expect(c.sessionFor(a), a, reason: 'a later turn does not rotate');
    c.dispose();
  });

  testWidgets('loadThreads falls back to a fresh thread when the store throws',
      (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    ChatStore.backend = _ThrowingBackend();
    ChatStore.resetCache();
    final c = ChatController(FakeChatBackend(), model: 'sonder');
    await c.loadThreads();
    expect(c.loadingThreads, isFalse);
    expect(c.currentThreadId, isNotEmpty);
    expect(c.threads, hasLength(1));
    c.dispose();
  });
}

class _ThrowingBackend implements ChatStoreBackend {
  @override
  Future<List<String>> list() async => throw StateError('no disk');
  @override
  Future<String?> read(String name) async => throw StateError('no disk');
  @override
  Future<void> write(String name, String data) async =>
      throw StateError('no disk');
  @override
  Future<void> delete(String name) async => throw StateError('no disk');
  @override
  Future<void> rename(String from, String to) async =>
      throw StateError('no disk');
}
