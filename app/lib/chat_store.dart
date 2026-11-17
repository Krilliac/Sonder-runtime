import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'chat/store_files_stub.dart'
    if (dart.library.io) 'chat/store_files_io.dart' as files;
import 'models.dart';

final RegExp _accountLine = RegExp(
    r'^\s*(/(?:login|register|admin_login))(?:\s+(\S+))?(\s+\S[\s\S]*)?$',
    caseSensitive: false);

/// True when [text] is a `/login`, `/register` or `/admin_login` line that
/// may carry a password. Such a line must never be stored or sent as chat.
bool isAccountSecretLine(String text) => _accountLine.hasMatch(text);

/// [message] with any typed password removed: a user line such as
/// `/login bob hunter2` (from an app version before the composer
/// intercepted it) keeps only the command and the user name, so it is never
/// persisted again nor re-sent as history.
ChatMessage redactAccountSecret(ChatMessage message) {
  if (message.role != Role.user) return message;
  final m = _accountLine.firstMatch(message.content);
  if (m == null || m.group(3) == null) return message;
  final user = m.group(2) ?? '';
  return message.copyWith(
      content: '${m.group(1)!.toLowerCase()} $user [password removed]');
}

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
  static void resetCache() {
    _written.clear();
    _wroteThisSession.clear();
    _protectedNames.clear();
    _migrationPending = false;
    keepUnknownFiles = false;
    // Tests swap zones and backends; do not wait on an earlier run's work.
    _queued = null;
    _queuedDone = null;
    _saving = false;
    persistFailed.value = false;
  }

  /// True while the last save could not write every thread (quota, disk,
  /// permissions). The UI shows a warning; the next fully successful save
  /// clears it.
  static final ValueNotifier<bool> persistFailed = ValueNotifier<bool>(false);

  /// Set when history could not be read at all: saves then never delete
  /// files for threads they do not know about, so unread history survives.
  /// Cleared by the next successful [load].
  static bool keepUnknownFiles = false;

  /// Thread files this session wrote. After a failed read only these may be
  /// deleted; files that were never read stay.
  static final Set<String> _wroteThisSession = <String>{};

  /// Files that could not be read or parsed and could not be set aside
  /// either. They are never overwritten or deleted by a save.
  static final Set<String> _protectedNames = <String>{};

  /// A v1 history was only partly migrated: the first fully successful save
  /// completes the migration (marker written, legacy key removed).
  static bool _migrationPending = false;

  static String _nameFor(String id) {
    final safe = id.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
    return 'thread-$safe.json';
  }

  static Future<List<ChatThread>> load() async {
    _written.clear();
    keepUnknownFiles = false;
    _wroteThisSession.clear();
    _protectedNames.clear();
    final unmigrated = await _migrateLegacy();
    final names = await backend.list();
    final threads = <ChatThread>[];
    for (final name in names) {
      if (!name.startsWith('thread-') || !name.endsWith('.json')) continue;
      String raw;
      try {
        final read = await backend.read(name);
        if (read == null) continue;
        raw = read;
      } catch (_) {
        // Unreadable (invalid UTF-8, locked, permission denied): set the
        // blob aside like a corrupt one so it costs only itself.
        await _trySetAside(name);
        continue;
      }
      try {
        final decoded = jsonDecode(raw);
        if (decoded is! Map<String, dynamic>) {
          throw const FormatException('thread is not an object');
        }
        final parsed = ChatThread.fromJson(decoded);
        final thread = _redactThread(parsed);
        threads.add(thread);
        // A thread that needed redaction is rewritten on the next save.
        if (identical(thread, parsed)) _written[thread.id] = raw;
      } catch (_) {
        await _trySetAside(name);
      }
    }
    // A v1 history that could not be saved yet stays visible this session
    // (and is written by the next successful save).
    for (final t in unmigrated) {
      if (!threads.any((x) => x.id == t.id)) threads.add(t);
    }
    threads.sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    return threads.isEmpty ? [ChatThread.fresh()] : threads;
  }

  static Future<void> _trySetAside(String name) async {
    try {
      await _setAside(name);
    } catch (_) {
      // Cannot even be moved: leave it in place, and keep every save away
      // from it.
      _protectedNames.add(name);
    }
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

  /// Migrate the v1 blob once. Returns the legacy threads that could not be
  /// written (the kept legacy key and the missing marker make the next start
  /// retry); empty otherwise.
  ///
  /// A retry never overwrites a thread file that already exists (the person
  /// may have edited it since), and never deletes anything.
  static Future<List<ChatThread>> _migrateLegacy() async {
    final names = await backend.list();
    if (names.contains(_migratedName)) return const <ChatThread>[];
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(legacyKey);
    if (raw != null && raw.trim().isNotEmpty) {
      var threads = <ChatThread>[];
      try {
        final decoded = jsonDecode(raw) as List<dynamic>;
        threads = decoded
            .whereType<Map<String, dynamic>>()
            .map(ChatThread.fromJson)
            .toList();
      } catch (_) {
        // Keep the unreadable v1 blob for a person to recover; never
        // overwrite it.
        await prefs.setString('$legacyKey.corrupt', raw);
        await prefs.remove(legacyKey);
      }
      final unsaved = <ChatThread>[];
      for (final t in threads) {
        final name = _nameFor(t.id);
        if (names.contains(name)) continue;
        try {
          await backend.write(name, jsonEncode(capThread(t).toJson()));
          _wroteThisSession.add(name);
        } catch (_) {
          unsaved.add(t);
        }
      }
      if (unsaved.isNotEmpty) {
        // A write failure is not a corrupt blob: keep the legacy key,
        // write no marker, and retry (see [_migrationPending]).
        _migrationPending = true;
        persistFailed.value = true;
        return unsaved;
      }
      await prefs.remove(legacyKey);
    }
    _migrationPending = false;
    try {
      await backend.write(_migratedName, DateTime.now().toIso8601String());
    } catch (_) {
      // Retried next start; the legacy key is already gone.
    }
    return const <ChatThread>[];
  }

  static Future<void> _completeMigration() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await backend.write(_migratedName, DateTime.now().toIso8601String());
      await prefs.remove(legacyKey);
      _migrationPending = false;
    } catch (_) {
      // Still pending: the next successful save tries again.
    }
  }

  static ChatThread _redactThread(ChatThread thread) {
    var changed = false;
    final messages = [
      for (final m in thread.messages)
        () {
          final r = redactAccountSecret(m);
          if (!identical(r, m)) changed = true;
          return r;
        }(),
    ];
    return changed ? thread.copyWith(messages: messages) : thread;
  }

  /// Cap one thread: newest [maxMessages] messages, then drop oldest until
  /// the serialized thread fits [maxThreadBytes]. The newest user+assistant
  /// pair (the last two messages) is always kept; if it alone is too large
  /// its text is truncated with a visible marker.
  static ChatThread capThread(ChatThread thread) {
    var messages = thread.messages
        .where((m) => !m.pending)
        .map(redactAccountSecret)
        .toList();
    if (messages.length > maxMessages) {
      messages = messages.sublist(messages.length - maxMessages);
    }
    int sizeOf(List<ChatMessage> ms) =>
        utf8.encode(jsonEncode(thread.copyWith(messages: ms).toJson())).length;
    var size = sizeOf(messages);
    while (size > maxThreadBytes && messages.length > 2) {
      // Drop in chunks proportional to the overshoot, at least one.
      final drop = (messages.length * (size - maxThreadBytes) / size)
          .ceil()
          .clamp(1, messages.length - 2);
      messages = messages.sublist(drop);
      size = sizeOf(messages);
    }
    if (size > maxThreadBytes) {
      messages = _truncateToFit(messages, size, sizeOf);
    }
    return thread.copyWith(messages: messages);
  }

  static const _truncationMarker =
      '\n\n[truncated: this message was too large to keep]';

  /// Shorten the largest text field until the thread fits. Removing one
  /// UTF-16 unit removes at least one encoded byte, so the loop converges.
  static List<ChatMessage> _truncateToFit(List<ChatMessage> input, int size,
      int Function(List<ChatMessage>) sizeOf) {
    final messages = [...input];
    var guard = 0;
    while (size > maxThreadBytes && guard++ < 64) {
      var bestIndex = -1;
      var bestIsContent = true;
      var bestLength = _truncationMarker.length;
      for (var i = 0; i < messages.length; i++) {
        if (messages[i].content.length > bestLength) {
          bestIndex = i;
          bestIsContent = true;
          bestLength = messages[i].content.length;
        }
        if (messages[i].reasoning.length > bestLength) {
          bestIndex = i;
          bestIsContent = false;
          bestLength = messages[i].reasoning.length;
        }
      }
      if (bestIndex < 0) break;
      final cut = size - maxThreadBytes + _truncationMarker.length + 64;
      var keep = bestLength - cut;
      if (keep < 0) keep = 0;
      final m = messages[bestIndex];
      final text = bestIsContent ? m.content : m.reasoning;
      // Never split a surrogate pair.
      if (keep > 0 && keep < text.length) {
        final unit = text.codeUnitAt(keep - 1);
        if (unit >= 0xD800 && unit <= 0xDBFF) keep--;
      }
      final shorter = text.substring(0, keep) + _truncationMarker;
      messages[bestIndex] = bestIsContent
          ? m.copyWith(content: shorter)
          : m.copyWith(reasoning: shorter);
      size = sizeOf(messages);
    }
    return messages;
  }

  // Saves are serialized: callers fire them unawaited, so two could
  // otherwise interleave their writes and deletes. While one runs, newer
  // requests collapse into the latest list, which is written next.
  static List<ChatThread>? _queued;
  static Completer<bool>? _queuedDone;
  static bool _saving = false;

  /// Persist [threads]. Completes true when every thread was written. Never
  /// throws: a failure is recorded in [persistFailed] and the remaining
  /// threads are still written.
  static Future<bool> save(List<ChatThread> threads) {
    _queued = threads;
    final done = _queuedDone ??= Completer<bool>();
    if (!_saving) {
      _saving = true;
      unawaited(_drainSaves());
    }
    return done.future;
  }

  static Future<void> _drainSaves() async {
    try {
      while (_queued != null) {
        final threads = _queued!;
        final done = _queuedDone!;
        _queued = null;
        _queuedDone = null;
        var ok = false;
        try {
          ok = await _saveNow(threads);
          if (ok && _migrationPending) await _completeMigration();
        } catch (_) {
          ok = false;
        }
        // Complete first: a throwing listener must not strand the callers.
        done.complete(ok);
        try {
          // Never clear the warning while history was never read.
          persistFailed.value = !ok || keepUnknownFiles;
        } catch (_) {}
      }
    } finally {
      _saving = false;
    }
  }

  static Future<bool> _saveNow(List<ChatThread> threads) async {
    final kept = ([...threads]
          ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt)))
        .take(maxThreads)
        .toList();
    final keepNames = <String>{};
    var ok = true;
    for (final thread in kept) {
      final name = _nameFor(thread.id);
      keepNames.add(name);
      try {
        final data = jsonEncode(capThread(thread).toJson());
        if (_written[thread.id] == data) continue;
        await backend.write(name, data);
        _wroteThisSession.add(name);
        _written[thread.id] = data;
      } catch (_) {
        ok = false;
        _written.remove(thread.id);
      }
    }
    try {
      for (final name in await backend.list()) {
        if (name.startsWith('thread-') &&
            name.endsWith('.json') &&
            !keepNames.contains(name) &&
            !_protectedNames.contains(name) &&
            (!keepUnknownFiles || _wroteThisSession.contains(name))) {
          try {
            await backend.delete(name);
            _wroteThisSession.remove(name);
          } catch (_) {
            ok = false;
          }
        }
      }
    } catch (_) {
      ok = false;
    }
    _written.removeWhere((id, _) => !keepNames.contains(_nameFor(id)));
    return ok;
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
