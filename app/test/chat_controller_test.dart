import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/connection.dart';
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
  testWidgets('status poll never overlaps and idles at 5 s', (tester) async {
    final backend = FakeChatBackend()..statusDelay = const Duration(seconds: 3);
    final c = await _start(tester, backend);
    for (var i = 0; i < 30; i++) {
      await tester.pump(const Duration(seconds: 1));
    }
    expect(backend.maxConcurrentStatus, 1);
    // 3 s request + 5 s idle gap: about 4 requests in 30 s, never 30.
    expect(backend.statusCalls, inInclusiveRange(3, 5));
    c.dispose();
  });

  testWidgets('polls at 1 s only while a turn runs', (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    await tester.pump(const Duration(seconds: 1));
    final idleStart = backend.statusCalls;
    await tester.pump(const Duration(seconds: 10));
    final idle = backend.statusCalls - idleStart;
    expect(idle, inInclusiveRange(1, 3));

    final sent = c.send('long question');
    await tester.pump();
    final busyStart = backend.statusCalls;
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(seconds: 1));
    }
    expect(backend.statusCalls - busyStart, inInclusiveRange(8, 11));
    backend.lastTurn.done('answer');
    await tester.pump();
    await sent;
    c.dispose();
  });

  testWidgets('no status requests while the app is paused', (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    await tester.pump(const Duration(seconds: 1));
    c.handleLifecycle(AppLifecycleState.inactive);
    c.handleLifecycle(AppLifecycleState.hidden);
    c.handleLifecycle(AppLifecycleState.paused);
    final before = backend.statusCalls;
    await tester.pump(const Duration(minutes: 2));
    expect(backend.statusCalls, before);

    c.handleLifecycle(AppLifecycleState.resumed);
    await tester.pump();
    expect(backend.statusCalls, before + 1);
    c.dispose();
  });

  testWidgets('identical polls do not notify the controller', (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    await tester.pump(const Duration(seconds: 1));
    var notifications = 0;
    var statusChanges = 0;
    c.addListener(() => notifications++);
    c.status.addListener(() => statusChanges++);
    for (var i = 0; i < 10; i++) {
      await c.pollStatus();
    }
    expect(notifications, 0);
    // The status notifier gets the fresh value; only the strip listens.
    expect(statusChanges, lessThanOrEqualTo(10));
    c.dispose();
  });

  testWidgets('a status failure is a word, not a green dot', (tester) async {
    final backend = FakeChatBackend()
      ..statusError = SonderException('Server returned HTTP 421.');
    final c = await _start(tester, backend);
    await tester.pump();
    expect(c.connection.value.state, ConnState.refused);
    expect(c.connection.value.sentence, contains('refused this address'));

    backend.statusError =
        SonderException('Cannot reach server: SocketException');
    await c.pollStatus();
    expect(c.connection.value.state, ConnState.unreachable);
    expect(c.connection.value.isOffline, isTrue);

    backend.statusError = null;
    await c.pollStatus();
    expect(c.connection.value.state, ConnState.connected);
    c.dispose();
  });

  testWidgets('streamed deltas build the pending reply, done replaces it',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final sent = c.send('hello');
    await tester.pump();
    final turn = backend.lastTurn;
    turn.phase('routing');
    turn.delta('Hel');
    turn.delta('lo, ');
    await tester.pump();
    expect(c.entries.last.message.pending, isTrue);
    expect(c.entries.last.message.content, 'Hello, ');
    expect(c.live.value?.phase, 'routing');

    turn.done('Hello, world.');
    await tester.pump();
    await sent;
    expect(c.entries.last.message.pending, isFalse);
    expect(c.entries.last.message.content, 'Hello, world.');
    expect(c.sending, isFalse);
    expect(c.live.value, isNull);
    c.dispose();
  });

  testWidgets('send() itself refuses an account line with a password',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final before = c.entries.length;
    await c.send('/login bob hunter2');
    await tester.pump();
    expect(backend.turns, isEmpty);
    expect(c.entries.length, before);
    c.dispose();
  });

  testWidgets('cancelling the first turn rotates the session', (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final thread = c.currentThreadId;
    final first = c.send('start something');
    await tester.pump();
    expect(backend.lastTurn.request.sessionId, thread);
    expect(backend.lastTurn.request.historyMode, isNull);

    final restored = c.cancel();
    await tester.pump();
    await first;
    expect(restored, 'start something');
    expect(backend.turns.first.cancels, 1);
    expect(c.entries, isEmpty);

    final second = c.send('something else');
    await tester.pump();
    final request = backend.lastTurn.request;
    expect(request.sessionId, isNot(thread));
    expect(request.sessionId, '$thread-1');
    expect(request.historyMode, 'client');
    expect(request.history.map((m) => m.content), ['something else']);
    backend.lastTurn.done('ok');
    await tester.pump();
    await second;

    // A later cancel is not a first turn: no second rotation.
    final third = c.send('again');
    await tester.pump();
    c.cancel();
    await tester.pump();
    await third;
    final fourth = c.send('once more');
    await tester.pump();
    expect(backend.lastTurn.request.sessionId, '$thread-1');
    backend.lastTurn.done('ok');
    await tester.pump();
    await fourth;
    c.dispose();
  });

  testWidgets('a transport failure is retryable and Retry resends once',
      (tester) async {
    final backend = FakeChatBackend();
    final c = await _start(tester, backend);
    final sent = c.send('question');
    await tester.pump();
    backend.lastTurn.fail(SonderException('Could not reach http://x.'));
    await tester.pump();
    await sent;
    final error = c.entries.last;
    expect(error.message.error, isTrue);
    expect(error.retryable, isTrue);
    expect(error.elapsedMs, isNotNull);

    final again = c.retry(error.id);
    await tester.pump();
    expect(backend.turns, hasLength(2));
    expect(backend.lastTurn.request.history.last.content, 'question');
    expect(c.entries.where((e) => e.message.role == Role.user), hasLength(1));
    backend.lastTurn.done('fine');
    await tester.pump();
    await again;
    c.dispose();
  });

  testWidgets(
      'a 403 on the mode route makes the chip read-only until the '
      'account changes', (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('manual')
      ..modeWriteError = SonderException(
          "{message: permission mode changes need an administrator, code: FORBIDDEN}");
    final c = await _start(tester, backend);
    await tester.pump();
    final (outcome, text) =
        await c.requestModeChange('plan', confirm: (_, __) async => true);
    expect(outcome, ModeChangeOutcome.readOnly);
    expect(text, isNot(contains('{')));
    expect(c.modeReadOnly, isTrue);

    // Further requests are refused locally, with no POST.
    await c.requestModeChange('manual', confirm: (_, __) async => true);
    expect(backend.modePosts, ['plan']);

    c.updateBackend(
        FakeChatBackend(serverUrl: 'http://other:11435')
          ..mode = permissionModeFor('manual'),
        identityChanged: true);
    expect(c.modeReadOnly, isFalse);
    c.dispose();
  });

  testWidgets('raising asks first; lowering does not', (tester) async {
    final backend = FakeChatBackend()..mode = permissionModeFor('manual');
    final c = await _start(tester, backend);
    await tester.pump();
    final asked = <String>[];
    Future<bool> decline(String from, String to) async {
      asked.add('$from->$to');
      return false;
    }

    var (outcome, _) = await c.requestModeChange('auto', confirm: decline);
    expect(outcome, ModeChangeOutcome.declined);
    expect(backend.modePosts, isEmpty);
    expect(asked, ['manual->auto']);

    (outcome, _) =
        await c.requestModeChange('auto', confirm: (_, __) async => true);
    expect(outcome, ModeChangeOutcome.changed);
    expect(backend.modePosts, ['auto']);

    (outcome, _) = await c.requestModeChange('plan', confirm: decline);
    expect(outcome, ModeChangeOutcome.changed);
    expect(backend.modePosts, ['auto', 'plan']);
    expect(asked, ['manual->auto']);
    c.dispose();
  });
}
