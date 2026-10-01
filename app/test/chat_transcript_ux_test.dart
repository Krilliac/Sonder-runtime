// Transcript affordances (#8): actions confirm visibly, Retry belongs to
// the newest answer, new messages enter once, tool calls summarise, and the
// whole screen keeps 48 dp labelled targets on a phone.
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/transcript.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';
import 'goldens/golden_fonts.dart';

const _answer = 'The driver compiles the PSO on first bind.\n\n'
    '=== ACTIVITY (observable work) ===\n'
    '• file_read src/render/pso_cache.cpp\n'
    '• text_search CreateGraphicsPipelineState\n'
    '× file_write src/render/pso_cache.cpp\n'
    '+120ms tool_call file_read\n'
    '+1840ms tool_call text_search\n'
    '=== END ACTIVITY ===';

const _meta = ChatResponseMetadata(
  requestId: 'req_1',
  elapsedMs: 61200,
  modelCalls: 2,
  promptTokens: 2600,
  completionTokens: 143,
  model: 'sonder:latest',
  tier: 'code',
);

Map<String, Object> _seed(List<ChatMessage> messages) {
  final at = DateTime(2026, 9, 30, 12);
  return {
    'sonder_chat_v2/thread-a.json': jsonEncode({
      'id': 'a',
      'title': 'PSO',
      'project': 'engine',
      'created_at': at.toIso8601String(),
      'updated_at': at.toIso8601String(),
      'messages': messages.map((m) => m.toJson()).toList(),
    }),
    'sonder_chat_v2/migrated-v1': 'yes',
  };
}

final _twoTurns = <ChatMessage>[
  const ChatMessage(role: Role.user, content: 'Why the stall?'),
  const ChatMessage(
      role: Role.assistant, content: _answer, responseMetadata: _meta),
  const ChatMessage(role: Role.user, content: 'And the fix?'),
  const ChatMessage(
      role: Role.assistant,
      content: 'Warm the cache at load.',
      responseMetadata: _meta),
];

List<String> _recordClipboard(WidgetTester tester) {
  final copied = <String>[];
  tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
    SystemChannels.platform,
    (call) async {
      if (call.method == 'Clipboard.setData') {
        copied.add((call.arguments as Map)['text'] as String);
      }
      return null;
    },
  );
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, null));
  return copied;
}

Future<void> _settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 500));
}

