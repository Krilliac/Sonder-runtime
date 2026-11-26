import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/live_line.dart';
import 'package:sonder_runtime/chat/streaming_error_banner.dart';
import 'package:sonder_runtime/chat/streaming_reasoning_panel.dart';
import 'package:sonder_runtime/chat/tool_run_card.dart';
import 'package:sonder_runtime/chat/transcript.dart';
import 'package:sonder_runtime/chat/turn_progress.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/workspace_ui.dart' show StatusKind;

import 'goldens/golden_fonts.dart';

TranscriptActions _stubActions({
  VoidCallback? onStop,
  ValueChanged<int>? onRetry,
}) =>
    TranscriptActions(
      onStop: onStop ?? () {},
      onFeedback: (_) {},
      onRetry: onRetry ?? (_) {},
      onChangeMode: () {},
      onApprove: (_, __, ___, ____) async =>
          const ApprovalOutcome(ApprovalStatus.approved),
      onDeny: (_, __, ___) async =>
          const ApprovalOutcome(ApprovalStatus.approved),
      onApproveClient: (_, __, ___) async =>
          const ApprovalOutcome(ApprovalStatus.approved),
      onDenyClient: (_, __, ___) async =>
          const ApprovalOutcome(ApprovalStatus.approved),
      fetchWorkRun: (_) async => const WorkRun(id: 'wr-1', status: 'running'),
      cancelWorkRun: (_) async => const WorkRun(id: 'wr-1', status: 'cancelled'),
      listWorkRuns: () async => const [],
      onWorkRunResolved: (_, __) {},
    );

