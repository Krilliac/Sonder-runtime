import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/chat_store.dart';
import 'package:sonder_runtime/models.dart';

ChatThread _thread(String id, List<ChatMessage> messages, {int minute = 0}) =>
    ChatThread(
      id: id,
      title: id,
      project: 'default',
      createdAt: DateTime(2026, 9, 25, 12, minute),
      updatedAt: DateTime(2026, 9, 25, 12, minute),
      messages: messages,
    );

ChatMessage _user(String text) => ChatMessage(role: Role.user, content: text);
ChatMessage _bot(String text) =>
    ChatMessage(role: Role.assistant, content: text);

/// In-memory backend. Writes either complete at once or wait for the test
/// ([hold]), so a test can finish them in any order.
class _FakeBackend implements ChatStoreBackend {
  final Map<String, String> files = {};
  final List<Completer<void>> pending = [];
  final List<String> failWrites = [];
  final List<String> unreadable = [];
  bool hold = false;

  @override
  Future<List<String>> list() async => files.keys.toList();

  @override
  Future<String?> read(String name) async {
    if (unreadable.contains(name)) {
      throw const FileSystemException('permission denied');
    }
    return files[name];
  }

  @override
  Future<void> write(String name, String data) async {
    if (failWrites.contains(name)) throw const FileSystemException('quota');
    if (hold) {
      final c = Completer<void>();
      pending.add(c);
      await c.future;
    }
    files[name] = data;
  }

  @override
  Future<void> delete(String name) async => files.remove(name);

  @override
  Future<void> rename(String from, String to) async {
    final v = files.remove(from);
    if (v != null) files[to] = v;
  }
}

