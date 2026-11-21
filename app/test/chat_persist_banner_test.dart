import 'package:flutter/widgets.dart' show Key;
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/chat_store.dart';

import 'chat_fakes.dart';
import 'goldens/golden_fonts.dart';

void main() {
  setUpAll(loadGoldenFonts);

  testWidgets('W6: the unsaved-chats banner follows ChatStore.persistFailed',
      (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    final banner = find.byKey(const Key('persist-failed-notice'));
    expect(banner, findsNothing);

    ChatStore.persistFailed.value = true;
    await tester.pump();
    expect(banner, findsOneWidget);
    expect(find.text("Chats aren't being saved"), findsOneWidget);
    expect(find.textContaining('refused the write'), findsOneWidget);

    ChatStore.persistFailed.value = false;
    await tester.pump();
    expect(banner, findsNothing);
  });

  testWidgets('W6: a failed history load gets its own wording', (tester) async {
    final backend = FakeChatBackend();
    await pumpChat(tester, backend);
    ChatStore.keepUnknownFiles = true;
    ChatStore.persistFailed.value = true;
    await tester.pump();
    expect(find.textContaining('could not be read'), findsOneWidget);
    expect(find.textContaining('refused the write'), findsNothing);
    ChatStore.keepUnknownFiles = false;
    ChatStore.persistFailed.value = false;
  });
}