void main() {
  setUpAll(loadGoldenFonts);

  testWidgets(
      'Copy, Useful and Edited confirm with a toast and a marked '
      'state, and are sent once', (tester) async {
    final copied = _recordClipboard(tester);
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: _seed(_twoTurns));
    await _settle(tester);

    final copy = find.byKey(const Key('answer-copy')).last;
    await tester.tap(copy);
    await _settle(tester);
    expect(copied, ['Warm the cache at load.']);
    expect(backend.feedback, ['/copied']);
    expect(find.text('Response copied'), findsOneWidget);
    expect(find.descendant(of: copy, matching: find.text('Copied')),
        findsOneWidget);

    final useful = find.byKey(const Key('answer-useful')).last;
    await tester.tap(useful);
    await _settle(tester);
    expect(backend.feedback, ['/copied', '/accept']);
    expect(find.text('Marked useful'), findsOneWidget);
    final semantics = tester.ensureSemantics();
    expect(
        tester.getSemantics(useful),
        matchesSemantics(
            label: 'Marked useful',
            isButton: true,
            isSelected: true,
            hasSelectedState: true,
            hasEnabledState: true));
    semantics.dispose();
    // Marked: a second tap sends nothing.
    await tester.tap(useful);
    await _settle(tester);
    expect(backend.feedback, ['/copied', '/accept']);

    await tester.tap(find.byKey(const Key('answer-edited')).last);
    await _settle(tester);
    expect(backend.feedback, ['/copied', '/accept', '/edited']);
    expect(find.text('Marked as edited'), findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('Retry is offered on the newest answer only and asks again',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: _seed(_twoTurns));
    await _settle(tester);
    expect(find.byKey(const Key('answer-retry')), findsOneWidget);
    await tester.tap(find.byKey(const Key('answer-retry')));
    await tester.pump();
    expect(backend.turns, hasLength(1));
    expect(backend.lastTurn.request.history.last.content, 'And the fix?');
    // The replaced answer is gone while the new one runs.
    expect(
        find.text('Warm the cache at load.', findRichText: true), findsNothing);
    backend.lastTurn.done('Warm it at load, then verify.');
    await _settle(tester);
    expect(find.byKey(const Key('answer-retry')), findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('new messages fade in once; history and replays never do',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: _seed(_twoTurns));
    await _settle(tester);
    double opacityOf(String text) => tester
        .widget<Opacity>(find
            .ancestor(
                of: find.text(text, findRichText: true),
                matching: find.byType(Opacity))
            .first)
        .opacity;
    // Loaded history is simply there.
    expect(opacityOf('Why the stall?'), 1);

    await tester.enterText(find.byType(TextField), 'Profile it');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(opacityOf('Profile it'), lessThan(1));
    await tester.pump(const Duration(milliseconds: 400));
    expect(opacityOf('Profile it'), 1);

    // The reply replaces the pending row in place: no second entrance.
    backend.lastTurn.done('Done profiling.');
    await tester.pump();
    await tester.pump();
    expect(opacityOf('Done profiling.'), 1);
    await unmountChat(tester);
  });

  testWidgets('with reduced motion new messages appear at once',
      (tester) async {
    tester.platformDispatcher.accessibilityFeaturesTestValue =
        const FakeAccessibilityFeatures(disableAnimations: true);
    addTearDown(tester.platformDispatcher.clearAccessibilityFeaturesTestValue);
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: _seed(_twoTurns));
    await _settle(tester);
    await tester.enterText(find.byType(TextField), 'Profile it');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    final opacity = tester
        .widget<Opacity>(find
            .ancestor(
                of: find.text('Profile it', findRichText: true),
                matching: find.byType(Opacity))
            .first)
        .opacity;
    expect(opacity, 1);
    backend.lastTurn.done('ok');
    await tester.pump();
    await unmountChat(tester);
  });

  testWidgets('tool calls are one summary line that opens to the calls',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: _seed(_twoTurns));
    await _settle(tester);
    expect(
        toolCallsSummary(const [
          ToolCallRow('a', elapsedMs: 120),
          ToolCallRow('b', elapsedMs: 1840),
          ToolCallRow('c', ok: false),
        ]),
        'ran 2 tools · 2.0s · 1 refused');
    final chip = find.byKey(const Key('detail-tools'));
    expect(
        find.descendant(
            of: chip,
            matching: find.textContaining('ran 2 tools · 2.0s · ',
                findRichText: true)),
        findsOneWidget);
    expect(
        find.descendant(
            of: chip,
            matching: find.textContaining('⊘ 1 refused', findRichText: true)),
        findsOneWidget);
    expect(find.text('text_search CreateGraphicsPipelineState'), findsNothing);
    await tester.tap(chip);
    await _settle(tester);
    expect(
        find.text('text_search CreateGraphicsPipelineState'), findsOneWidget);
    expect(find.text('1.8s'), findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets(
      'on a phone the details fold into one disclosure and the '
      'footer stays one line', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend,
        size: const Size(390, 844), prefs: _seed(_twoTurns));
    await _settle(tester);
    expect(find.byKey(const Key('detail-all')), findsOneWidget);
    expect(find.byKey(const Key('detail-tools')), findsNothing);
    final footer =
        tester.widget<Text>(find.byKey(const Key('answer-footer-text')).first);
    expect(footer.maxLines, 1);
    expect(footer.data, startsWith('done 61.2s'));
    await tester.tap(find.byKey(const Key('detail-all')));
    await _settle(tester);
    expect(find.text('Tool calls'), findsOneWidget);
    // The panel's title, and the newest answer's own (single) disclosure.
    expect(find.text('Response details'), findsNWidgets(2));
    await unmountChat(tester);
  });

  testWidgets('with a mouse, older answers show their actions on hover',
      (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.windows;
    final backend = FakeChatBackend();
    await pumpChat(tester, backend,
        size: const Size(1440, 900), prefs: _seed(_twoTurns));
    await _settle(tester);
    double opacityOfActions(int index) => tester
        .widget<AnimatedOpacity>(find
            .ancestor(
                of: find.byKey(const Key('answer-useful')).at(index),
                matching: find.byType(AnimatedOpacity))
            .first)
        .opacity;
    expect(opacityOfActions(0), 0, reason: 'an older answer, not hovered');
    expect(opacityOfActions(1), 1, reason: 'the newest answer');
    final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await mouse.addPointer(location: Offset.zero);
    await mouse.moveTo(tester.getCenter(find.text(
        'The driver compiles the PSO on first bind.',
        findRichText: true)));
    await _settle(tester);
    expect(opacityOfActions(0), 1);
    await mouse.removePointer();
    await unmountChat(tester);
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('a phone conversation keeps 48 dp labelled targets',
      (tester) async {
    final semantics = tester.ensureSemantics();
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('manual')
      ..models = const ['sonder', 'general', 'llama3:8b']
      ..statusInfo = SystemInfo.fromJson({
        'context': {'context_limit': 8192, 'estimated_tokens': 2100},
      });
    await pumpChat(tester, backend,
        size: const Size(390, 844), prefs: _seed(_twoTurns));
    await _settle(tester);
    // The composer, the turns' actions and the details disclosure.
    expect(find.byKey(const Key('model-picker')), findsOneWidget);
    expect(find.byKey(const Key('detail-all')), findsWidgets);
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    semantics.dispose();
    await unmountChat(tester);
  });
}
