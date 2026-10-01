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
  });

  group('delete chat', () {
    testWidgets('deleting a chat with messages offers Undo that restores it',
        (tester) async {
      final backend = FakeChatBackend()..autoReply = 'hello back';
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await tester.enterText(find.byType(TextField), 'first message');
      await tester.testTextInput.receiveAction(TextInputAction.send);
      await tester.pumpAndSettle();
      expect(find.text('hello back', findRichText: true), findsWidgets);

      await tester.tap(find.byTooltip('New chat').first);
      await tester.pumpAndSettle();
      final deletes = find.byTooltip('Delete chat');
      expect(deletes, findsNWidgets(2));
      // The older chat (with messages) is second in the rail.
      await tester.tap(deletes.last);
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
      final backend = FakeChatBackend();
      await pumpChat(tester, backend, size: const Size(1440, 900));
      await tester.tap(find.byTooltip('New chat').first);
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Delete chat').first);
      await tester.pump();
      expect(find.text('Chat deleted.'), findsNothing);
      await unmountChat(tester);
    });
  });
}
