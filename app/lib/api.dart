import 'dart:convert';
import 'package:http/http.dart' as http;

import 'models.dart';

/// Thin client for a hosted trilobite instance (trilobite_serve.py).
///
/// The server speaks the OpenAI chat-completions dialect:
///   GET  <base>/v1/models
///   POST <base>/v1/chat/completions   { model, messages[], stream }
/// with an optional `Authorization: Bearer <key>` header when the host
/// enabled auth. This mirrors trilobite_client.py, but for a GUI.
class TrilobiteApi {
  final String baseUrl; // e.g. http://192.168.1.10:11435
  final String apiKey; // empty when the server has auth disabled

  const TrilobiteApi({required this.baseUrl, this.apiKey = ''});

  Uri _uri(String path) {
    final root = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$root$path');
  }

  Map<String, String> _headers() {
    final h = <String, String>{'Content-Type': 'application/json'};
    if (apiKey.trim().isNotEmpty) {
      h['Authorization'] = 'Bearer ${apiKey.trim()}';
    }
    return h;
  }

  /// Verify connectivity + auth. Returns the list of model ids the server
  /// advertises (typically just ["trilobite"]). Throws [TrilobiteException]
  /// on any failure so the UI can show a precise reason.
  Future<List<String>> listModels() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/models'), headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      throw TrilobiteException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw TrilobiteException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw TrilobiteException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj = jsonDecode(resp.body) as Map<String, dynamic>;
      final data = (obj['data'] as List?) ?? const [];
      return data
          .map((m) => (m as Map<String, dynamic>)['id']?.toString() ?? '')
          .where((s) => s.isNotEmpty)
          .toList();
    } catch (_) {
      throw TrilobiteException('Unexpected response from server.');
    }
  }

  Future<SystemInfo> systemInfo() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/trilobite/status'), headers: _headers())
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      throw TrilobiteException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw TrilobiteException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw TrilobiteException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj = jsonDecode(utf8.decode(resp.bodyBytes))
          as Map<String, dynamic>;
      return SystemInfo.fromJson(obj);
    } catch (_) {
      throw TrilobiteException('Could not parse system status.');
    }
  }

  /// Send the full conversation and return the assistant's reply text.
  ///
  /// The serve layer threads history from the messages we send, and also
  /// handles slash-commands (/stats, /train, /pass, …), passive feedback,
  /// and natural-language control — so we simply forward whatever the user
  /// typed as the last user message.
  Future<String> chat(List<ChatMessage> messages,
      {String model = 'trilobite'}) async {
    final body = jsonEncode({
      'model': model,
      'messages': messages
          .where((m) => !m.pending)
          .map((m) => m.toWire())
          .toList(),
      'stream': false,
    });

    late http.Response resp;
    try {
      resp = await http
          .post(_uri('/v1/chat/completions'),
              headers: _headers(), body: body)
          .timeout(const Duration(minutes: 5));
    } catch (e) {
      throw TrilobiteException('Request failed: $e');
    }

    if (resp.statusCode == 401) {
      throw TrilobiteException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw TrilobiteException('Server returned HTTP ${resp.statusCode}.');
    }

    try {
      final obj = jsonDecode(utf8.decode(resp.bodyBytes))
          as Map<String, dynamic>;
      final choices = (obj['choices'] as List?) ?? const [];
      if (choices.isEmpty) {
        throw TrilobiteException('Empty response from server.');
      }
      final msg = (choices.first as Map<String, dynamic>)['message']
          as Map<String, dynamic>?;
      final content = msg?['content']?.toString() ?? '';
      return content.trimRight();
    } on TrilobiteException {
      rethrow;
    } catch (_) {
      throw TrilobiteException('Could not parse server response.');
    }
  }
}

class TrilobiteException implements Exception {
  final String message;
  TrilobiteException(this.message);
  @override
  String toString() => message;
}

class SystemInfo {
  final String status;
  final String stats;
  final String learnTiers;
  final String dbPath;
  final String stateHome;
  final ContextHealth? context;
  final List<SystemModel> models;

  const SystemInfo({
    required this.status,
    required this.stats,
    required this.learnTiers,
    required this.dbPath,
    required this.stateHome,
    required this.context,
    required this.models,
  });