void main() {
  setUpAll(loadGoldenFonts);

  group('StreamingReasoningPanel', () {
    testWidgets(
        'expands while thinking, auto-collapses when answer starts, and '
        'respects manual toggles', (tester) async {
      var reasoning = 'Step 1: inspect the SSE state machine and check timers.';
      var thinkingActive = true;
      late StateSetter rebuild;

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: StatefulBuilder(
              builder: (context, setState) {
                rebuild = setState;
                return StreamingReasoningPanel(
                  reasoning: reasoning,
                  thinkingActive: thinkingActive,
                );
              },
            ),
          ),
        ),
      );
      await tester.pump();

      expect(find.byKey(const Key('streaming-reasoning')), findsOneWidget);
      expect(find.text('Thinking…'), findsOneWidget);
      expect(find.byKey(const Key('streaming-reasoning-body')), findsOneWidget);
      expect(find.textContaining('Step 1: inspect'), findsOneWidget);
      expect(find.textContaining('tok'), findsOneWidget);

      // Once answer tokens begin, the panel auto-folds unless user toggled it.
      rebuild(() => thinkingActive = false);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('Reasoning'), findsOneWidget);
      expect(find.byKey(const Key('streaming-reasoning-body')), findsNothing);

      // User can re-expand manually after answer starts.
      await tester.tap(find.byKey(const Key('streaming-reasoning-toggle')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byKey(const Key('streaming-reasoning-body')), findsOneWidget);
      expect(find.textContaining('Step 1: inspect'), findsOneWidget);

      // Manual collapse stays collapsed even when more reasoning arrives.
      await tester.tap(find.byKey(const Key('streaming-reasoning-toggle')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byKey(const Key('streaming-reasoning-body')), findsNothing);

      rebuild(() {
        thinkingActive = true;
        reasoning = '$reasoning\nStep 2: verify reconnect path.';
      });
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byKey(const Key('streaming-reasoning-body')), findsNothing);
    });

    testWidgets(
        'keeps 48dp accessible tap target and zero overflow at 320px 2x scale',
        (tester) async {
      final semantics = tester.ensureSemantics();
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1.0;
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
        tester.platformDispatcher.clearTextScaleFactorTestValue();
      });

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: Padding(
              padding: const EdgeInsets.all(SonderSpace.sm),
              child: StreamingReasoningPanel(
                reasoning: List.filled(40, 'Checking edge case in SSE stream.').join('\n'),
                thinkingActive: true,
              ),
            ),
          ),
        ),
      );
      await tester.pump();

      expect(tester.takeException(), isNull);
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      semantics.dispose();
    });
  });

  group('LiveLineView metrics, stuck-spinner fix, and mobile layout', () {
    testWidgets(
        'renders TTFT and live tok/s badge once output streams, hidden before '
        'first token', (tester) async {
      var now = DateTime(2026, 11, 25, 1, 0, 0);
      final live = ValueNotifier<LiveTurn?>(
        LiveTurn(startedAt: now, phase: 'thinking', model: 'bonsai-9b'),
      );
      addTearDown(live.dispose);

      var outputLength = 0;
      late StateSetter rebuild;
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: StatefulBuilder(
              builder: (context, setState) {
                rebuild = setState;
                return LiveLineView(
                  live: live,
                  onStop: () {},
                  outputLength: outputLength,
                  clock: () => now,
                );
              },
            ),
          ),
        ),
      );

      // Before first token: no metrics badge (keeps pre-token layout clean).
      expect(find.byKey(const Key('live-metrics-badge')), findsNothing);

      // First token arrives at 420ms -> TTFT badge shows 420ms, rate warms up.
      now = now.add(const Duration(milliseconds: 420));
      rebuild(() => outputLength = 8);
      await tester.pump();
      expect(find.byKey(const Key('live-metrics-badge')), findsOneWidget);
      expect(find.byKey(const Key('live-ttft-badge')), findsOneWidget);
      expect(find.text('TTFT 420ms'), findsOneWidget);
      expect(find.byKey(const Key('live-tps-badge')), findsNothing);

      // After 1000ms of generation with 160 chars (~40 tokens) -> ~40.0 tok/s.
      now = now.add(const Duration(milliseconds: 1000));
      live.value = live.value!.copyWith(phase: 'writing', elapsedSeconds: 1);
      rebuild(() => outputLength = 160);
      await tester.pump();
      expect(find.text('TTFT 420ms'), findsOneWidget);
      expect(find.byKey(const Key('live-tps-badge')), findsOneWidget);
      expect(find.text('~40.0 tok/s'), findsOneWidget);
    });

    testWidgets(
        'clears spinner immediately when live turn becomes null (aborted SSE)',
        (tester) async {
      final live = ValueNotifier<LiveTurn?>(
        LiveTurn(startedAt: DateTime(2026, 11, 25, 1, 0), model: 'sonder'),
      );
      addTearDown(live.dispose);

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: LiveLineView(live: live, onStop: () {}, outputLength: 24),
          ),
        ),
      );
      expect(find.byKey(const Key('live-line')), findsOneWidget);
      expect(find.byKey(const Key('live-glyph')), findsOneWidget);
      expect(find.byKey(const Key('live-stop')), findsOneWidget);

      // SSE aborts and clears live notifier -> spinner must disappear cleanly.
      live.value = null;
      await tester.pump();
      expect(find.byKey(const Key('live-line')), findsNothing);
      expect(find.byKey(const Key('live-glyph')), findsNothing);
      expect(find.byKey(const Key('live-stop')), findsNothing);
      expect(find.byKey(const Key('live-line-idle')), findsOneWidget);
    });

    testWidgets(
        'live tool updates reset stall clock and awaiting_client_approval '
        'never triggers stall hint', (tester) async {
      final start = DateTime(2026, 11, 25, 1, 0);
      final live = ValueNotifier<LiveTurn?>(
        LiveTurn(startedAt: start, phase: 'thinking', model: 'sonder'),
      );
      addTearDown(live.dispose);

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: LiveLineView(live: live, onStop: () {}, outputLength: 0),
          ),
        ),
      );

      for (var s = 1; s <= 15; s++) {
        live.value = live.value!.copyWith(elapsedSeconds: s);
      }
      // Tool call arrives on tick 16 -> resets stall clock.
      live.value = live.value!.copyWith(
        elapsedSeconds: 16,
        toolCalls: const [
          ChatStreamToolEvent(
            id: 'c1',
            callId: '1111111122222222',
            tool: 'file_read',
            executionTarget: 'client',
            state: 'awaiting_client_approval',
          ),
        ],
      );
      for (var s = 17; s <= 40; s++) {
        live.value = live.value!.copyWith(elapsedSeconds: s);
      }
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.byKey(const Key('live-stall')), findsNothing);
    });

    testWidgets('lays out without overflow at 320px width and 2x text scale',
        (tester) async {
      var now = DateTime(2026, 11, 25, 1, 0, 0);
      final live = ValueNotifier<LiveTurn?>(
        LiveTurn(
          startedAt: now,
          phase: 'writing',
          model: 'qwen3-coder-30b-a3b-instruct-q4_K_M',
          elapsedSeconds: 25,
        ),
      );
      addTearDown(live.dispose);
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1.0;
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
        tester.platformDispatcher.clearTextScaleFactorTestValue();
      });

      now = now.add(const Duration(seconds: 2));
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: Padding(
              padding: const EdgeInsets.all(SonderSpace.sm),
              child: LiveLineView(
                live: live,
                onStop: () {},
                outputLength: 200,
                clock: () => now.add(const Duration(seconds: 3)),
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('live-metrics-badge')), findsOneWidget);
    });
  });

  group('ToolRunCard live deltas, settled/aborted states, and narrow layout',
      () {
    testWidgets(
        'shows live delta indicator while running and settles cleanly on '
        'completed or failed turns', (tester) async {
      const runningTool = ChatStreamToolEvent(
        id: 'call_live_1',
        callId: 'a1b2c3d4e5f60708',
        tool: 'get_weather',
        phase: 'running',
        state: 'running',
        detail: '{"city":"Tokyo","units":"metric"}',
        argumentsReported: true,
      );

      // 1. In-flight: shows running + streaming delta badge.
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: const Scaffold(
            body: ToolRunCard(event: runningTool),
          ),
        ),
      );
      expect(find.textContaining('running', findRichText: true), findsOneWidget);
      expect(find.byKey(const Key('tool-run-live-delta')), findsOneWidget);

      // 2. Settled completed turn: never stuck on 'running', shows 'done'.
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: const Scaffold(
            body: ToolRunCard(event: runningTool, turnEnded: true),
          ),
        ),
      );
      expect(find.textContaining('done', findRichText: true), findsOneWidget);
      expect(find.textContaining('running', findRichText: true), findsNothing);
      expect(find.byKey(const Key('tool-run-live-delta')), findsNothing);

      // 3. Aborted/failed turn: marks interrupted and hides approval actions.
      const pendingApproval = ChatStreamToolEvent(
        id: 'call_live_2',
        callId: 'a1b2c3d4e5f60708',
        tool: 'run_code',
        code: 'print(1)',
        state: 'awaiting_approval',
        backend: 'host',
      );
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: ToolRunCard(
              event: pendingApproval,
              turnEnded: true,
              turnFailed: true,
              onApprove: (_, __, ___) async =>
                  const ApprovalOutcome(ApprovalStatus.approved),
              onDeny: (_, __, ___) async =>
                  const ApprovalOutcome(ApprovalStatus.approved),
            ),
          ),
        ),
      );
      expect(
        find.textContaining('interrupted', findRichText: true),
        findsOneWidget,
      );
      expect(find.byKey(const Key('tool-run-approve')), findsNothing);
      expect(find.byKey(const Key('tool-run-deny')), findsNothing);
    });

    testWidgets('does not overflow on 320px narrow phone at 2x text scale',
        (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1.0;
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
        tester.platformDispatcher.clearTextScaleFactorTestValue();
      });

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: const Scaffold(
            body: SingleChildScrollView(
              padding: EdgeInsets.all(SonderSpace.sm),
              child: ToolRunCard(
                event: ChatStreamToolEvent(
                  id: 'call_narrow',
                  callId: 'a1b2c3d4e5f60708',
                  tool: 'very_long_workspace_search_and_replace_tool',
                  backend: 'super_long_custom_sandbox_backend_name_v2',
                  state: 'running',
                  detail: '{"query":"stream_timeout_recovery"}',
                  argumentsReported: true,
                ),
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
    });
  });

  group('TurnProgress client approval and error recovery transitions', () {
    test('awaiting_client_approval locks phase and error returns to thinking',
        () {
      final p = TurnProgress(model: 'sonder')..opened();
      expect(
        p.toolEvent(const ChatStreamToolEvent(
          id: 'c1',
          tool: 'run_code',
          executionTarget: 'client',
          state: 'awaiting_client_approval',
        )),
        isTrue,
      );
      expect(p.phase, 'awaiting approval');
      // Status polls must not clobber awaiting client approval.
      expect(p.observe(null), isFalse);
      expect(p.phase, 'awaiting approval');

      expect(
        p.toolEvent(const ChatStreamToolEvent(
          id: 'c1',
          tool: 'run_code',
          state: 'error',
        )),
        isTrue,
      );
      expect(p.phase, 'thinking');
    });
  });

  group('StreamingErrorBanner classification and 1-click retry', () {
    test('classifies timeout, stalled, disconnected, offline, cancelled, and generic',
        () {
      expect(
        classifyStreamError(const ChatMessage(
          role: Role.assistant,
          content: 'The answer took longer than 30 min.',
          error: true,
          diagnostic: 'code: TIMEOUT',
        )).category,
        StreamErrorCategory.timeout,
      );
      expect(
        classifyStreamError(const ChatMessage(
          role: Role.assistant,
          content: 'The server stopped sending for 45 s.',
          error: true,
          diagnostic: 'code: STREAM_STALLED',
        )).category,
        StreamErrorCategory.stalled,
      );
      expect(
        classifyStreamError(const ChatMessage(
          role: Role.assistant,
          content: 'The connection dropped before the answer finished.',
          error: true,
          diagnostic: 'code: STREAM_INTERRUPTED',
        )).category,
        StreamErrorCategory.disconnected,
      );
      expect(
        classifyStreamError(const ChatMessage(
          role: Role.assistant,
          content: 'Cannot reach the Sonder server at http://127.0.0.1:11435.',
          error: true,
          diagnostic: 'code: UNREACHABLE',
        )).category,
        StreamErrorCategory.unreachable,
      );
      final cancelled = classifyStreamError(const ChatMessage(
        role: Role.assistant,
        content: 'Request cancelled.',
        error: true,
        diagnostic: 'code: CANCELLED',
      ));
      expect(cancelled.category, StreamErrorCategory.cancelled);
      expect(cancelled.kind, StatusKind.warn);
      expect(cancelled.recoverable, isTrue);

      final forbidden = classifyStreamError(const ChatMessage(
        role: Role.assistant,
        content: 'Only an administrator can do this.',
        error: true,
        diagnostic: 'HTTP 403\ncode: FORBIDDEN',
      ));
      expect(forbidden.category, StreamErrorCategory.generic);
      expect(forbidden.recoverable, isFalse);
    });

    testWidgets(
        'renders timeout recovery banner with 1-click Retry even when '
        'reloaded (entry.retryable=false), plain text only, and settles '
        'in-flight tools as interrupted', (tester) async {
      var retried = 0;
      const entry = ChatEntry(
        7,
        ChatMessage(
          role: Role.assistant,
          content: 'The Sonder server at http://127.0.0.1:11435 did not respond in time.',
          error: true,
          diagnostic: 'code: TIMEOUT',
        ),
        retryable: false,
        elapsedMs: 45200,
        toolCalls: [
          ChatStreamToolEvent(
            id: 'tc_abort',
            tool: 'file_search',
            state: 'running',
            detail: '{"pattern":"*.dart"}',
          ),
        ],
      );

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(SonderSpace.md),
              child: StreamingErrorBanner(
                entry: entry,
                footer: 'failed after 45.2s',
                onRetry: () => retried++,
                copyAction: const SizedBox.shrink(),
              ),
            ),
          ),
        ),
      );
      await tester.pump();

      expect(find.byKey(const Key('error-notice')), findsOneWidget);
      expect(find.textContaining('timed out', findRichText: true), findsOneWidget);
      expect(find.textContaining('Retry now', findRichText: true), findsOneWidget);
      expect(find.byType(MarkdownBody), findsNothing);
      expect(
        find.textContaining('interrupted', findRichText: true),
        findsOneWidget,
      );
      expect(find.textContaining('running', findRichText: true), findsNothing);

      await tester.tap(find.byKey(const Key('error-retry')));
      await tester.pump();
      expect(retried, 1);
    });

    testWidgets('hides Retry for non-retryable 403 error', (tester) async {
      const entry = ChatEntry(
        8,
        ChatMessage(
          role: Role.assistant,
          content: 'Only an administrator can do this.',
          error: true,
          diagnostic: 'HTTP 403\ncode: FORBIDDEN',
        ),
        retryable: false,
      );
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: StreamingErrorBanner(
              entry: entry,
              onRetry: () {},
              copyAction: const SizedBox.shrink(),
            ),
          ),
        ),
      );
      expect(find.byKey(const Key('error-retry')), findsNothing);
    });

    testWidgets('lays out error banner cleanly on 320px phone at 2x scale',
        (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1.0;
      tester.platformDispatcher.textScaleFactorTestValue = 2.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
        tester.platformDispatcher.clearTextScaleFactorTestValue();
      });

      const entry = ChatEntry(
        9,
        ChatMessage(
          role: Role.assistant,
          content: 'The connection dropped before the answer finished.',
          error: true,
          diagnostic: 'code: STREAM_INTERRUPTED',
        ),
        retryable: true,
      );
      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: SingleChildScrollView(
              padding: const EdgeInsets.all(SonderSpace.sm),
              child: StreamingErrorBanner(
                entry: entry,
                footer: 'failed after 12.0s',
                onRetry: () {},
                copyAction: OutlinedButton(
                  key: const Key('error-copy'),
                  onPressed: () {},
                  child: const Text('Copy error'),
                ),
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
    });
  });

  group('Transcript auto-scroll respects user scroll-up', () {
    testWidgets(
        'scrolling up detaches auto-scroll, shows Streaming jump button, '
        'and prevents streaming deltas from yanking the user back down',
        (tester) async {
      final scroll = ScrollController();
      final live = ValueNotifier<LiveTurn?>(
        LiveTurn(startedAt: DateTime(2026, 11, 25, 1, 0), phase: 'writing'),
      );
      addTearDown(() {
        scroll.dispose();
        live.dispose();
      });

      final filler = List.generate(
        18,
        (i) => ChatEntry(
          i + 1,
          ChatMessage(
            role: i.isEven ? Role.user : Role.assistant,
            content: 'Message #$i\n${List.filled(6, 'Line of conversation history.').join('\n')}',
          ),
        ),
      );
      var pendingText = 'Initial streaming output.\n';
      late StateSetter rebuild;

      await tester.pumpWidget(
        MaterialApp(
          theme: SonderTheme.dark,
          home: Scaffold(
            body: SizedBox(
              width: 600,
              height: 400,
              child: StatefulBuilder(
                builder: (context, setState) {
                  rebuild = setState;
                  return Transcript(
                    entries: [
                      ...filler,
                      ChatEntry(
                        99,
                        ChatMessage(
                          role: Role.assistant,
                          content: pendingText,
                          pending: true,
                        ),
                      ),
                    ],
                    scroll: scroll,
                    live: live,
                    actions: _stubActions(),
                  );
                },
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Start at the bottom.
      scroll.jumpTo(scroll.position.maxScrollExtent);
      await tester.pump();
      expect(find.byKey(const Key('transcript-jump-to-bottom')), findsNothing);
      expect(isTranscriptAutoScrollDetached(scroll), isFalse);

      // User drags UP slightly (less than the old 160px threshold!).
      await tester.drag(
        find.byKey(const Key('chat-transcript')),
        const Offset(0, 100),
      );
      await tester.pump();
      final userOffset = scroll.position.pixels;
      expect(userOffset, lessThan(scroll.position.maxScrollExtent));
      expect(isTranscriptAutoScrollDetached(scroll), isTrue);
      expect(find.byKey(const Key('transcript-jump-to-bottom')), findsOneWidget);
      expect(find.text('Streaming'), findsOneWidget);

      // Rapid streaming deltas + explicit scrollTranscriptToEnd must NOT yank user back!
      for (var i = 0; i < 5; i++) {
        rebuild(() {
          pendingText = '$pendingText\nMore streamed paragraph $i\n'
              'Extra detail line $i to grow maxScrollExtent.';
        });
        scrollTranscriptToEnd(scroll);
        await tester.pump();
      }
      expect(scroll.position.pixels, closeTo(userOffset, 1.0));
      expect(isTranscriptAutoScrollDetached(scroll), isTrue);

      // Tapping the jump-to-bottom pill snaps back and re-attaches.
      await tester.tap(find.byKey(const Key('transcript-jump-to-bottom')));
      await tester.pumpAndSettle();
      expect(isTranscriptAutoScrollDetached(scroll), isFalse);
      expect(find.byKey(const Key('transcript-jump-to-bottom')), findsNothing);
      expect(
        scroll.position.maxScrollExtent - scroll.position.pixels,
        lessThanOrEqualTo(2.0),
      );
    });
  });

  group('openChatStream delta & transport hardening', () {
    test('handles uppercase Text/Event-Stream, delta.thinking, and map tool args',
        () async {
      final frames = [
        'data: ${jsonEncode({
              'choices': [
                {
                  'index': 0,
                  'delta': {'thinking': 'Inspecting schema.'},
                }
              ],
            })}\n\n',
        'data: ${jsonEncode({
              'choices': [
                {
                  'index': 0,
                  'delta': {
                    'tool_calls': [
                      {
                        'index': 0.0,
                        'id': 'call_map',
                        'function': {
                          'name': 'lookup',
                          'arguments': {'id': 42},
                        },
                      }
                    ],
                  },
                }
              ],
            })}\n\n',
        'data: ${jsonEncode({
              'choices': [
                {
                  'index': 0,
                  'delta': {'content': 'Done.'},
                  'finish_reason': 'stop',
                }
              ],
            })}\n\n',
        'data: [DONE]\n\n',
      ];
      final client = MockClient.streaming((request, _) async {
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable(frames.map(utf8.encode)),
          200,
          headers: const {'content-type': 'Text/Event-Stream; charset=utf-8'},
        );
      });
      final events = await http.runWithClient(
        () => openChatStream(
          ChatStreamRequest(
            uri: Uri.parse('http://127.0.0.1:11435/v1/chat/completions'),
            headers: const {'content-type': 'application/json'},
            body: '{}',
          ),
        ).toList(),
        () => client,
      );
      expect(events.whereType<ChatStreamReasoningDelta>().single.text,
          'Inspecting schema.');
      final tc = events.whereType<ChatStreamToolCallDelta>().single;
      expect(tc.index, 0);
      expect(tc.name, 'lookup');
      expect(tc.argumentsDelta, '{"id":42}');
      expect(events.whereType<ChatStreamDone>().single.reply.reasoning,
          'Inspecting schema.');
    });

    test('non-SSE stream disconnect mid-body reports retryable transport error',
        () async {
      final client = MockClient.streaming((request, _) async {
        final ctrl = StreamController<List<int>>();
        scheduleMicrotask(() {
          ctrl.add(utf8.encode('{"choices":'));
          ctrl.addError(http.ClientException('Connection reset by peer'));
          ctrl.close();
        });
        return http.StreamedResponse(
          ctrl.stream,
          200,
          headers: const {'content-type': 'application/json'},
        );
      });
      Object? caught;
      try {
        await http.runWithClient(
          () => openChatStream(
            ChatStreamRequest(
              uri: Uri.parse('http://127.0.0.1:11435/v1/chat/completions'),
              headers: const {'content-type': 'application/json'},
              body: '{}',
            ),
          ).toList(),
          () => client,
        );
      } catch (e) {
        caught = e;
      }
      expect(
        caught,
        isA<SonderException>().having((e) => e.retryable, 'retryable', isTrue),
      );
    });
  });
}
