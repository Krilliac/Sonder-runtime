import 'package:flutter_test/flutter_test.dart';
import 'package:trilobite/models.dart';

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
}
