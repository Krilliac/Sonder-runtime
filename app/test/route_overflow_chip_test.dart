import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api/chat.dart';
import 'package:sonder_runtime/models.dart';

import 'chat_fakes.dart';

const _switched = 'long-context overflow: switched to qwen3.6:35b on '
    '10.77.0.2:8443 — context 41.2k tokens > 32.8k threshold';
const _stayed = 'long-context overflow (qwen3.6:35b) unavailable, stayed on '
    'dense:27b: no eligible Ollama pool worker advertises qwen3.6:35b '
    '— context 41.2k tokens > 32.8k threshold';

Future<void> _reply(WidgetTester tester, FakeChatBackend backend,
    {ChatResponseMetadata? metadata}) async {
  await tester.enterText(find.byType(TextField), 'summarise the thread');
  await tester.testTextInput.receiveAction(TextInputAction.send);
  await tester.pump();
  backend.lastTurn.done('Here is the summary.', metadata: metadata);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
}

void main() {
  test('the receipt overflow entry is parsed into the metadata', () {
    final metadata = chatMetadataFrom({
      'sonder_receipt': {
        'model': 'qwen3.6:35b',
        'overflow': {'status': 'switched', 'notice': _switched, 'worker': 'x'},
      },
    });
    expect(metadata.overflow?.switched, isTrue);
    expect(metadata.overflow?.notice, _switched);
    expect(metadata.diagnosticText, contains(_switched));
    expect(
        ChatResponseMetadata.fromJson(metadata.toJson()).overflow?.notice,
        _switched);
    expect(chatMetadataFrom({'sonder_receipt': {'overflow': 'x'}}).overflow,
        isNull);
  });

  testWidgets('a switched turn shows the overflow chip above the answer',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _reply(tester, backend,
        metadata: const ChatResponseMetadata(
          model: 'qwen3.6:35b',
          overflow: RouteOverflow(status: 'switched', notice: _switched),
        ));
    expect(find.byKey(const Key('route-overflow-chip')), findsOneWidget);
    expect(find.text(_switched), findsOneWidget);
    expect(find.text('· note'), findsOneWidget);
    expect(find.bySemanticsLabel('note: $_switched'), findsOneWidget);
    final chip = tester.getTopLeft(find.byKey(const Key('route-overflow-chip')));
    final answer = tester.getTopLeft(find.textContaining('Here is the summary.'));
    expect(chip.dy, lessThan(answer.dy));
  });

  testWidgets('an unavailable overflow is a warn chip', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _reply(tester, backend,
        metadata: const ChatResponseMetadata(
          model: 'dense:27b',
          overflow: RouteOverflow(status: 'unavailable', notice: _stayed),
        ));
    expect(find.text(_stayed), findsOneWidget);
    expect(find.bySemanticsLabel('warn: $_stayed'), findsOneWidget);
  });

  testWidgets('a turn without an overflow decision shows no chip',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    await _reply(tester, backend,
        metadata: const ChatResponseMetadata(model: 'dense:27b'));
    expect(find.byKey(const Key('route-overflow-chip')), findsNothing);
  });
}