/// Finish held writes newest-first until [all] completes.
Future<void> _drain(_FakeBackend b, Future<Object?> all) async {
  var done = false;
  unawaited(all.then((_) => done = true));
  while (!done) {
    await Future<void>.delayed(Duration.zero);
    if (b.pending.isNotEmpty) b.pending.removeLast().complete();
  }
}

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    ChatStore.backend = PrefsChatStoreBackend();
    ChatStore.resetCache();
  });

  test('W2: an unreadable thread file does not break load', () async {
    final b = _FakeBackend();
    ChatStore.backend = b;
    await ChatStore.save([
      _thread('good', [_user('keep me')], minute: 1)
    ]);
    b.files['thread-bad.json'] = 'x';
    b.unreadable.add('thread-bad.json');
    final loaded = await ChatStore.load();
    expect(loaded.map((t) => t.id), ['good']);
  });

  test('W2: a non-UTF8 thread file on disk does not break load', () async {
    final dir = Directory.systemTemp.createTempSync('chatstore');
    addTearDown(() => dir.deleteSync(recursive: true));
    ChatStore.useDirectory(dir.path);
    await ChatStore.save([
      _thread('good', [_user('keep me')], minute: 1)
    ]);
    File('${dir.path}/chats/thread-bad.json')
        .writeAsBytesSync([0x7b, 0xff, 0xfe]);
    final loaded = await ChatStore.load();
    expect(loaded.map((t) => t.id), ['good']);
    expect(
        File('${dir.path}/chats/thread-bad.json.corrupt').existsSync(), isTrue);
  });

  test('W3: an oversized answer is truncated, never the whole thread dropped',
      () {
    final t = _thread('t', [
      for (var i = 0; i < 20; i++) i.isEven ? _user('q$i') : _bot('a$i'),
      _user('last question'),
      _bot('x' * (3 * 1024 * 1024)),
    ]);
    final c = ChatStore.capThread(t);
    expect(c.messages.length, greaterThanOrEqualTo(2));
    expect(c.messages[c.messages.length - 2].content, 'last question');
    final answer = c.messages.last;
    expect(answer.role, Role.assistant);
    expect(answer.content, isNotEmpty);
    expect(answer.content, contains('truncated'));
    expect(utf8.encode(jsonEncode(c.toJson())).length,
        lessThanOrEqualTo(ChatStore.maxThreadBytes));
  });

  test('W3: a single oversized pair still keeps both messages', () {
    final t = _thread('t', [
      _user('y' * (3 * 1024 * 1024)),
      _bot('z' * (3 * 1024 * 1024)),
    ]);
    final c = ChatStore.capThread(t);
    expect(c.messages.length, 2);
    expect(utf8.encode(jsonEncode(c.toJson())).length,
        lessThanOrEqualTo(ChatStore.maxThreadBytes));
  });

  test('W4: a save failure during migration keeps the legacy blob and retries',
      () async {
    final legacy = jsonEncode([
      _thread('old', [_user('from v1')], minute: 1).toJson()
    ]);
    SharedPreferences.setMockInitialValues(
        <String, Object>{ChatStore.legacyKey: legacy});
    final b = _FakeBackend()..failWrites.add('thread-old.json');
    ChatStore.backend = b;
    final first = await ChatStore.load();
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString(ChatStore.legacyKey), legacy,
        reason: 'legacy blob kept');
    expect(prefs.getString('${ChatStore.legacyKey}.corrupt'), isNull);
    expect(b.files.containsKey('migrated-v1'), isFalse);
    expect(first.map((t) => t.id), contains('old'),
        reason: 'still visible this session');

    // Next start: the disk works again.
    b.failWrites.clear();
    ChatStore.resetCache();
    final second = await ChatStore.load();
    expect(second.map((t) => t.id), ['old']);
    expect(b.files.containsKey('thread-old.json'), isTrue);
    expect(b.files.containsKey('migrated-v1'), isTrue);
    expect(prefs.getString(ChatStore.legacyKey), isNull);
  });

  test('W5: overlapping saves finish with the last requested state', () async {
    final b = _FakeBackend()..hold = true;
    ChatStore.backend = b;
    final a1 = _thread('a', [_user('v1')], minute: 1);
    final a2 = _thread('a', [_user('v2')], minute: 2);
    final gone = _thread('gone', [_user('old')], minute: 1);
    final f1 = ChatStore.save([a1, gone]);
    final f2 = ChatStore.save([a2]);
    await _drain(b, Future.wait([f1, f2]));
    expect(
        b.files.keys.where((k) => k.startsWith('thread-')), ['thread-a.json'],
        reason: 'a deleted thread must not come back');
    expect(b.files['thread-a.json'], contains('v2'));
  });

  test('W5: a stale save never runs after a newer one', () async {
    final b = _FakeBackend()..hold = true;
    ChatStore.backend = b;
    final futures = [
      for (var i = 0; i < 5; i++)
        ChatStore.save([
          _thread('a', [_user('v$i')], minute: i)
        ]),
    ];
    await _drain(b, Future.wait(futures));
    expect(b.files['thread-a.json'], contains('v4'));
  });

  test('W5: file writes never share the fixed <name>.tmp path', () async {
    final dir = Directory.systemTemp.createTempSync('chatstore');
    addTearDown(() => dir.deleteSync(recursive: true));
    ChatStore.useDirectory(dir.path);
    // A leftover or in-use <name>.tmp (here a directory, so it can never be
    // overwritten) must not stop a write: the temp name is unique per write.
    Directory('${dir.path}/chats/thread-a.json.tmp')
        .createSync(recursive: true);
    final backend = ChatStore.backend;
    await backend.write('thread-a.json', 'one');
    await backend.write('thread-a.json', 'two');
    expect(await backend.read('thread-a.json'), 'two');
  });

  test('W6: one failed thread does not stop the rest or the cleanup', () async {
    final b = _FakeBackend();
    ChatStore.backend = b;
    await ChatStore.save([
      _thread('stale', [_user('s')], minute: 0),
    ]);
    b.failWrites.add('thread-a.json');
    await ChatStore.save([
      _thread('a', [_user('a')], minute: 3),
      _thread('b', [_user('b')], minute: 2),
    ]);
    expect(b.files.containsKey('thread-b.json'), isTrue);
    expect(b.files.containsKey('thread-stale.json'), isFalse);
    expect(ChatStore.persistFailed.value, isTrue);

    b.failWrites.clear();
    await ChatStore.save([
      _thread('a', [_user('a')], minute: 3),
      _thread('b', [_user('b')], minute: 2),
    ]);
    expect(b.files.containsKey('thread-a.json'), isTrue);
    expect(ChatStore.persistFailed.value, isFalse);
  });
}
