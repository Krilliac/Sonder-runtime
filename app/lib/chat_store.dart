import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'chat/store_files_stub.dart'
    if (dart.library.io) 'chat/store_files_io.dart' as files;
import 'models.dart';

/// Where chat history bytes live. One named blob per thread, so a corrupt
/// blob costs one thread, never the whole history.
abstract class ChatStoreBackend {
  Future<List<String>> list();
  Future<String?> read(String name);
  Future<void> write(String name, String data);
  Future<void> delete(String name);

  /// Move [from] to [to]; used to set a corrupt blob aside, never over an
  /// existing name.
  Future<void> rename(String from, String to);
}

/// SharedPreferences, one key per thread. The default on every platform
/// (and the only one on web).
class PrefsChatStoreBackend implements ChatStoreBackend {
  static const prefix = 'sonder_chat_v2/';

  @override
  Future<List<String>> list() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs
        .getKeys()
        .where((k) => k.startsWith(prefix))
        .map((k) => k.substring(prefix.length))
        .toList();
  }

  @override
  Future<String?> read(String name) async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.get('$prefix$name');
    return value is String ? value : null;
  }

  @override
  Future<void> write(String name, String data) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('$prefix$name', data);
  }

  @override
  Future<void> delete(String name) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('$prefix$name');
  }

  @override
  Future<void> rename(String from, String to) async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.get('$prefix$from');
    if (value is String) await prefs.setString('$prefix$to', value);
    await prefs.remove('$prefix$from');
  }
}

/// Local chat history.
///
/// * One JSON blob per thread (`thread-<id>.json`).
/// * A blob that fails to parse is renamed to `<name>.corrupt` (or
///   `<name>.corrupt-<n>`) and is never overwritten or deleted by the app.
/// * Each thread is capped at [maxMessages] messages and [maxThreadBytes]
///   bytes, dropping the oldest messages first.
/// * The v1 single-blob history (`sonder_chat_threads_v1`) is migrated once;
///   a v1 blob that cannot be parsed is kept as
///   `sonder_chat_threads_v1.corrupt`.
class ChatStore {
  static const legacyKey = 'sonder_chat_threads_v1';
  static const _migratedName = 'migrated-v1';
  static const _sessionsName = 'sessions.json';
  static const maxThreads = 60;
  static const maxMessages = 500;
  static const maxThreadBytes = 2 * 1024 * 1024;

  /// Replaceable for tests and for a native app-support directory
  /// ([useDirectory]).
  static ChatStoreBackend backend = PrefsChatStoreBackend();

  /// What was last written per thread, so a save rewrites only threads that
  /// changed.
  static final Map<String, String> _written = <String, String>{};

  /// Store one JSON file per thread under [path] (native only). Returns
  /// false where files are unavailable (web), leaving the prefs backend.
  static bool useDirectory(String path) {
    final b = files.directoryBackend(path);
    if (b == null) return false;
    backend = b;
    _written.clear();
    return true;
  }

  /// Forget per-process caches (tests swap the backend between cases).
  static void resetCache() => _written.clear();

  static String _nameFor(String id) {
    final safe = id.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
    return 'thread-$safe.json';
  }

  static Future<List<ChatThread>> load() async {
    _written.clear();
    await _migrateLegacy();
    final names = await backend.list();
    final threads = <ChatThread>[];
    for (final name in names) {
      if (!name.startsWith('thread-') || !name.endsWith('.json')) continue;
      final raw = await backend.read(name);
      if (raw == null) continue;
      try {
        final decoded = jsonDecode(raw);
        if (decoded is! Map<String, dynamic>) {
          throw const FormatException('thread is not an object');
        }
        final thread = ChatThread.fromJson(decoded);
        threads.add(thread);
        _written[thread.id] = raw;
      } catch (_) {
        await _setAside(name);
      }
    }
    threads.sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    return threads.isEmpty ? [ChatThread.fresh()] : threads;
  }

  static Future<void> _setAside(String name) async {
    final names = (await backend.list()).toSet();
    var target = '$name.corrupt';
    var n = 1;
    while (names.contains(target)) {
      target = '$name.corrupt-${n++}';
    }
    await backend.rename(name, target);
  }

  static Future<void> _migrateLegacy() async {
    final names = await backend.list();
    if (names.contains(_migratedName)) return;
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(legacyKey);
    if (raw != null && raw.trim().isNotEmpty) {
      try {
        final decoded = jsonDecode(raw) as List<dynamic>;
        final threads = decoded
            .whereType<Map<String, dynamic>>()
            .map(ChatThread.fromJson)
            .toList();
        await save(threads);
        await prefs.remove(legacyKey);
      } catch (_) {
        // Keep the unreadable v1 blob for a person to recover; never
        // overwrite it.
        await prefs.setString('$legacyKey.corrupt', raw);
        await prefs.remove(legacyKey);
      }
    }
    await backend.write(_migratedName, DateTime.now().toIso8601String());
  }

  /// Cap one thread: newest [maxMessages] messages, then drop oldest until
  /// the serialized thread fits [maxThreadBytes].
  static ChatThread capThread(ChatThread thread) {
    var messages = thread.messages.where((m) => !m.pending).toList();
    if (messages.length > maxMessages) {
      messages = messages.sublist(messages.length - maxMessages);
    }
    var capped = thread.copyWith(messages: messages);
    var size = utf8.encode(jsonEncode(capped.toJson())).length;
    while (size > maxThreadBytes && messages.isNotEmpty) {
      // Drop in chunks proportional to the overshoot, at least one.
      final drop = (messages.length * (size - maxThreadBytes) / size)
          .ceil()
          .clamp(1, messages.length);
      messages = messages.sublist(drop);
      capped = thread.copyWith(messages: messages);
      size = utf8.encode(jsonEncode(capped.toJson())).length;
    }
    return capped;
  }

  static Future<void> save(List<ChatThread> threads) async {
    final kept = ([...threads]..sort((a, b) => b.updatedAt.compareTo(a.updatedAt)))
        .take(maxThreads)
        .toList();
    final keepNames = <String>{};
    for (final thread in kept) {
      final name = _nameFor(thread.id);
      keepNames.add(name);
      final data = jsonEncode(capThread(thread).toJson());
      if (_written[thread.id] == data) continue;
      await backend.write(name, data);
      _written[thread.id] = data;
    }
    for (final name in await backend.list()) {
      if (name.startsWith('thread-') &&
          name.endsWith('.json') &&
          !keepNames.contains(name)) {
        await backend.delete(name);
      }
    }
    _written.removeWhere((id, _) => !keepNames.contains(_nameFor(id)));
  }

  /// Per-thread session rotation counters (P1-9).
  static Future<Map<String, int>> loadSessions() async {
    try {
      final raw = await backend.read(_sessionsName);
      if (raw == null) return <String, int>{};
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return <String, int>{};
      return {
        for (final e in decoded.entries)
          if (e.value is int) e.key.toString(): e.value as int,
      };
    } catch (_) {
      return <String, int>{};
    }
  }

  static Future<void> saveSessions(Map<String, int> sessions) =>
      backend.write(_sessionsName, jsonEncode(sessions));
}
