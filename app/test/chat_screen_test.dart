import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/transcript.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

Future<void> _send(WidgetTester tester, String text) async {
  await tester.enterText(find.byType(TextField), text);
  await tester.testTextInput.receiveAction(TextInputAction.send);
  await tester.pump();
}

String _statusLine(WidgetTester tester) => tester
    .widget<Text>(find.byKey(const Key('chat-status-line')))
    .textSpan!
    .toPlainText();

void main() {
  // Real Plex metrics, so cell-width fitting behaves as in the app.
  setUpAll(loadAppFonts);

  group('connection state (P0-3)', () {
    testWidgets('an unreachable server is never "Connected"', (tester) async {
      final backend = FakeChatBackend()
        ..statusError = SonderException('Cannot reach server: SocketException');
      await pumpChat(tester, backend, size: const Size(390, 844));
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Connected'), findsNothing);
      expect(
          find.textContaining("Can't reach 127.0.0.1:11435",
              findRichText: true),
          findsOneWidget);
      expect(find.byKey(const Key('connection-retry')), findsOneWidget);
      expect(find.byKey(const Key('connection-settings')), findsOneWidget);

      backend.statusError = null;
      await tester.tap(find.byKey(const Key('connection-retry')));
      await tester.pump();
      await tester.pump();
      expect(find.text('Connected to 127.0.0.1:11435'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('421 reads as refused, with the remedy', (tester) async {
      final backend = FakeChatBackend(serverUrl: 'http://mypc.local:11435')
        ..statusError = SonderException('Server returned HTTP 421.');
      await pumpChat(tester, backend, size: const Size(390, 844));
      await tester.pump(const Duration(milliseconds: 100));
      expect(
          find.textContaining('! refused', findRichText: true), findsOneWidget);
      expect(
          find.textContaining('mypc.local:11435 refused this address',
              findRichText: true),
          findsOneWidget);
      expect(find.textContaining('SONDER_ALLOWED_HOSTS'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('the desktop rail says the same thing with a word',
        (tester) async {
      final backend = FakeChatBackend()
        ..statusError = SonderException('Cannot reach server: refused');
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await tester.pump(const Duration(milliseconds: 100));
      final rail = find.byKey(const Key('rail-connection'));
      expect(rail, findsOneWidget);
      expect(find.descendant(of: rail, matching: find.text("can't reach")),
          findsOneWidget);
      await unmountChat(tester);
    });
  });

  testWidgets(
      'offline with a conversation: pinned notice, disabled chip '
      '(P2-13)', (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('manual')
      ..autoReply = 'hi';
    await pumpChat(tester, backend);
    await _send(tester, 'hello');
    await tester.pump();
    expect(find.byKey(const Key('offline-notice')), findsNothing);

    backend
      ..statusError = SonderException('Cannot reach server: SocketException')
      ..modeReadError = SonderException('Cannot reach server: x');
    await tester.pump(const Duration(seconds: 16));
    await tester.pump();
    expect(find.byKey(const Key('offline-notice')), findsOneWidget);
    expect(find.byKey(const Key('permission-mode-chip')), findsNothing);
    expect(
        find.byKey(const Key('permission-mode-chip-offline')), findsOneWidget);
    await unmountChat(tester);
  });

  testWidgets('streaming: partial text, live line, slow hint, Stop (P1-1)',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _send(tester, 'why does PSO compile stall?');
    final turn = backend.lastTurn;
    expect(find.byKey(const Key('live-line')), findsOneWidget);
    expect(find.textContaining('◈ working · routing · 0s', findRichText: true),
        findsOneWidget);

    turn.phase('reading files');
    turn.delta('The stall comes from ');
    await tester.pump();
    expect(find.byKey(const Key('streaming-text')), findsOneWidget);
    expect(find.textContaining('The stall comes from', findRichText: true),
        findsWidgets);

    await tester.pump(const Duration(seconds: 12));
    expect(find.textContaining('reading files · 12s', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('slow local model', findRichText: true),
        findsNothing);
    await tester.pump(const Duration(seconds: 9));
    expect(
        find.textContaining('slow local model? try the fast route',
            findRichText: true),
        findsOneWidget);

    await tester.tap(find.byKey(const Key('live-stop')));
    await tester.pump();
    expect(turn.cancels, 1);
    expect(find.byKey(const Key('live-line')), findsNothing);
    // The question comes back to the composer, and the thread rotates.
    expect(tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'why does PSO compile stall?');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pump();
    expect(backend.lastTurn.request.sessionId,
        isNot(backend.turns.first.request.sessionId));
    backend.lastTurn.done('ok');
    await tester.pump();
    await unmountChat(tester);
  });

  testWidgets('answer footer replaces the metrics card (P2-7)', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _send(tester, 'q');
    backend.lastTurn.done(
      'The stall comes from the driver.\n\n```cpp\n'
      'device->CreateGraphicsPipelineState(&desc, IID_PPV_ARGS(&pso));\n```',
      metadata: const ChatResponseMetadata(
          elapsedMs: 61200,
          modelCalls: 2,
          promptTokens: 2600,
          completionTokens: 143,
          model: 'sonder:latest',
          tier: 'code'),
    );
    await tester.pump();
    await tester.pump();
    expect(
        find.text('done 61.2s · 2 model calls · 2.6k→143 tok'), findsOneWidget);
    expect(find.text('useful'), findsOneWidget);
    // The tier from the receipt reaches the status line.
    expect(_statusLine(tester), startsWith('code · sonder'));
    await unmountChat(tester);
  });

  testWidgets('failures: plain text, retry, announced (P2-12, P2-13)',
      (tester) async {
    final announced = <String>[];
    tester.binding.defaultBinaryMessenger.setMockDecodedMessageHandler<Object?>(
      SystemChannels.accessibility,
      (message) async {
        final map = message! as Map<Object?, Object?>;
        if (map['type'] == 'announce') {
          announced.add((map['data']! as Map)['message'] as String);
        }
        return null;
      },
    );
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockDecodedMessageHandler<Object?>(
            SystemChannels.accessibility, null));

    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _send(tester, 'q');
    backend.lastTurn.fail(SonderException(
        'Cannot reach the Sonder server at http://127.0.0.1:11435.\n\n'
        'It does not look like it is running.'));
    await tester.pump();
    await tester.pump();

    expect(announced.last,
        'Request failed: Cannot reach the Sonder server at http://127.0.0.1:11435.');
    final notice = find.byKey(const Key('error-notice'));
    expect(notice, findsOneWidget);
    expect(find.descendant(of: notice, matching: find.byType(MarkdownBody)),
        findsNothing,
        reason: 'error URLs are not auto-linked');
    expect(find.text('useful'), findsNothing);
    expect(find.textContaining('failed after'), findsOneWidget);

    await tester.tap(find.byKey(const Key('error-retry')));
    await tester.pump();
    expect(backend.turns, hasLength(2));
    backend.lastTurn.done('back');
    await tester.pump();
    await tester.pump();
    expect(announced.last, 'Sonder replied');
    expect(find.byKey(const Key('error-notice')), findsNothing);
    await unmountChat(tester);
  });

  testWidgets('status strip keeps the mode word at 320 px (P2-6)',
      (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('acceptEdits')
      ..statusInfo = SystemInfo.fromJson({
        'context': {'context_limit': 8192, 'estimated_tokens': 2100},
        'agents': {'active_agents': 2},
      });
    await pumpChat(tester, backend, size: const Size(320, 700));
    await tester.pump(const Duration(milliseconds: 100));
    expect(_statusLine(tester), contains('acceptEdits'));
    await unmountChat(tester);

    await pumpChat(tester, backend, size: const Size(1440, 900));
    await tester.pump(const Duration(milliseconds: 100));
    expect(
        _statusLine(tester), 'sonder · acceptEdits · ctx 2.1k/8.2k · 2 agents');
    await unmountChat(tester);
  });

  testWidgets(
      '500 messages: polls rebuild no turns, parses are cached, the '
      'reader is not yanked to the end (P2-17)', (tester) async {
    final messages = <Map<String, Object>>[
      for (var i = 0; i < 500; i++)
        ChatMessage(
          role: i.isEven ? Role.user : Role.assistant,
          content:
              i.isEven ? 'question $i' : 'answer $i with some **bold** text',
        ).toJson(),
    ];
    final thread = {
      'id': 'chat-big',
      'title': 'big',
      'project': 'default',
      'created_at': DateTime(2026).toIso8601String(),
      'updated_at': DateTime(2026).toIso8601String(),
      'messages': messages,
    };
    final backend = FakeChatBackend();
    await pumpChat(tester, backend, prefs: {
      'sonder_chat_v2/thread-chat-big.json': jsonEncode(thread),
      'sonder_chat_v2/migrated-v1': 'yes',
    });
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.text('answer 1 with some ', findRichText: true), findsNothing);

    TranscriptDebug.reset();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(seconds: 5));
    }
    expect(backend.statusCalls, greaterThanOrEqualTo(10));
    expect(TranscriptDebug.turnBuilds, 0);

    // Scroll away from the end; a streaming reply must not pull the reader.
    final scrollable = find.byKey(const Key('chat-transcript'));
    await tester.drag(scrollable, const Offset(0, 3000));
    await tester.pump();
    final position = tester.widget<ListView>(scrollable).controller!.position;
    final before = position.pixels;
    await _send(tester, 'another');
    // Sending always goes to the end.
    await tester.pumpAndSettle(const Duration(milliseconds: 50),
        EnginePhase.sendSemanticsUpdate, const Duration(seconds: 2));
    expect(position.pixels, greaterThan(before));
    await tester.drag(scrollable, const Offset(0, 3000));
    await tester.pump();
    final away = position.pixels;
    TranscriptDebug.reset();
    backend.lastTurn.delta('streaming ');
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(position.pixels, away);
    // Only the rows on screen rebuilt, not 500.
    expect(TranscriptDebug.turnBuilds, lessThan(40));
    expect(TranscriptDebug.parses, lessThan(40));
    backend.lastTurn.done('fine');
    await tester.pump();
    await unmountChat(tester);
  });
}
