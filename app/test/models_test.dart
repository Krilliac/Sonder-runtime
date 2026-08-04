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

  test('UpdateStatus parses releases and available plans', () {
    final status = UpdateStatus.fromJson({
      'running_version': '1.4.0',
      'running_commit': 'abcdef0123456789',
      'platform': 'linux',
      'architecture': 'x86_64',
      'current_target': '/opt/sonder/releases/1.4.0-abcdef01',
      'active_release': {
        'version': '1.4.0',
        'release_id': 'rel_abc',
        'platform': 'linux',
        'architecture': 'x86_64',
      },
      'previous_release': {
        'version': '1.3.0',
        'release_id': 'rel_prev1234',
        'platform': 'linux',
        'architecture': 'x86_64',
      },
      'plans': [
        {
          'update_id': 'upd_1',
          'status': 'available',
          'channel': 'stable',
          'target_version': '1.5.0',
          'created_at_utc': '2026-08-04T00:00:00Z',
          'confirm_nonce': '8c5fc2c3',
        },
        {
          'update_id': 'upd_0',
          'status': 'committed',
          'channel': 'stable',
          'target_version': '1.4.0',
          'created_at_utc': '2026-08-01T00:00:00Z',
        },
      ],
    });

    expect(status.runningVersion, '1.4.0');
    expect(status.activeRelease?.version, '1.4.0');
    expect(status.canRollback, isTrue);
    final available = status.plans.where((p) => p.isAvailable).toList();
    expect(available.length, 1);
    expect(available.single.confirmNonce, '8c5fc2c3');
    expect(status.plans.firstWhere((p) => p.status == 'committed').isTerminal,
        isTrue);
  });

  test('UpdateStatus without a previous release cannot roll back', () {
    final status = UpdateStatus.fromJson({
      'running_version': '1.0.0',
      'running_commit': '',
      'platform': 'linux',
      'architecture': 'x86_64',
      'current_target': null,
      'active_release': null,
      'previous_release': null,
      'plans': const [],
    });
    expect(status.canRollback, isFalse);
    expect(status.activeRelease, isNull);
    expect(status.plans, isEmpty);
  });
}
