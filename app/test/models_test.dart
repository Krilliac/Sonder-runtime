import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/models.dart';

void main() {
  test('ChatMessage serializes to OpenAI wire format', () {
    const m = ChatMessage(role: Role.user, content: 'hello');
    expect(m.toWire(), {'role': 'user', 'content': 'hello'});
  });

  test('copyWith preserves role and updates content', () {
    const m = ChatMessage(role: Role.assistant, content: '', pending: true);
    final done = m.copyWith(content: 'hi', pending: false);
    expect(done.role, Role.assistant);
    expect(done.content, 'hi');
    expect(done.pending, false);
  });

  test('ChatThread derives a useful display title', () {
    final thread = ChatThread.fresh().copyWith(messages: const [
      ChatMessage(role: Role.user, content: 'build a dashboard for agents'),
    ]);

    expect(thread.displayTitle, 'build a dashboard for agents');
  });

  test('ChatThread serializes messages and project', () {
    final thread = ChatThread.fresh(project: 'app').copyWith(
      messages: const [ChatMessage(role: Role.assistant, content: 'ok')],
    );
    final restored = ChatThread.fromJson(thread.toJson());

    expect(restored.project, 'app');
    expect(restored.messages.single.content, 'ok');
    expect(restored.messages.single.role, Role.assistant);
  });
}
