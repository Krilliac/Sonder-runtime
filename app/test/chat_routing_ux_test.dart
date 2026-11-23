import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/drawer.dart';

import 'chat_fakes.dart';
import 'goldens/golden_fonts.dart';
import 'runtime_fixtures.dart';

EcosystemReading _allInference() => EcosystemReading.parse(ecosystemJson(
    inference: inferenceStatusJson()..['models'] = ['qwen3:14b']));

Future<void> _openPicker(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Choose inference route or model'));
  await tester.pumpAndSettle();
}

void main() {
  setUpAll(loadGoldenFonts);

  group('model picker routing', () {
    testWidgets(
        'routes bound to Sonder Inference say so; exact models are '
        'grouped under Ollama (direct)', (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'fast', 'general', 'qwen3:14b']
        ..ecosystem = _allInference();
      await pumpChat(tester, backend);
      await tester.pump();

      // The composer's picker names what will answer; its accessible name
      // carries the full binding.
      final semantics = tester.ensureSemantics();
      expect(find.text('sonder · qwen3:14b'), findsOneWidget);
      expect(
          find.bySemanticsLabel('Model: sonder · Sonder Inference (qwen3:14b)'),
          findsOneWidget);
      semantics.dispose();
      await _openPicker(tester);
      expect(
          find.text('general · Sonder Inference (qwen3:14b)'), findsOneWidget);
      expect(find.text('fast · Sonder Inference (qwen3:14b)'), findsOneWidget);
      expect(
          find.byKey(const Key('model-group-ollama-direct')), findsOneWidget);
      expect(find.text('Ollama (direct)'), findsOneWidget);
      expect(find.text('qwen3:14b'), findsOneWidget);

      await tester.tap(find.text('qwen3:14b'));
      await tester.pumpAndSettle();
      expect(find.text('qwen3:14b · Ollama (direct)'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('everything on Ollama keeps today\'s look', (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'general', 'llama3:8b']
        ..ecosystem = EcosystemReading.parse(ecosystemAllOllama());
      await pumpChat(tester, backend);
      await tester.pump();
      await _openPicker(tester);
      expect(find.text('sonder (local route)'), findsWidgets);
      expect(find.text('general'), findsOneWidget);
      expect(find.text('llama3:8b'), findsOneWidget);
      expect(find.text('Ollama (direct)'), findsNothing);
      await unmountChat(tester);
    });

    testWidgets('a non-admin still sees routing from /v1/models rows',
        (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'general', 'llama3:8b']
        ..origins = const {
          'sonder': ModelOrigin(
              kind: 'route',
              provider: 'sonder_inference',
              servedModel: 'qwen3:14b'),
          'general': ModelOrigin(
              kind: 'route',
              provider: 'sonder_inference',
              servedModel: 'qwen3:14b'),
          'llama3:8b': ModelOrigin(kind: 'model', provider: 'ollama'),
        }
        ..ecosystemError = SonderException('forbidden', httpStatus: 403);
      await pumpChat(tester, backend);
      await tester.pump();
      await _openPicker(tester);
      expect(
          find.text('general · Sonder Inference (qwen3:14b)'), findsOneWidget);
      expect(find.text('Ollama (direct)'), findsOneWidget);
      await unmountChat(tester);
    });

    testWidgets('an unreadable ecosystem document keeps today\'s look',
        (tester) async {
      final backend = FakeChatBackend()
        ..models = const ['sonder', 'llama3:8b']
        ..ecosystemError = SonderException('forbidden', httpStatus: 403);
      await pumpChat(tester, backend);
      await tester.pump();
      await _openPicker(tester);
      expect(find.text('Ollama (direct)'), findsNothing);
      expect(find.text('sonder (local route)'), findsWidgets);
      await unmountChat(tester);
    });

    testWidgets(
        '8-tier openai_compatible stack resolves per-tier models on the '
        'composer pill and groups image models distinctly', (tester) async {
      final json = ecosystemJson(
        provider: 'openai_compatible',
        inference: inferenceStatusJson()
          ..['models'] = ['astra-cyber-35b-a3b:latest']
          ..['tier_models'] = {'security': 'astra-cyber-35b-a3b:latest'},
      );
      final providers = json['providers'] as Map<String, dynamic>;
      providers['tier_providers'] = {
        'fast': 'openai_compatible',
        'general': 'openai_compatible',
        'code': 'openai_compatible',
        'reasoning': 'openai_compatible',
        'vision': 'ollama',
        'security': 'sonder_inference',
        'creative': 'openai_compatible',
        'draft': 'openai_compatible',
      };
      (providers['status'] as Map<String, dynamic>)['openai_compatible'] = {
        'provider': 'openai_compatible',
        'state': 'unknown',
        'configured': true,
        'base_url': 'http://127.0.0.1:18080',
        'model': 'bonsai-27b-pq2',
        'models': ['bonsai-9b-mtp', 'bonsai-27b-pq2', 'bonsai-4b-mtp'],
        'tier_models': {
          'fast': 'bonsai-9b-mtp',
          'general': 'bonsai-27b-pq2',
          'code': 'bonsai-27b-pq2',
          'reasoning': 'bonsai-27b-pq2',
          'creative': 'bonsai-27b-pq2',
          'draft': 'bonsai-4b-mtp',
        },
      };
      json['image_generation'] = {
        'configured': true,
        'backend': 'openai_api',
        'api_url': 'http://127.0.0.1:11440',
        'model': 'sdxl-turbo',
        'loopback': true,
      };

      final backend = FakeChatBackend()
        ..models = const [
          'sonder',
          'fast',
          'general',
          'code',
          'reasoning',
          'vision',
          'security',
          'creative',
          'draft',
          'llama3:8b',
          'sdxl-turbo',
        ]
        ..origins = const {
          'sonder': ModelOrigin(kind: 'route', provider: 'openai_compatible'),
        }
        ..ecosystem = EcosystemReading.parse(json);

      await pumpChat(tester, backend);
      await tester.pump();

      // Composer shows the resolved served model, never raw `openai_compatible`.
      expect(find.text('sonder · bonsai-27b-pq2'), findsOneWidget);
      expect(find.textContaining('openai_compatible'), findsNothing);

      await _openPicker(tester);
      expect(find.byKey(const Key('model-group-routes')), findsOneWidget);
      final listScrollable = find.descendant(
          of: find.byKey(const Key('model-picker-list')),
          matching: find.byType(Scrollable));
      await tester.scrollUntilVisible(
          find.byKey(const Key('model-group-image')), 120,
          scrollable: listScrollable);
      expect(find.byKey(const Key('model-group-image')), findsOneWidget);
      expect(find.text('Image generation'), findsOneWidget);
      await tester.scrollUntilVisible(
          find.byKey(const Key('model-group-ollama-direct')), 120,
          scrollable: listScrollable);
      expect(find.byKey(const Key('model-group-ollama-direct')), findsOneWidget);

      // Search for image models finds sdxl-turbo clearly labelled.
      await tester.enterText(
          find.byKey(const Key('model-picker-search')), 'image');
      await tester.pump();
      expect(find.byKey(const Key('model-option-sdxl-turbo')), findsOneWidget);
      expect(find.text('sdxl-turbo · Image generation (127.0.0.1:11440)'),
          findsOneWidget);

      await tester.tap(find.byKey(const Key('model-option-sdxl-turbo')));
      await tester.pumpAndSettle();
      expect(find.text('sdxl-turbo · Image'), findsOneWidget);
      await unmountChat(tester);
    });
  });

  // The conversation list lives in the app shell's sidebar.
  group('delete chat', () {
    testWidgets('deleting a chat with messages offers Undo that restores it',
        (tester) async {
      final backend = FakeChatBackend()..autoReply = 'hello back';
      await pumpShell(tester, backend, size: const Size(1440, 900));
      await tester.enterText(find.byType(TextField), 'first message');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pumpAndSettle();
      expect(find.text('hello back', findRichText: true), findsWidgets);

      await tester.tap(find.byKey(const Key('shell-new-chat')));
      await tester.pumpAndSettle();
      final rows = find.byType(ThreadRow);
      expect(rows, findsNWidgets(2));
      // The older chat (with messages) is second in the list; its Delete
      // shows under the pointer.
      final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await mouse.addPointer(location: Offset.zero);
      addTearDown(mouse.removePointer);
      await mouse.moveTo(tester.getCenter(rows.last));
      await tester.pump();
      await tester.tap(find.descendant(
          of: rows.last, matching: find.byTooltip('Delete chat')));
      await tester.pumpAndSettle();
      expect(find.byType(ThreadRow), findsOneWidget);
      expect(find.text('Chat deleted.'), findsOneWidget);

      await tester.tap(find.widgetWithText(SnackBarAction, 'Undo'));
      await tester.pumpAndSettle();
      expect(find.byType(ThreadRow), findsNWidgets(2));
      expect(find.textContaining('first message'), findsWidgets);
      await unmountChat(tester);
    });

    testWidgets('deleting an empty chat needs no undo', (tester) async {
      final backend = FakeChatBackend()..autoReply = 'hi';
      await pumpShell(tester, backend, size: const Size(1440, 900));
      await tester.enterText(find.byType(TextField), 'first message');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('shell-new-chat')));
      await tester.pumpAndSettle();
      // The new, empty chat is first; its Delete shows under the pointer.
      final mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await mouse.addPointer(location: Offset.zero);
      addTearDown(mouse.removePointer);
      await mouse.moveTo(tester.getCenter(find.byType(ThreadRow).first));
      await tester.pump();
      await tester.tap(find.descendant(
          of: find.byType(ThreadRow).first,
          matching: find.byTooltip('Delete chat')));
      await tester.pump();
      expect(find.byType(ThreadRow), findsOneWidget);
      expect(find.text('Chat deleted.'), findsNothing);
      await unmountChat(tester);
    });
  });
}
