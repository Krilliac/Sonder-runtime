import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/transcript.dart';
import 'package:sonder_runtime/chat/turn_progress.dart';
import 'package:sonder_runtime/theme.dart';

import 'chat_fakes.dart';

void main() {
  test('streamChat emits live reasoning, tool_call deltas, and content deltas',
      () async {
    final frames = [
      'data: ${jsonEncode({
            'id': 'chatcmpl-live',
            'object': 'chat.completion.chunk',
            'model': 'bonsai-9b',
            'choices': [
              {
                'index': 0,
                'delta': {'role': 'assistant', 'reasoning_content': 'Plan: '},
                'finish_reason': null,
              }
            ],
          })}\n\n',
      'data: ${jsonEncode({
            'id': 'chatcmpl-live',
            'object': 'chat.completion.chunk',
            'model': 'bonsai-9b',
            'choices': [
              {
                'index': 0,
                'delta': {'reasoning': 'check weather.'},
                'finish_reason': null,
              }
            ],
          })}\n\n',
      'data: ${jsonEncode({
            'id': 'chatcmpl-live',
            'object': 'chat.completion.chunk',
            'model': 'bonsai-9b',
            'choices': [
              {
                'index': 0,
                'delta': {
                  'tool_calls': [
                    {
                      'index': 0,
                      'id': 'call_1',
                      'type': 'function',
                      'function': {'name': 'get_weather', 'arguments': '{"city":"Tokyo"}'},
                    }
                  ],
                },
                'finish_reason': null,
              }
            ],
          })}\n\n',
      'data: ${jsonEncode({
            'id': 'chatcmpl-live',
            'object': 'chat.completion.chunk',
            'model': 'bonsai-9b',
            'choices': [
              {
                'index': 0,
                'delta': {'content': 'Sunny in Tokyo.'},
                'finish_reason': 'stop',
              }
            ],
          })}\n\n',
      'data: [DONE]\n\n',
    ];

    final client = MockClient.streaming((request, _) async {
      final stream = Stream<List<int>>.fromIterable(
        frames.map((f) => utf8.encode(f)),
      );
      return http.StreamedResponse(
        stream,
        200,
        headers: const {'content-type': 'text/event-stream'},
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

    final reasoning = events.whereType<ChatStreamReasoningDelta>().toList();
    expect(reasoning, hasLength(2));
    expect(reasoning.first.text, 'Plan: ');
    expect(reasoning.last.accumulated, 'Plan: check weather.');

    final toolDeltas = events.whereType<ChatStreamToolCallDelta>().toList();
    expect(toolDeltas, hasLength(1));
    expect(toolDeltas.single.name, 'get_weather');
    expect(toolDeltas.single.argumentsDelta, '{"city":"Tokyo"}');

    final done = events.whereType<ChatStreamDone>().single;
    expect(done.reply.text, 'Sunny in Tokyo.');
    expect(done.reply.reasoning, 'Plan: check weather.');
  });

  test('TurnProgress transitions thinking -> writing on live reasoning then text',
      () {
    final progress = TurnProgress(model: 'sonder');
    expect(progress.phase, 'routing');
    expect(progress.reasoned(), isTrue);
    expect(progress.phase, 'thinking');
    expect(progress.wrote(), isTrue);
    expect(progress.phase, 'writing');
  });

  testWidgets('pending transcript renders live streaming reasoning above text',
      (tester) async {
    SharedPreferences.setMockInitialValues({});
    final backend = FakeChatBackend();
    final controller = ChatController(backend, model: 'sonder');
    await controller.start();

    final sendFuture = controller.send('Explain');
    await tester.pump();
    final turn = backend.turns.single;
    turn.controller.add(const TurnReasoningDelta('Step 1: verify input'));
    await tester.pump();

    final scroll = ScrollController();
    addTearDown(scroll.dispose);
    await tester.pumpWidget(
      MaterialApp(
        theme: SonderTheme.dark,
        home: Scaffold(
          body: ListenableBuilder(
            listenable: controller,
            builder: (context, _) => Transcript(
              entries: controller.entries,
              live: controller.live,
              scroll: scroll,
              actions: TranscriptActions(
                onRetry: (_) {},
                onFeedback: (_) {},
                onStop: controller.cancel,
                onChangeMode: () {},
                onApprove: (_, __, ___, ____) async =>
                    const ApprovalOutcome(ApprovalStatus.approved),
                fetchWorkRun: (_) async => throw UnimplementedError(),
                cancelWorkRun: (_) async => throw UnimplementedError(),
                listWorkRuns: () async => const [],
                onWorkRunResolved: (_, __) {},
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump();

    expect(find.byKey(const Key('streaming-reasoning')), findsOneWidget);
    expect(find.textContaining('Step 1: verify input'), findsOneWidget);
    expect(find.text('Thinking…'), findsOneWidget);

    turn.delta('Final answer');
    await tester.pump();
    expect(find.byKey(const Key('streaming-text')), findsOneWidget);
    expect(find.text('Reasoning'), findsOneWidget);

    turn.done('Final answer');
    await sendFuture;
    await tester.pump();
    expect(controller.messages.last.reasoning, 'Step 1: verify input');
    controller.dispose();
    await tester.pump();
  });
}
