// Live progress (#9): what the server actually reports becomes the live
// line's phase, the timer keeps counting with animations off, and a turn
// with no output for 20 s says so.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';
import 'package:sonder_runtime/chat/controller.dart';
import 'package:sonder_runtime/chat/live_line.dart';
import 'package:sonder_runtime/chat/turn_progress.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/theme.dart';

import 'fixtures/server_fixtures.dart';
import 'goldens/golden_fonts.dart';

/// One running activity span as `/v1/sonder/status` projects it.
Map<String, dynamic> _span(
  String id, {
  String label = 'chat:sonder',
  List<Map<String, dynamic>> events = const [],
  int modelCalls = 0,
  int toolCalls = 0,
}) =>
    {
      'id': id,
      'label': label,
      'status': 'running',
      'model_calls': modelCalls,
      'tool_calls': toolCalls,
      'events': [
        {'kind': 'response_start', 'elapsed_ms': 0},
        ...events,
      ],
    };

Map<String, dynamic> _tool(String tool) =>
    {'kind': 'tool_call', 'tool': tool, 'title': 'Read File', 'ok': true};

const _modelCall = {'kind': 'model_call', 'model': 'qwen3:14b', 'ok': true};

ActivityStatus _activity(List<Map<String, dynamic>> spans) =>
    ActivityStatus.fromJson({'active_count': spans.length, 'active': spans});

