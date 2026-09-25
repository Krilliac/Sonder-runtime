@Tags(['golden'])
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/permission_mode.dart';
import 'package:sonder_runtime/chat/status_strip.dart';
import 'package:sonder_runtime/models.dart';
import 'package:sonder_runtime/theme.dart';

import '../chat_fakes.dart';

/// Lane C goldens. Generate on Linux only:
///   flutter test --update-goldens --tags golden test/goldens/chat_golden_test.dart
///
/// Fonts are the bundled Plex faces loaded from disk, so no network font is
/// fetched. Status glyphs (◈ ⊘ ✓ ❯) are not in Plex; until lane B's
/// SonderSymbols fallback lands they render with the engine's fallback.

const _phone = Size(390, 844);
const _desk = Size(1440, 900);

const _code = 'The stall comes from the driver compiling the PSO lazily on '
    'first bind, after hot-reload invalidated the cache.\n\n'
    '```cpp\n'
    'device->CreateGraphicsPipelineState(&desc, IID_PPV_ARGS(&pso));\n'
    'cache->Store(key, pso); // warm at load, not at first draw\n'
    '```\n\n'
    'Warm the cache at load with `PrecompilePipelines()`.';

const _refusal =
    "refused /write: file_write changes files and nobody is here to answer "
    "manual's ask, so it is refused rather than assumed; approve with "
    '/approve 3f9a12c0 (mode: manual)';

const _runId = 'wr-7c1e0a55d2b44b1e9d3f6a8b0c2d4e6f';
const _pending =
    'Work is still running as work run $_runId (wall-clock budget 1800 s). '
    'Fetch the answer with GET /v1/work-runs/$_runId, or stop further changes '
    'with POST /v1/work-runs/$_runId/cancel.';

Map<String, Object> _seed(List<ChatMessage> messages) {
  final thread = {
    'id': 'chat-golden',
    'title': 'Shader cache refactor',
    'project': 'engine',
    'created_at': DateTime(2026, 9, 25, 12).toIso8601String(),
    'updated_at': DateTime(2026, 9, 25, 12).toIso8601String(),
    'messages': messages.map((m) => m.toJson()).toList(),
  };
  return {
    'sonder_chat_v2/thread-chat-golden.json': jsonEncode(thread),
    'sonder_chat_v2/migrated-v1': 'yes',
  };
}

final _conversation = <ChatMessage>[
  const ChatMessage(
      role: Role.user,
      content: 'Why does PSO compile stall on first draw after hot-reload?'),
  const ChatMessage(
    role: Role.assistant,
    content: _code,
    responseMetadata: ChatResponseMetadata(
        elapsedMs: 61200,
        modelCalls: 2,
        promptTokens: 2600,
        completionTokens: 143,
        model: 'sonder:latest',
        tier: 'code'),
  ),
  const ChatMessage(
      role: Role.user, content: '/write src/render/pso_cache.cpp'),
  const ChatMessage(role: Role.assistant, content: _refusal),
];

SystemInfo _info() => SystemInfo.fromJson({
      'context': {'context_limit': 8192, 'estimated_tokens': 2100},
      'agents': {'active_agents': 1},
    });

Future<void> _golden(WidgetTester tester, String name) async {
  await expectLater(find.byType(MaterialApp), matchesGoldenFile('$name.png'));
}

