import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/composer.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/execution_route_badge.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/settings.dart';
import 'package:sonder_runtime/settings/execution_mode_choice.dart';
import 'package:sonder_runtime/settings_screen.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';
import 'fixtures/server_fixtures.dart';
import 'goldens/golden_fonts.dart';
import 'settings_connect_test.dart'
    show FakeConnection, pumpSettings, saveSettings;

http.StreamedResponse _jsonStreamed(String text) => http.StreamedResponse(
      Stream.value(utf8.encode(jsonEncode({
        'choices': [
          {
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
          }
        ]
      }))),
      200,
      headers: const {'content-type': 'application/json'},
    );

SystemInfo _context(int used, int limit) => SystemInfo.fromJson({
      'context': {'context_limit': limit, 'estimated_tokens': used},
    });

Future<void> _reply(
  WidgetTester tester,
  FakeChatBackend backend, {
  ChatResponseMetadata? metadata,
}) async {
  await tester.enterText(find.byType(TextField), 'Analyze lock-free ring');
  await tester.testTextInput.receiveAction(TextInputAction.send);
  await tester.pump();
  backend.lastTurn.done('Here is the analysis.', metadata: metadata);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

void main() {
  setUpAll(loadGoldenFonts);

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  group('ExecutionRoute parsing and formatting', () {
    test('parses sonder_receipt.execution_route from non-stream completion',
        () {
      final metadata = chatMetadataFrom({
        'id': 'chatcmpl-881',
        'model': 'sonder',
        'sonder_receipt': {
          'request_id': 'req-881',
          'model': 'sonder:latest',
          'tier': 'reasoning',
          'execution_route': {
            'mode': 'ensemble',
            'band': 'hard',
            'difficulty_score': 0.782,
            'recommended_tiers': ['code', 'reasoning'],
            'ensemble_mode': 'code',
            'fanout_scope': 'local',
            'reason':
                'hard reasoning prompt auto-routed to parallel multi-model ensemble',
            'signals': ['architecture_tradeoff', 'concurrency_memory_systems'],
            'auto_selected': true,
          },
        },
      });

      final route = metadata.executionRoute;
      expect(route, isNotNull);
      expect(route!.mode, 'ensemble');
      expect(route.band, 'hard');
      expect(route.difficultyScore, closeTo(0.782, 1e-6));
      expect(route.recommendedTiers, const ['code', 'reasoning']);
      expect(route.ensembleMode, 'code');
      expect(route.fanoutScope, 'local');
      expect(route.autoSelected, isTrue);
      expect(
        route.reason,
        'hard reasoning prompt auto-routed to parallel multi-model ensemble',
      );
      expect(
        route.signals,
        const ['architecture_tradeoff', 'concurrency_memory_systems'],
      );
      expect(route.compactLabel, 'Auto · Hard · Ensemble (code+reasoning)');
      expect(route.scoreText, '0.782');
      expect(route.tooltipText, contains('Difficulty score: 0.782'));
      expect(route.tooltipText, contains('parallel multi-model ensemble'));
      expect(
        metadata.diagnosticText,
        contains('Auto · Hard · Ensemble (code+reasoning)'),
      );

      // Persistence round-trip preserves execution_route.
      final roundTrip = ChatResponseMetadata.fromJson(metadata.toJson());
      expect(
        roundTrip.executionRoute?.compactLabel,
        'Auto · Hard · Ensemble (code+reasoning)',
      );
      expect(roundTrip.executionRoute?.difficultyScore, closeTo(0.782, 1e-6));
      expect(roundTrip.executionRoute?.autoSelected, isTrue);
      expect(
        roundTrip.executionRoute?.signals,
        const ['architecture_tradeoff', 'concurrency_memory_systems'],
      );

      // withWork preserves executionRoute and ChatMessage.toWire never leaks it.
      final updated = metadata.withWork(workStatus: 'returned');
      expect(updated.executionRoute?.mode, 'ensemble');
      final msg = ChatMessage(
        role: Role.assistant,
        content: 'Done.',
        responseMetadata: metadata,
      );
      expect(msg.toWire().containsKey('response_metadata'), isFalse);
      expect(msg.toWire().containsKey('execution_route'), isFalse);
    });

    test('parses execution_route through ChatStreamAccumulator SSE chunks', () {
      final acc = ChatStreamAccumulator();
      acc.apply({
        'id': 'chatcmpl-sse',
        'model': 'sonder',
        'choices': [
          {
            'delta': {'content': 'Pong!'},
            'finish_reason': null,
          }
        ],
      });
      acc.apply({
        'id': 'chatcmpl-sse',
        'model': 'sonder',
        'choices': [
          {
            'delta': const <String, dynamic>{},
            'finish_reason': 'stop',
          }
        ],
        'sonder_receipt': {
          'execution_route': {
            'mode': 'single',
            'band': 'quick',
            'difficulty_score': 0.05,
            'recommended_tiers': ['fast'],
            'auto_selected': true,
            'reason': 'quick low-complexity prompt routed to fast single tier',
          },
        },
      });
      final reply = acc.reply();
      expect(reply.text, 'Pong!');
      expect(reply.metadata?.executionRoute, isNotNull);
      expect(reply.metadata!.executionRoute!.compactLabel, 'Auto · Quick · fast');
      expect(reply.metadata!.executionRoute!.scoreText, '0.05');
    });

    test('formats labels across all modes and handles malformed input safely',
        () {
      expect(
        const ExecutionRoute(
          mode: 'single',
          band: 'quick',
          recommendedTiers: ['fast'],
          autoSelected: true,
        ).compactLabel,
        'Auto · Quick · fast',
      );
      expect(
        const ExecutionRoute(
          mode: 'single',
          band: 'standard',
          recommendedTiers: ['code'],
          autoSelected: false,
        ).compactLabel,
        'Manual · Standard · Single (code)',
      );
      expect(
        const ExecutionRoute(
          mode: 'fanout',
          band: 'multi_scope',
          recommendedTiers: ['code', 'reasoning'],
          autoSelected: true,
        ).compactLabel,
        'Auto · Multi-scope · Fanout (code+reasoning)',
      );
      expect(
        const ExecutionRoute(
          mode: 'agent',
          band: 'multi_scope',
          autoSelected: true,
        ).compactLabel,
        'Auto · Multi-scope · Agent',
      );

      // Clamping & malformed inputs degrade cleanly.
      final clamped = ExecutionRoute.fromJson({
        'mode': 'SINGLE',
        'band': 'QUICK',
        'difficulty_score': 4.5,
        'recommended_tiers': ['FAST', ''],
        'auto_selected': true,
      });
      expect(clamped?.difficultyScore, 1.0);
      expect(clamped?.scoreText, '1.00');
      expect(clamped?.recommendedTiers, const ['fast']);

      expect(ExecutionRoute.fromJson('not-a-map'), isNull);
      expect(ExecutionRoute.fromJson(const <String, dynamic>{}), isNull);
      expect(
        chatMetadataFrom({
          'sonder_receipt': {'execution_route': 'invalid'}
        }).executionRoute,
        isNull,
      );
      expect(
        const ChatResponseMetadata(
          executionRoute: ExecutionRoute(mode: 'single', band: 'quick'),
        ).isEmpty,
        isFalse,
      );
    });
  });

  group('ExecutionRouteBadge rendering', () {
    testWidgets(
        'renders pill badge above assistant turn with tooltip and details dialog',
        (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend);
      await _reply(
        tester,
        backend,
        metadata: const ChatResponseMetadata(
          model: 'sonder:latest',
          executionRoute: ExecutionRoute(
            mode: 'ensemble',
            band: 'hard',
            difficultyScore: 0.78,
            recommendedTiers: ['code', 'reasoning'],
            ensembleMode: 'code',
            autoSelected: true,
            reason:
                'hard reasoning prompt auto-routed to parallel multi-model ensemble',
            signals: ['concurrency_memory_systems'],
          ),
        ),
      );

      final badge = find.byKey(const Key('execution-route-badge'));
      expect(badge, findsOneWidget);
      expect(
        find.text('Auto · Hard · Ensemble (code+reasoning)'),
        findsOneWidget,
      );

      final tooltip = tester.widget<Tooltip>(
        find.byKey(const Key('execution-route-tooltip')),
      );
      expect(tooltip.message, contains('Difficulty score: 0.78'));
      expect(
        tooltip.message,
        contains(
            'hard reasoning prompt auto-routed to parallel multi-model ensemble'),
      );

      final badgePos = tester.getTopLeft(badge);
      final answerPos =
          tester.getTopLeft(find.textContaining('Here is the analysis.'));
      expect(badgePos.dy, lessThan(answerPos.dy));

      // Tap opens the details dialog with score, reason, and signals.
      await tester.tap(badge);
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('execution-route-details-dialog')),
        findsOneWidget,
      );
      expect(find.text('Auto router'), findsOneWidget);
      expect(find.text('0.78'), findsOneWidget);
      expect(find.text('code, reasoning'), findsOneWidget);
      expect(find.text('concurrency_memory_systems'), findsOneWidget);
      expect(
        find.text(
            'hard reasoning prompt auto-routed to parallel multi-model ensemble'),
        findsOneWidget,
      );
      await tester.tap(find.text('Close'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('execution-route-details-dialog')),
        findsNothing,
      );

      await unmountChat(tester);
    });

    testWidgets('omits badge when turn has no execution_route', (tester) async {
      final backend = FakeChatBackend();
      await pumpChat(tester, backend);
      await _reply(
        tester,
        backend,
        metadata: const ChatResponseMetadata(model: 'sonder:latest'),
      );
      expect(find.byKey(const Key('execution-route-badge')), findsNothing);
      await unmountChat(tester);
    });

    testWidgets('renders cleanly on narrow mobile width in light and dark themes',
        (tester) async {
      for (final theme in [SonderTheme.dark, SonderTheme.light]) {
        tester.view.physicalSize = const Size(320, 480);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.reset);

        await tester.pumpWidget(
          MaterialApp(
            theme: theme,
            home: const Scaffold(
              body: Padding(
                padding: EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    ExecutionRouteBadge(
                      route: ExecutionRoute(
                        mode: 'ensemble',
                        band: 'hard',
                        difficultyScore: 0.84,
                        recommendedTiers: ['code', 'reasoning', 'general'],
                        autoSelected: true,
                        reason: 'complex architecture review',
                      ),
                    ),
                    SizedBox(height: 8),
                    ExecutionRouteBadge(
                      route: ExecutionRoute(
                        mode: 'single',
                        band: 'quick',
                        difficultyScore: 0.08,
                        recommendedTiers: ['fast'],
                        autoSelected: true,
                        reason: 'quick greeting',
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
        await tester.pump();
        expect(tester.takeException(), isNull);
        expect(find.text('Auto · Quick · fast'), findsOneWidget);
      }
    });
  });

  group('Execution mode selector and request serialization', () {
    test('normalizes and parses canonical modes and aliases', () {
      expect(executionModeOptions, const [null, 'single', 'ensemble', 'fanout']);
      expect(normalizeExecutionMode(null), isNull);
      expect(normalizeExecutionMode(''), isNull);
      expect(normalizeExecutionMode('auto'), isNull);
      expect(normalizeExecutionMode('DEFAULT'), isNull);
      expect(normalizeExecutionMode('reset'), isNull);
      expect(normalizeExecutionMode('bogus'), isNull);
      expect(normalizeExecutionMode('single'), 'single');
      expect(normalizeExecutionMode(' ENSEMBLE '), 'ensemble');
      expect(normalizeExecutionMode('fanout'), 'fanout');
      expect(normalizeExecutionMode('fan-out'), 'fanout');
      expect(normalizeExecutionMode('fan_out'), 'fanout');

      expect(parseExecutionModeInput('auto'), (true, null));
      expect(parseExecutionModeInput('single'), (true, 'single'));
      expect(parseExecutionModeInput('ensemble'), (true, 'ensemble'));
      expect(parseExecutionModeInput('fan-out'), (true, 'fanout'));
      expect(parseExecutionModeInput('turbo'), (false, null));
    });

    test(
        'SonderApi omits execution_mode for Auto/null and sends it when non-default',
        () async {
      final streamBodies = <Map<String, dynamic>>[];
      await recordStreamingClients(() async {
        final backend =
            SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435');
        for (final mode in <String?>[
          null,
          'auto',
          'single',
          'ensemble',
          'fanout',
        ]) {
          final turn = backend.startTurn(TurnRequest(
            history: const [ChatMessage(role: Role.user, content: 'hi')],
            model: 'sonder',
            sessionId: 's1',
            executionMode: mode,
          ));
          await turn.events.drain<void>();
        }
      }, (request, body) async {
        streamBodies
            .add(jsonDecode(await body.bytesToString()) as Map<String, dynamic>);
        return _jsonStreamed('ok');
      });

      expect(streamBodies, hasLength(5));
      expect(streamBodies[0].containsKey('execution_mode'), isFalse);
      expect(streamBodies[1].containsKey('execution_mode'), isFalse);
      expect(streamBodies[2]['execution_mode'], 'single');
      expect(streamBodies[3]['execution_mode'], 'ensemble');
      expect(streamBodies[4]['execution_mode'], 'fanout');

      final detailedBodies = <Map<String, dynamic>>[];
      await recordClients(() async {
        final api = SonderApi(baseUrl: 'http://127.0.0.1:11435');
        const hi = [ChatMessage(role: Role.user, content: 'hi')];
        await api.chatDetailed(hi);
        await api.chatDetailed(hi, executionMode: 'auto');
        await api.chatDetailed(hi, executionMode: 'ensemble');
      }, (request) async {
        detailedBodies.add(jsonDecode(request.body) as Map<String, dynamic>);
        return http.Response(
          jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'ok'}
              }
            ]
          }),
          200,
        );
      });

      expect(detailedBodies[0].containsKey('execution_mode'), isFalse);
      expect(detailedBodies[1].containsKey('execution_mode'), isFalse);
      expect(detailedBodies[2]['execution_mode'], 'ensemble');
    });

    test('ChatController passes non-default executionMode and omits Auto',
        () async {
      final backend = FakeChatBackend()..autoReply = 'ok';
      final controller = ChatController(backend, model: 'sonder');
      await controller.loadThreads();

      await controller.send('default auto turn');
      expect(backend.turns.last.request.executionMode, isNull);

      controller.selectExecutionMode('ensemble');
      expect(controller.executionMode, 'ensemble');
      await controller.send('hard architecture question');
      expect(backend.turns.last.request.executionMode, 'ensemble');

      controller.selectExecutionMode('fanout');
      await controller.send('compare across layers');
      expect(backend.turns.last.request.executionMode, 'fanout');

      controller.selectExecutionMode(null);
      expect(controller.executionMode, isNull);
      await controller.send('back to auto');
      expect(backend.turns.last.request.executionMode, isNull);

      controller.dispose();
    });

    test('Settings persists executionMode under sonder_execution_mode',
        () async {
      final s = Settings(executionMode: 'ensemble');
      expect(s.executionMode, 'ensemble');
      await s.save();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('sonder_execution_mode'), 'ensemble');

      final loaded = await Settings.load();
      expect(loaded.executionMode, 'ensemble');

      final cleared = loaded.copyWith(autoExecutionMode: true);
      expect(cleared.executionMode, isNull);
      await cleared.save();
      expect(prefs.containsKey('sonder_execution_mode'), isFalse);
      expect((await Settings.load()).executionMode, isNull);

      SharedPreferences.setMockInitialValues(
        const {'sonder_execution_mode': 'invalid-mode'},
      );
      expect((await Settings.load()).executionMode, isNull);
    });

    testWidgets('ExecutionModeSelector and ExecutionModePickerButton work',
        (tester) async {
      String? picked;
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: StatefulBuilder(
              builder: (context, setState) => Column(
                children: [
                  ExecutionModeSelector(
                    value: picked,
                    onChanged: (v) => setState(() => picked = v),
                  ),
                  ExecutionModePickerButton(
                    value: picked,
                    onSelected: (v) => setState(() => picked = v),
                  ),
                ],
              ),
            ),
          ),
        ),
      );

      expect(find.text('Exec: Auto'), findsOneWidget);

      await tester.tap(find.byKey(const Key('settings-execution-mode-ensemble')));
      await tester.pumpAndSettle();
      expect(picked, 'ensemble');
      expect(find.text('Exec: Ensemble'), findsOneWidget);

      await tester.tap(find.byKey(const Key('composer-execution-mode-picker')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('execution-mode-option-fanout')));
      await tester.pumpAndSettle();
      expect(picked, 'fanout');
      expect(find.text('Exec: Fanout'), findsOneWidget);

      await tester.tap(find.byKey(const Key('composer-execution-mode-picker')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('execution-mode-option-auto')));
      await tester.pumpAndSettle();
      expect(picked, isNull);
      expect(find.text('Exec: Auto'), findsOneWidget);
    });

    testWidgets(
        'ChatScreen + menu, inline override pill, and /exec_mode intercept work end-to-end',
        (tester) async {
      final backend = FakeChatBackend()..autoReply = 'ok';
      final settings =
          await pumpChat(tester, backend, size: const Size(1200, 900));

      // Default Auto keeps the control strip clean (no extra inline pill).
      expect(
        find.byKey(const Key('composer-execution-mode-picker')),
        findsNothing,
      );

      // Select Ensemble via the composer + menu.
      await tester.tap(find.byKey(const Key('composer-actions')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('composer-action-execution-mode')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('execution-mode-dialog')), findsOneWidget);
      await tester.tap(find.byKey(const Key('execution-mode-dialog-ensemble')));
      await tester.pumpAndSettle();

      expect(settings.executionMode, 'ensemble');
      expect(
        find.byKey(const Key('composer-execution-mode-picker')),
        findsOneWidget,
      );
      expect(find.text('Exec: Ensemble'), findsOneWidget);

      // Next chat turn carries executionMode: 'ensemble'.
      await tester.enterText(find.byType(TextField), 'analyze consensus');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(1));
      expect(backend.lastTurn.request.executionMode, 'ensemble');

      // /exec_mode invalid shows error without sending a turn.
      await tester.enterText(find.byType(TextField), '/exec_mode bogus');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(1));
      expect(
        find.textContaining("Unknown execution mode 'bogus'"),
        findsOneWidget,
      );

      // /exec_mode auto resets to default and hides the override pill.
      await tester.enterText(find.byType(TextField), '/exec_mode auto');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(settings.executionMode, isNull);
      expect(
        find.byKey(const Key('composer-execution-mode-picker')),
        findsNothing,
      );

      await tester.enterText(find.byType(TextField), 'hello again');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(backend.turns, hasLength(2));
      expect(backend.lastTurn.request.executionMode, isNull);

      await unmountChat(tester);
    });

    testWidgets(
        'composer strip with active executionMode has zero overflow across widths',
        (tester) async {
      for (final size in const [
        Size(320, 700),
        Size(390, 844),
        Size(500, 800),
        Size(800, 900),
        Size(1200, 900),
        Size(1440, 900),
      ]) {
        final backend = FakeChatBackend()
          ..mode = permissionModeFor('acceptEdits')
          ..models = const ['sonder', 'qwen2.5-coder:7b-instruct-q4_K_M']
          ..statusInfo = _context(2100, 8192);
        await pumpChat(
          tester,
          backend,
          size: size,
          prefs: const {
            'sonder_reasoning_effort': 'high',
            'sonder_persona': 'architect',
            'sonder_execution_mode': 'ensemble',
          },
        );
        await tester.pump(const Duration(milliseconds: 200));
        expect(tester.takeException(), isNull, reason: 'overflow at $size');
        if (size.width >= 500) {
          expect(
            find.byKey(const Key('composer-execution-mode-picker')),
            findsOneWidget,
            reason: 'override pill visible at $size',
          );
        }
        await unmountChat(tester);
      }
    });

    testWidgets('Settings General page stages and saves Execution mode',
        (tester) async {
      await pumpSettings(
        tester,
        connection: FakeConnection(),
        category: SettingsCategory.general,
      );
      final chip = find.byKey(const Key('settings-execution-mode-fanout'));
      await tester.ensureVisible(chip);
      await tester.tap(chip);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('settings-save')), findsOneWidget);
      await saveSettings(tester);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('sonder_execution_mode'), 'fanout');
    });

    test('classifyIntercept and interceptLabel handle /exec_mode', () {
      final bare = classifyIntercept('/exec_mode');
      expect(bare, isA<ExecutionModeIntercept>());
      expect((bare as ExecutionModeIntercept).hasValue, isFalse);

      final setSingle =
          classifyIntercept('/execution_mode single') as ExecutionModeIntercept;
      expect(setSingle.hasValue, isTrue);
      expect(setSingle.target, 'single');

      final setAuto =
          classifyIntercept('/exec_mode auto') as ExecutionModeIntercept;
      expect(setAuto.hasValue, isTrue);
      expect(setAuto.target, isNull);

      expect(interceptLabel('/exec_mode'), 'sets execution mode');
      expect(interceptLabel('/execution_mode ensemble'), 'sets execution mode');
    });
  });
}