void main() {
  setUpAll(loadGoldenFonts);

  group('TurnProgress', () {
    test('routing, then thinking once the server commits, then writing', () {
      final p = TurnProgress(model: 'sonder');
      expect(p.phase, 'routing');
      expect(p.opened(), isTrue);
      expect(p.phase, 'thinking');
      expect(p.opened(), isFalse);
      expect(p.wrote(), isTrue);
      expect(p.phase, 'writing');
      expect(p.wrote(), isFalse, reason: 'more text is not a new phase');
    });

    test("the turn's span names tools and model calls as the REPL does", () {
      final p = TurnProgress(model: 'sonder');
      expect(p.observe(_activity([_span('r2')])), isTrue);
      expect(p.phase, 'thinking');
      expect(p.observe(_activity([_span('r2')])), isFalse,
          reason: 'nothing new was recorded');
      expect(
          p.observe(_activity([
            _span('r2', events: [_tool('file_read')], toolCalls: 1)
          ])),
          isTrue);
      expect(p.phase, 'file_read');
      // The same tool again is still progress, under the same phase.
      expect(
          p.observe(_activity([
            _span('r2',
                events: [_tool('file_read'), _tool('file_read')], toolCalls: 2)
          ])),
          isTrue);
      expect(p.phase, 'file_read');
      p.observe(_activity([
        _span('r2', events: [_tool('file_read'), _modelCall], modelCalls: 1)
      ]));
      expect(p.phase, 'model call 2');
      p.observe(_activity([
        _span('r2',
            events: [
              _modelCall,
              {'kind': 'model_escalation'}
            ],
            modelCalls: 1)
      ]));
      expect(p.phase, 'escalating');
    });

    test('text after activity is writing; activity after text wins back', () {
      final p = TurnProgress(model: 'sonder')
        ..observe(_activity([_span('r2')]));
      p.wrote();
      expect(p.phase, 'writing');
      p.observe(_activity([
        _span('r2', events: [_tool('run_code')], toolCalls: 1)
      ]));
      expect(p.phase, 'run_code');
    });

    test('spans of other work are never this turn', () {
      final p = TurnProgress(model: 'qwen3:14b', preexisting: {'r1'});
      // Already running before the turn; another route; another model.
      expect(
          p.observe(_activity([
            _span('r1', label: 'chat:qwen3:14b'),
            _span('r3', label: 'work run'),
            _span('r4', label: 'chat:sonder'),
          ])),
          isFalse);
      expect(p.phase, 'routing');
      expect(
          p.observe(_activity([
            _span('r1', label: 'chat:qwen3:14b'),
            _span('r5', label: 'chat:qwen3:14b'),
          ])),
          isTrue);
      // Claimed: a newer span with the same label does not take over.
      expect(
          p.observe(_activity([
            _span('r5', label: 'chat:qwen3:14b'),
            _span('r6',
                label: 'chat:qwen3:14b',
                events: [_tool('web_search')],
                toolCalls: 1),
          ])),
          isFalse);
      expect(p.phase, 'thinking');
    });
  });

  test(
      'the production backend turns stream milestones and status readings '
      'into phases', () async {
    final release = Completer<void>();
    var statusReads = 0;
    final events = <TurnEvent>[];
    await recordStreamingClients(() async {
      final backend = SonderApiChatBackend(baseUrl: 'http://127.0.0.1:11435');
      // Before the turn: other work is running.
      await backend.systemInfo();
      final turn = backend.startTurn(const TurnRequest(
        history: [ChatMessage(role: Role.user, content: 'hi')],
        model: 'sonder',
      ));
      final done = turn.events.listen(events.add).asFuture<void>();
      await pause(30);
      expect(events.whereType<TurnPhase>().map((e) => e.phase), ['thinking'],
          reason: 'the early SSE headers mean the server committed');
      await backend.systemInfo();
      await pause(5);
      expect(events.whereType<TurnPhase>().last.phase, 'file_read');
      release.complete();
      await done;
    }, (request, body) async {
      await body.drain<void>();
      if (request.url.path == '/v1/sonder/status') {
        statusReads++;
        final spans = [
          _span('r1', events: [_tool('git_status')], toolCalls: 1),
          if (statusReads > 1)
            _span('r2', events: [_tool('file_read')], toolCalls: 1),
        ];
        return http.StreamedResponse(
          Stream.value(utf8.encode(jsonEncode({
            'status': 'ok',
            'activity': {'active_count': spans.length, 'active': spans},
          }))),
          200,
          headers: jsonHeaders,
        );
      }
      final controller = StreamController<List<int>>();
      () async {
        controller.add(utf8.encode(': keep-alive\n\n'));
        await release.future;
        controller.add(
            utf8.encode('data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'));
        await controller.close();
      }();
      return http.StreamedResponse(controller.stream, 200,
          headers: const {'content-type': 'text/event-stream'});
    });
    expect(events.whereType<TurnPhase>().map((e) => e.phase),
        ['thinking', 'file_read', 'writing']);
    // The phase comes before the text it announces.
    final writing =
        events.indexWhere((e) => e is TurnPhase && e.phase == 'writing');
    expect(events[writing + 1], isA<TurnDelta>());
    expect(events.last, isA<TurnDone>());
  });

  group('LiveLineView', () {
    late ValueNotifier<LiveTurn?> live;
    final start = DateTime(2026, 10, 1, 12);

    Future<void> pumpLine(WidgetTester tester,
        {bool reduceMotion = false, int output = 0}) async {
      await tester.pumpWidget(MaterialApp(
        theme: SonderTheme.dark,
        home: MediaQuery(
          data: MediaQueryData(
              size: const Size(800, 600), disableAnimations: reduceMotion),
          child: Scaffold(
            body: LiveLineView(live: live, onStop: () {}, outputLength: output),
          ),
        ),
      ));
    }

    /// The controller's 1 Hz clock.
    void tick() => live.value =
        live.value!.copyWith(elapsedSeconds: live.value!.elapsedSeconds + 1);

    setUp(() => live = ValueNotifier<LiveTurn?>(
        LiveTurn(startedAt: start, model: 'sonder:latest')));

    testWidgets('no output for 20 s turns warn and says so', (tester) async {
      await pumpLine(tester);
      live.value = live.value!.copyWith(phase: 'thinking');
      for (var i = 0; i < 19; i++) {
        tick();
      }
      await tester.pump();
      expect(find.byKey(const Key('live-stall')), findsNothing);
      tick();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.textContaining('! no output for 20s', findRichText: true),
          findsOneWidget);
      expect(
          find.textContaining('slow local model? try the fast route',
              findRichText: true),
          findsOneWidget);
      expect(
          find.textContaining('working · thinking · 20s', findRichText: true),
          findsOneWidget);
    });

    testWidgets('server progress under the same phase resets the stall clock',
        (tester) async {
      await pumpLine(tester);
      for (var i = 0; i < 15; i++) {
        tick();
      }
      // A phase event re-states the phase within the same second.
      live.value = live.value!.copyWith(phase: 'routing');
      for (var i = 0; i < 10; i++) {
        tick();
      }
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(
          find.textContaining('no output', findRichText: true), findsNothing);
      for (var i = 0; i < 10; i++) {
        tick();
      }
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.textContaining('! no output for 20s', findRichText: true),
          findsOneWidget);
    });

    testWidgets('new answer text resets the stall clock', (tester) async {
      await pumpLine(tester);
      for (var i = 0; i < 18; i++) {
        tick();
      }
      await pumpLine(tester, output: 12);
      for (var i = 0; i < 18; i++) {
        tick();
      }
      await tester.pump();
      expect(
          find.textContaining('no output', findRichText: true), findsNothing);
    });

    testWidgets(
        'with reduced motion the glyph holds still and the timer '
        'keeps counting', (tester) async {
      await pumpLine(tester, reduceMotion: true);
      for (var s = 1; s <= 3; s++) {
        tick();
        await tester.pump();
        expect(
            find.textContaining('working · routing · ${s}s',
                findRichText: true),
            findsOneWidget);
        final glyph =
            tester.widget<AnimatedOpacity>(find.byKey(const Key('live-glyph')));
        expect(glyph.opacity, 1);
        expect(glyph.duration, Duration.zero);
        expect(tester.binding.hasScheduledFrame, isFalse);
      }
    });

    testWidgets('with motion the glyph breathes on the tick', (tester) async {
      await pumpLine(tester);
      tick();
      await tester.pump();
      final glyph =
          tester.widget<AnimatedOpacity>(find.byKey(const Key('live-glyph')));
      expect(glyph.opacity, lessThan(1));
    });
  });
}
