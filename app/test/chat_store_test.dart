import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sonder_runtime/chat/store_files_io.dart';
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

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    ChatStore.backend = PrefsChatStoreBackend();
    ChatStore.resetCache();
  });

  test('threads round-trip, one blob per thread', () async {
    await ChatStore.save([
      _thread('chat-a', [_user('one')], minute: 1),
      _thread('chat-b', [_user('two')], minute: 2),
    ]);
    final names = await ChatStore.backend.list();
    expect(names, containsAll(['thread-chat-a.json', 'thread-chat-b.json']));
    final loaded = await ChatStore.load();
    expect(loaded.map((t) => t.id), ['chat-b', 'chat-a']);
    expect(loaded.last.messages.single.content, 'one');
  });

  test(
      'a corrupt thread is set aside, never overwritten, and costs only itself',
      () async {
    await ChatStore.save([
      _thread('chat-a', [_user('keep me')], minute: 1),
      _thread('chat-b', [_user('fine')], minute: 2),
    ]);
    await ChatStore.backend
        .write('thread-chat-a.json', '{"id": "chat-a", "mess');
    // An older .corrupt copy must survive too.
    await ChatStore.backend.write('thread-chat-a.json.corrupt', 'older backup');

    final loaded = await ChatStore.load();
    expect(loaded.map((t) => t.id), ['chat-b']);
    final names = await ChatStore.backend.list();
    expect(names, contains('thread-chat-a.json.corrupt'));
    expect(names, contains('thread-chat-a.json.corrupt-1'));
    expect(await ChatStore.backend.read('thread-chat-a.json.corrupt'),
        'older backup');
    expect(await ChatStore.backend.read('thread-chat-a.json.corrupt-1'),
        '{"id": "chat-a", "mess');

    // A later save (the app continuing normally) never touches the backups.
    await ChatStore.save([
      _thread('chat-b', [_user('fine'), _user('more')], minute: 3),
    ]);
    expect(await ChatStore.backend.read('thread-chat-a.json.corrupt-1'),
        '{"id": "chat-a", "mess');
  });

  test('everything corrupt still yields one fresh thread', () async {
    await ChatStore.backend.write('thread-x.json', '[not json');
    final loaded = await ChatStore.load();
    expect(loaded, hasLength(1));
    expect(loaded.single.messages, isEmpty);
    expect(await ChatStore.backend.list(), contains('thread-x.json.corrupt'));
  });

  test('v1 history migrates once', () async {
    final v1 = jsonEncode([
      _thread('chat-old', [_user('from v1')], minute: 5).toJson(),
    ]);
    SharedPreferences.setMockInitialValues({ChatStore.legacyKey: v1});
    final first = await ChatStore.load();
    expect(first.single.id, 'chat-old');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString(ChatStore.legacyKey), isNull);

    // A v1 key that reappears (an old build running once more) is ignored.
    await prefs.setString(ChatStore.legacyKey, jsonEncode([]));
    final second = await ChatStore.load();
    expect(second.single.id, 'chat-old');
  });

  test('an unreadable v1 blob is kept aside, not lost', () async {
    SharedPreferences.setMockInitialValues({ChatStore.legacyKey: '[{"broken'});
    final loaded = await ChatStore.load();
    expect(loaded, hasLength(1));
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('${ChatStore.legacyKey}.corrupt'), '[{"broken');
  });

  test('threads are capped at 500 messages and 2 MB', () async {
    final many = [for (var i = 0; i < 620; i++) _user('m$i')];
    final capped = ChatStore.capThread(_thread('c', many));
    expect(capped.messages, hasLength(ChatStore.maxMessages));
    expect(capped.messages.first.content, 'm120');
    expect(capped.messages.last.content, 'm619');

    final big = 'x' * 20000;
    final heavy = [for (var i = 0; i < 200; i++) _user('$i $big')];
    final byBytes = ChatStore.capThread(_thread('h', heavy));
    final size = utf8.encode(jsonEncode(byBytes.toJson())).length;
    expect(size, lessThanOrEqualTo(ChatStore.maxThreadBytes));
    expect(byBytes.messages.last.content, startsWith('199 '));
    expect(byBytes.messages.length, lessThan(200));
  });

  test('pending rows are never stored', () async {
    await ChatStore.save([
      _thread('chat-p', [
        _user('q'),
        const ChatMessage(role: Role.assistant, content: '', pending: true),
      ]),
    ]);
    final loaded = await ChatStore.load();
    expect(loaded.single.messages, hasLength(1));
  });

  test('session rotation counters persist', () async {
    await ChatStore.saveSessions({'chat-a': 2});
    expect(await ChatStore.loadSessions(), {'chat-a': 2});
  });

  test('directory backend: one file per thread, corrupt file renamed',
      () async {
    final dir = await Directory.systemTemp.createTemp('sonder_chat_store');
    addTearDown(() => dir.delete(recursive: true));
    ChatStore.backend = DirectoryChatStoreBackend(dir);
    ChatStore.resetCache();
    await ChatStore.save([
      _thread('chat-f', [_user('file')])
    ]);
    final file = File('${dir.path}/chats/thread-chat-f.json');
    expect(file.existsSync(), isTrue);
    file.writeAsStringSync('{oops');
    final loaded = await ChatStore.load();
    expect(loaded.single.id, isNot('chat-f'));
    expect(File('${dir.path}/chats/thread-chat-f.json.corrupt').existsSync(),
        isTrue);
  });
}
