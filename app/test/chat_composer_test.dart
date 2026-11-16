// The composer's control strip: model picker, context ring, keyboard and
// Send/Stop (#8, #10).
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/model_picker.dart';
import 'package:sonder_runtime/runtime/model_routing.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/workspace_ui.dart' show conversationWidth;

import 'chat_fakes.dart';
import 'goldens/golden_fonts.dart';
import 'runtime_fixtures.dart';

SystemInfo _context(int used, int limit) => SystemInfo.fromJson({
      'context': {'context_limit': limit, 'estimated_tokens': used},
    });

Finder get _picker => find.byKey(const Key('model-picker'));
Finder get _panel => find.byKey(const Key('model-picker-panel'));

Future<void> _settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 500));
}

void main() {
  setUpAll(loadGoldenFonts);

  group('model picker', () {
    testWidgets('groups routes and local models, and search narrows both',
        (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'general', 'llama3:8b', 'gemma3:12b'];
      await pumpChat(tester, backend);
      await tester.tap(_picker);
      await _settle(tester);
      expect(find.byKey(const Key('model-group-routes')), findsOneWidget);
      expect(find.byKey(const Key('model-group-local')), findsOneWidget);
      expect(find.byKey(const Key('model-group-ollama-direct')), findsNothing);

      await tester.enterText(
          find.byKey(const Key('model-picker-search')), 'LLAMA');
      await tester.pump();
      expect(find.byKey(const Key('model-option-llama3:8b')), findsOneWidget);
      expect(find.byKey(const Key('model-option-gemma3:12b')), findsNothing);
      expect(find.byKey(const Key('model-group-routes')), findsNothing);

      await tester.enterText(
          find.byKey(const Key('model-picker-search')), 'mistral');
      await tester.pump();
      expect(find.text('No model matches "mistral".'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('arrow keys and Enter pick; Escape closes without a change',
        (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'general', 'llama3:8b'];
      final settings = await pumpChat(tester, backend);
      await tester.tap(_picker);
      await _settle(tester);
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await _settle(tester);
      expect(_panel, findsNothing);
      expect(settings.model, 'sonder');

      await tester.tap(_picker);
      await _settle(tester);
      // The highlight starts on the current model; down twice is llama.
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await _settle(tester);
      expect(_panel, findsNothing);
      expect(settings.model, 'llama3:8b');
      expect(find.descendant(of: _picker, matching: find.text('llama3:8b')),
          findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('on a phone it is a bottom sheet', (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'general', 'llama3:8b'];
      final settings =
          await pumpChat(tester, backend, size: const Size(390, 844));
      await tester.tap(_picker);
      await _settle(tester);
      expect(find.byType(BottomSheet), findsOneWidget);
      await tester.tap(find.byKey(const Key('model-option-general')));
      await _settle(tester);
      expect(find.byType(BottomSheet), findsNothing);
      expect(settings.model, 'general');
      await unmountChat(tester);
    });

    test('the trigger names what will answer; rows keep the binding', () {
      final routing = ModelRouting(EcosystemReading.parse(ecosystemJson(
              inference: inferenceStatusJson()..['models'] = ['qwen3:14b']))
          .status);
      expect(compactModelLabel('sonder', routing), 'sonder · qwen3:14b');
      expect(routing.pickerLabel('sonder'),
          'sonder · Sonder Inference (qwen3:14b)');
      expect(compactModelLabel('llama3:8b', routing),
          'llama3:8b · Ollama (direct)');
      const plain = ModelRouting();
      expect(compactModelLabel('sonder', plain), 'sonder (local route)');
      expect(compactModelLabel('sonder', plain, dense: true), 'sonder');
      final groups =
          groupModels(const ['sonder', 'fast', 'llama3:8b'], routing);
      expect(groups.map((g) => g.kind),
          [ModelGroupKind.routes, ModelGroupKind.direct]);
    });
  });

  group('context ring', () {
    testWidgets('states the numbers and the percentage', (tester) async {
      final backend = FakeChatBackend()..statusInfo = _context(2100, 8192);
      await pumpChat(tester, backend, size: const Size(800, 900));
      await _settle(tester);
      final ring =
          tester.widget<Tooltip>(find.byKey(const Key('context-ring')));
      expect(ring.message, 'Context: 2,100 of 8,192 tokens used (26%)');
      expect(find.byKey(const Key('context-ring-percent')), findsNothing,
          reason: 'below 1000 px the composer is not the desktop layout');
      await unmountChat(tester);

      await pumpChat(tester, backend, size: const Size(1440, 900));
      await _settle(tester);
      expect(find.text('26%'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('turns danger near the limit; colour is never alone',
        (tester) async {
      final backend = FakeChatBackend()..statusInfo = _context(7700, 8192);
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await _settle(tester);
      final percent =
          tester.widget<Text>(find.byKey(const Key('context-ring-percent')));
      expect(percent.data, '94%');
      expect(percent.style?.color, SonderTokens.dark.danger);
      await unmountChat(tester);
    });

    testWidgets('is absent when the server reports no context', (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend);
      await _settle(tester);
      expect(find.byKey(const Key('context-ring')), findsNothing);
      await unmountChat(tester);
    });

    test('words for the ring', () {
      expect(contextUsageText(null), isNull);
      expect(contextUsageText(_context(0, 0).context), isNull);
      expect(contextUsageText(_context(123456, 131072).context),
          'Context: 123,456 of 131,072 tokens used (94%)');
    });
  });

  group('keyboard and Send', () {
    testWidgets('Enter sends, Shift+Enter adds a line', (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await tester.tap(find.byType(TextField));
      await tester.enterText(find.byType(TextField), 'first line');
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.pump();
      expect(tester.widget<TextField>(find.byType(TextField)).controller!.text,
          'first line\n');
      expect(backend.turns, isEmpty);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      expect(backend.turns, hasLength(1));
      expect(backend.lastTurn.request.history.last.content, 'first line');
      backend.lastTurn.done('ok');
      await _settle(tester);
      await unmountChat(tester);
    });

    testWidgets('Send waits for text, then becomes Stop during a turn',
        (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend);
      IconButton send() =>
          tester.widget<IconButton>(find.byKey(const Key('composer-send')));
      expect(send().onPressed, isNull);
      expect(send().tooltip, 'Send');
      await tester.enterText(find.byType(TextField), 'hello');
      await tester.pump();
      expect(send().onPressed, isNotNull);
      await tester.tap(find.byKey(const Key('composer-send')));
      await tester.pump();
      expect(backend.turns, hasLength(1));
      expect(send().tooltip, 'Stop');
      await tester.tap(find.byKey(const Key('composer-send')));
      await tester.pump();
      expect(backend.lastTurn.cancels, 1);
      await unmountChat(tester);
    });

    testWidgets('the slash palette opens the full command browser',
        (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend);
      await tester.enterText(find.byType(TextField), '/');
      await _settle(tester);
      expect(find.byKey(const Key('command-palette')), findsOneWidget);
      await tester.tap(find.byKey(const Key('command-palette-browse')));
      await _settle(tester);
      expect(find.byKey(const Key('command-browser')), findsOneWidget);
      await unmountChat(tester);
    });
  });

  group('frame', () {
    testWidgets(
        'one rounded, lifted card in the reading column holds the field '
        'and every control', (tester) async {
      for (final mode in [ThemeMode.dark, ThemeMode.light]) {
        final backend = FakeChatBackend()
          ..mode = permissionModeFor('manual')
          ..models = const ['sonder', 'general']
          ..statusInfo = _context(2100, 8192);
        await pumpChat(tester, backend,
            size: const Size(1440, 900), themeMode: mode);
        await _settle(tester);
        final tokens =
            mode == ThemeMode.dark ? SonderTokens.dark : SonderTokens.light;
        final frame = find.byKey(const Key('composer-surface'));
        BoxDecoration decoration() =>
            tester.widget<AnimatedContainer>(frame).decoration!
                as BoxDecoration;
        expect(decoration().color, tokens.panel, reason: '$mode');
        expect(decoration().borderRadius,
            BorderRadius.circular(SonderRadius.composer));
        expect(tokens.lift, isNotEmpty);
        expect(decoration().boxShadow, tokens.lift);

        // The same centred column as the conversation above it.
        final rect = tester.getRect(frame);
        expect(rect.width, conversationWidth);
        expect(rect.center.dx, 1440 / 2);
        // The field and the whole bar are inside the frame.
        expect(rect.contains(tester.getCenter(find.byType(TextField))), isTrue);
        Rect control(String key) => tester.getRect(find.byKey(Key(key)));
        const bar = [
          'composer-actions',
          'permission-mode-chip',
          'model-picker',
          'context-ring',
          'composer-send',
        ];
        for (final key in bar) {
          expect(rect.contains(control(key).center), isTrue, reason: key);
        }
        // In reading order: more actions, mode, model, context; Send last.
        for (var i = 1; i < bar.length; i++) {
          expect(control(bar[i]).left, greaterThan(control(bar[i - 1]).left),
              reason: '${bar[i]} follows ${bar[i - 1]}');
        }
        expect(tester.getSize(find.byKey(const Key('composer-send'))),
            const Size(48, 48));

        // Its edge darkens while the box has focus, and only then.
        final field = tester.widget<TextField>(find.byType(TextField));
        field.focusNode!.unfocus();
        await _settle(tester);
        expect(decoration().border, Border.all(color: tokens.hairline));
        field.focusNode!.requestFocus();
        await _settle(tester);
        expect(decoration().border, Border.all(color: tokens.hairlineStrong));
        await unmountChat(tester);
      }
    });

    testWidgets('the field grows with its text, up to eight lines',
        (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await _settle(tester);
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.minLines, 1);
      expect(field.maxLines, 8);
      final frame = find.byKey(const Key('composer-surface'));
      final one = tester.getSize(frame).height;
      await tester.enterText(find.byType(TextField), 'a\nb\nc');
      await _settle(tester);
      final three = tester.getSize(frame).height;
      expect(three, greaterThan(one));
      await tester.enterText(
          find.byType(TextField), List.filled(30, 'line').join('\n'));
      await _settle(tester);
      // Eight 26 px lines at most; the rest scrolls inside the field.
      expect(tester.getSize(find.byType(TextField)).height,
          lessThanOrEqualTo(8 * 26 + SonderSpace.lg + SonderSpace.xs));
      expect(tester.takeException(), isNull);
      await unmountChat(tester);
    });

    testWidgets(
        'the + menu holds what the bar has no room for, and rises above '
        'its button', (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await _settle(tester);
      final button = find.byKey(const Key('composer-actions'));
      expect(find.byTooltip('More actions'), findsOneWidget);
      await tester.tap(button);
      await _settle(tester);
      const labels = {
        'commands': 'Browse commands',
        'project': 'Set project',
        'server-conversations': 'Server conversations',
      };
      for (final e in labels.entries) {
        final item = find.byKey(Key('composer-action-${e.key}'));
        expect(find.descendant(of: item, matching: find.text(e.value)),
            findsOneWidget);
        // Clear of the words being written.
        expect(tester.getRect(item).bottom,
            lessThanOrEqualTo(tester.getRect(button).top + 1),
            reason: e.key);
      }
      await tester.tap(find.byKey(const Key('composer-action-commands')));
      await _settle(tester);
      expect(find.byKey(const Key('command-browser')), findsOneWidget);
      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await _settle(tester);
      expect(find.byKey(const Key('command-browser')), findsNothing);

      await tester.tap(button);
      await _settle(tester);
      await tester.tap(find.byKey(const Key('composer-action-project')));
      await _settle(tester);
      expect(find.text('Project name'), findsOneWidget);
      await tester.tap(find.text('Cancel'));
      await _settle(tester);
      await unmountChat(tester);
    });

    testWidgets(
        'the + menu opens on the medium step, and at once with reduced '
        'motion', (tester) async {
      addTearDown(
          tester.platformDispatcher.clearAccessibilityFeaturesTestValue);
      for (final reduce in [false, true]) {
        tester.platformDispatcher.accessibilityFeaturesTestValue =
            FakeAccessibilityFeatures(disableAnimations: reduce);
        final backend = FakeChatBackend();
        await pumpChat(tester, backend, size: const Size(1440, 900));
        await _settle(tester);
        await tester.tap(find.byKey(const Key('composer-actions')));
        await _settle(tester);
        final item = find.byKey(const Key('composer-action-commands'));
        // Material's own menu takes 300 ms to open.
        expect(ModalRoute.of(tester.element(item))!.transitionDuration,
            reduce ? Duration.zero : SonderMotion.medium,
            reason: 'reduced motion: $reduce');
        await tester.sendKeyEvent(LogicalKeyboardKey.escape);
        await _settle(tester);
        expect(item, findsNothing);
        await unmountChat(tester);
      }
    });
  });

  testWidgets('a 320 px strip keeps the mode word and does not overflow',
      (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('acceptEdits')
      ..models = const ['sonder', 'qwen2.5-coder:7b-instruct-q4_K_M']
      ..statusInfo = _context(2100, 8192);
    await pumpChat(tester, backend, size: const Size(320, 700));
    await _settle(tester);
    expect(tester.takeException(), isNull);
    final chip = find.byKey(const Key('permission-mode-chip'));
    expect(chip, findsOneWidget);
    expect(find.byKey(const Key('context-ring')), findsNothing);
    expect(
        tester.getRect(chip).right, lessThan(tester.getRect(_picker).left + 1));
    await unmountChat(tester);
  });
}
