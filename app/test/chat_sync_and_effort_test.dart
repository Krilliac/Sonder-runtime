import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/session_effort.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings/reasoning_effort.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    Settings.testingCredentialStore = null;
  });

  test('normalizeEffort and parseEffortInput handle levels and aliases', () {
    expect(normalizeEffort(null), isNull);
    expect(normalizeEffort('auto'), isNull);
    expect(normalizeEffort('HIGH'), 'high');
    expect(normalizeEffort('none'), 'off');
    expect(normalizeEffort('min'), 'minimal');
    expect(normalizeEffort('med'), 'medium');
    expect(normalizeEffort('max'), 'xhigh');
    expect(normalizeEffort('bogus'), isNull);

    expect(parseEffortInput('high'), (true, 'high'));
    expect(parseEffortInput('auto'), (true, null));
    expect(parseEffortInput('bogus'), (false, null));
  });

  test('classifyIntercept parses /effort commands', () {
    final bare = classifyIntercept('/effort');
    expect(bare, isA<EffortIntercept>());
    expect((bare as EffortIntercept).hasValue, isFalse);
    expect(bare.invalidArg, isNull);

    final setHigh = classifyIntercept('/effort high') as EffortIntercept;
    expect(setHigh.hasValue, isTrue);
    expect(setHigh.target, 'high');

    final setAuto = classifyIntercept('/effort auto') as EffortIntercept;
    expect(setAuto.hasValue, isTrue);
    expect(setAuto.target, isNull);

    final bad = classifyIntercept('/effort ultra-turbo') as EffortIntercept;
    expect(bad.invalidArg, 'ultra-turbo');
  });

  test('SessionEffortSync sends on first turn and on change, omits when confirmed',
      () {
    final sync = SessionEffortSync();
    expect(sync.pending('s1', null), 'auto');
    sync.confirm('s1', 'auto');
    expect(sync.pending('s1', null), isNull);
    expect(sync.pending('s1', 'high'), 'high');
    sync.confirm('s1', 'high');
    expect(sync.pending('s1', 'high'), isNull);
    sync.forget('s1');
    expect(sync.pending('s1', 'high'), 'high');
    sync.confirm('s1', 'high');
    sync.reset();
    expect(sync.pending('s1', 'high'), 'high');
  });

  test('Settings persists reasoningEffort across save and load', () async {
    final s = Settings(reasoningEffort: 'low');
    await s.save();
    final loaded = await Settings.load();
    expect(loaded.reasoningEffort, 'low');

    final cleared = loaded.copyWith(autoReasoningEffort: true);
    await cleared.save();
    final reloaded = await Settings.load();
    expect(reloaded.reasoningEffort, isNull);
  });

  test('ChatController syncs server sessions into sidebar and replays on switch',
      () async {
    final backend = FakeChatBackend()
      ..serverSessions = SessionPage(sessions: [
        SessionSummary(
          id: 'arena-game-dev-20261122',
          title: 'Continue building the arena game',
          turns: 2,
          updatedAt: DateTime.utc(2026, 11, 22, 12, 0),
        ),
      ])
      ..serverReplays['arena-game-dev-20261122'] = const SessionReplay(
        sessionId: 'arena-game-dev-20261122',
        integrityValid: true,
        crashSafe: true,
        transcript: [
          ReplayMessage(
              role: 'user',
              content: 'Continue building the arena game',
              sequence: 1),
          ReplayMessage(
              role: 'assistant',
              content: 'Added the TAB scoreboard overlay.',
              sequence: 2),
        ],
      );

    final controller = ChatController(backend, model: 'sonder');
    await controller.loadThreads();
    await controller.syncServerSessions(force: true);

    final synced = controller.threads
        .where((t) => t.id == 'arena-game-dev-20261122')
        .firstOrNull;
    expect(synced, isNotNull);
    expect(synced!.title, 'Continue building the arena game');

    controller.switchThread(synced);
    await controller.syncThreadFromServer(synced.id);

    expect(controller.currentThreadId, 'arena-game-dev-20261122');
    expect(controller.messages.length, 2);
    expect(controller.messages.first.content, 'Continue building the arena game');
    expect(controller.messages.last.content, 'Added the TAB scoreboard overlay.');
    controller.dispose();
  });

  test('ChatController sends sessionReasoningEffort when selected', () async {
    final backend = FakeChatBackend()..autoReply = 'ok';
    final controller = ChatController(backend, model: 'sonder');
    await controller.loadThreads();

    controller.selectReasoningEffort('high');
    await controller.send('explain this');
    expect(backend.turns.last.request.sessionReasoningEffort, 'high');

    // Second turn with unchanged effort omits the field.
    await controller.send('follow up');
    expect(backend.turns.last.request.sessionReasoningEffort, isNull);

    // Changing to off resends.
    controller.selectReasoningEffort('off');
    await controller.send('quick fix');
    expect(backend.turns.last.request.sessionReasoningEffort, 'off');
    controller.dispose();
  });

  testWidgets('EffortSelector and EffortPickerButton update selection',
      (tester) async {
    String? picked = 'low';
    await tester.pumpWidget(
      MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: StatefulBuilder(
            builder: (context, setState) => Column(
              children: [
                EffortSelector(
                  value: picked,
                  onChanged: (v) => setState(() => picked = v),
                ),
                EffortPickerButton(
                  value: picked,
                  onSelected: (v) => setState(() => picked = v),
                ),
              ],
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.byKey(const Key('settings-effort-high')));
    await tester.pumpAndSettle();
    expect(picked, 'high');

    await tester.tap(find.byKey(const Key('composer-effort-picker')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('effort-option-off')));
    await tester.pumpAndSettle();
    expect(picked, 'off');
  });
}