  factory SystemInfo.fromJson(Map<String, dynamic> json) {
    final models = (json['models'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(SystemModel.fromJson)
        .toList();
    return SystemInfo(
      status: json['status']?.toString() ?? '',
      stats: json['stats']?.toString() ?? '',
      learnTiers: json['learn_tiers']?.toString() ?? '',
      dbPath: json['db_path']?.toString() ?? '',
      stateHome: json['state_home']?.toString() ?? '',
      context: json['context'] is Map<String, dynamic>
          ? ContextHealth.fromJson(json['context'] as Map<String, dynamic>)
          : null,
      models: models,
    );
  }
}

class ContextHealth {
  final String session;
  final String project;
  final String title;
  final String status;
  final int contextLimit;
  final int estimatedTokens;
  final double contextPercent;
  final String contextBar;
  final int liveTurns;
  final int maxLiveTurns;
  final int totalTurns;
  final double turnPercent;
  final String turnBar;
  final int summaryTokens;
  final int liveTokens;
  final int summaryChars;
  final String summarizedThrough;
  final String updatedTs;
  final int sessions;
  final int lessons;
  final int facts;
  final int interactions;
  final int outcomes;
  final double memoryPercent;
  final String memoryBar;
  final String dbPath;
  final String stateHome;

  const ContextHealth({
    required this.session,
    required this.project,
    required this.title,
    required this.status,
    required this.contextLimit,
    required this.estimatedTokens,
    required this.contextPercent,
    required this.contextBar,
    required this.liveTurns,
    required this.maxLiveTurns,
    required this.totalTurns,
    required this.turnPercent,
    required this.turnBar,
    required this.summaryTokens,
    required this.liveTokens,
    required this.summaryChars,
    required this.summarizedThrough,
    required this.updatedTs,
    required this.sessions,
    required this.lessons,
    required this.facts,
    required this.interactions,
    required this.outcomes,
    required this.memoryPercent,
    required this.memoryBar,
    required this.dbPath,
    required this.stateHome,
  });

  factory ContextHealth.fromJson(Map<String, dynamic> json) {
    return ContextHealth(
      session: json['session']?.toString() ?? '',
      project: json['project']?.toString() ?? '',
      title: json['title']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      contextLimit: _asInt(json['context_limit']),
      estimatedTokens: _asInt(json['estimated_tokens']),
      contextPercent: _asDouble(json['context_percent']),
      contextBar: json['context_bar']?.toString() ?? '',
      liveTurns: _asInt(json['live_turns']),
      maxLiveTurns: _asInt(json['max_live_turns']),
      totalTurns: _asInt(json['total_turns']),
      turnPercent: _asDouble(json['turn_percent']),
      turnBar: json['turn_bar']?.toString() ?? '',
      summaryTokens: _asInt(json['summary_tokens']),
      liveTokens: _asInt(json['live_tokens']),
      summaryChars: _asInt(json['summary_chars']),
      summarizedThrough: json['summarized_through']?.toString() ?? '',
      updatedTs: json['updated_ts']?.toString() ?? '',
      sessions: _asInt(json['sessions']),
      lessons: _asInt(json['lessons']),
      facts: _asInt(json['facts']),
      interactions: _asInt(json['interactions']),
      outcomes: _asInt(json['outcomes']),
      memoryPercent: _asDouble(json['memory_percent']),
      memoryBar: json['memory_bar']?.toString() ?? '',
      dbPath: json['db_path']?.toString() ?? '',
      stateHome: json['state_home']?.toString() ?? '',
    );
  }

  String consoleText() {
    return [
      'context $contextBar ${contextPercent.toStringAsFixed(1)}%  ~$estimatedTokens/$contextLimit tokens',
      'live    $turnBar $liveTurns/$maxLiveTurns turns ($totalTurns total)',
      'memory  $memoryBar $lessons lessons, $facts facts, $interactions interactions',
      'summary $summaryChars chars, ~$summaryTokens tokens',
    ].join('\n');
  }
}

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.round();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

double _asDouble(Object? value) {
  if (value is double) return value;
  if (value is num) return value.toDouble();
  return double.tryParse(value?.toString() ?? '') ?? 0;
}

class SystemModel {
  final String id;
  final String ownedBy;

  const SystemModel({required this.id, required this.ownedBy});

  factory SystemModel.fromJson(Map<String, dynamic> json) {
    return SystemModel(
      id: json['id']?.toString() ?? '',
      ownedBy: json['owned_by']?.toString() ?? '',
    );
  }
}