void main() {
  setUpAll(loadAppFonts);

  for (final theme in [ThemeMode.dark, ThemeMode.light]) {
    final t = theme == ThemeMode.dark ? 'dark' : 'light';

    testWidgets('chat_empty_offline_$t', (tester) async {
      final backend = FakeChatBackend()
        ..statusError = SonderException('Cannot reach server: SocketException');
      await pumpChat(tester, backend, size: _phone, themeMode: theme);
      await tester.pump(const Duration(milliseconds: 100));
      await _golden(tester, 'chat_empty_offline_$t');
      await unmountChat(tester);
    });

    testWidgets('chat_turn_desk_$t', (tester) async {
      final backend = FakeChatBackend()
        ..mode = permissionModeFor('manual')
        ..statusInfo = _info();
      await pumpChat(tester, backend,
          size: _desk, themeMode: theme, prefs: _seed(_conversation));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.enterText(find.byType(TextField), 'Now profile the warm-up');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pump();
      backend.lastTurn.phase('reading files');
      for (var i = 0; i < 23; i++) {
        await tester.pump(const Duration(seconds: 1));
      }
      await tester.pump(const Duration(milliseconds: 300));
      await _golden(tester, 'chat_turn_desk_$t');
      backend.lastTurn.done('ok');
      await tester.pump();
      await unmountChat(tester);
    });

    testWidgets('work_run_card_$t', (tester) async {
      final backend = FakeChatBackend()
        ..mode = permissionModeFor('acceptEdits')
        ..statusInfo = _info();
      await pumpChat(tester, backend,
          size: _phone,
          themeMode: theme,
          prefs: _seed([
            const ChatMessage(
                role: Role.user, content: 'Refactor the shader cache'),
            const ChatMessage(role: Role.assistant, content: _pending),
          ]));
      await tester.pump(const Duration(milliseconds: 100));
      await _golden(tester, 'work_run_card_$t');
      await unmountChat(tester);
    });
  }

  testWidgets('chat_empty_refused_dark', (tester) async {
    final backend = FakeChatBackend(serverUrl: 'http://mypc.local:11435')
      ..statusError = SonderException('Server returned HTTP 421.');
    await pumpChat(tester, backend, size: _phone);
    await tester.pump(const Duration(milliseconds: 100));
    await _golden(tester, 'chat_empty_refused_dark');
    await unmountChat(tester);
  });

  testWidgets('chat_empty_connected_desk_dark', (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('manual')
      ..statusInfo = _info();
    await pumpChat(tester, backend, size: _desk);
    await tester.pump(const Duration(milliseconds: 100));
    await _golden(tester, 'chat_empty_connected_desk_dark');
    await unmountChat(tester);
  });

  testWidgets('chat_turn_phone_dark', (tester) async {
    final backend = FakeChatBackend()
      ..mode = permissionModeFor('manual')
      ..statusInfo = _info();
    await pumpChat(tester, backend, size: _phone, prefs: _seed(_conversation));
    await tester.pump(const Duration(milliseconds: 100));
    await _golden(tester, 'chat_turn_phone_dark');
    await unmountChat(tester);
  });

  for (final theme in [ThemeMode.dark, ThemeMode.light]) {
    final t = theme == ThemeMode.dark ? 'dark' : 'light';
    testWidgets('raise_sheet_phone_$t', (tester) async {
      final backend = FakeChatBackend()..mode = permissionModeFor('manual');
      await pumpChat(tester, backend, size: _phone, themeMode: theme);
      await tester.enterText(find.byType(TextField), '/mode auto');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pumpAndSettle();
      await _golden(tester, 'raise_sheet_phone_$t');
      await tester.tap(find.byKey(const Key('raise-mode-cancel')));
      await tester.pumpAndSettle();
      await unmountChat(tester);
    });

    testWidgets('approval_sheet_desk_$t', (tester) async {
      final backend = FakeChatBackend()..mode = permissionModeFor('manual');
      await pumpChat(tester, backend,
          size: _desk, themeMode: theme, prefs: _seed(_conversation));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.tap(find.byKey(const Key('refusal-approve')));
      await tester.pumpAndSettle();
      await _golden(tester, 'approval_sheet_desk_$t');
      await tester.tap(find.byKey(const Key('approval-cancel')));
      await tester.pumpAndSettle();
      await unmountChat(tester);
    });
  }

  // P2-6: the status strip at 390, 600 and 1440 px.
  for (final width in [390.0, 600.0, 1440.0]) {
    testWidgets('status_strip_${width.toInt()}', (tester) async {
      tester.view.physicalSize = Size(width, 40);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: SonderTheme.dark,
        home: Scaffold(
          body: Align(
            alignment: Alignment.bottomCenter,
            child: ChatStatusStrip(
              info: ValueNotifier<SystemInfo?>(_info()),
              mode: permissionModeFor('acceptEdits'),
              model: 'qwen2.5-coder:7b-instruct-q4_K_M',
              tier: 'code',
              project: 'engine',
            ),
          ),
        ),
      ));
      await _golden(tester, 'status_strip_${width.toInt()}');
    });
  }

  testWidgets('raise_sheet_widget_accept_edits', (tester) async {
    tester.view.physicalSize = const Size(480, 260);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: SonderTheme.dark,
      home: const Scaffold(
        body: RaiseModeSheet(from: 'manual', to: 'acceptEdits', host: 'mypc'),
      ),
    ));
    await _golden(tester, 'raise_sheet_widget_accept_edits');
  });
}
