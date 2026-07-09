import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'models.dart';

class ChatStore {
  static const _key = 'chat_threads_v1';
  static const _maxThreads = 60;

  static Future<List<ChatThread>> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_key);
    if (raw == null || raw.trim().isEmpty) return [ChatThread.fresh()];
    try {
      final decoded = jsonDecode(raw) as List<dynamic>;
      final threads = decoded
          .whereType<Map<String, dynamic>>()
          .map(ChatThread.fromJson)
          .toList()
        ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
      return threads.isEmpty ? [ChatThread.fresh()] : threads;
    } catch (_) {
      return [ChatThread.fresh()];
    }
  }

  static Future<void> save(List<ChatThread> threads) async {
    final prefs = await SharedPreferences.getInstance();
    final cleaned = [...threads]
      ..sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    final capped = cleaned.take(_maxThreads).map((t) => t.toJson()).toList();
    await prefs.setString(_key, jsonEncode(capped));
  }
}
