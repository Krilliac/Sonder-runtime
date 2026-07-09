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
  final String localFallbackUrl;

  const TrilobiteApi({
    required this.baseUrl,
    this.apiKey = '',
    this.localFallbackUrl = 'http://127.0.0.1:11435',
  });

  Uri _uri(String path, [String? rootUrl]) {
    final root = (rootUrl ?? baseUrl).trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$root$path');
  }

  Map<String, String> _headers([String? keyOverride]) {
    final h = <String, String>{'Content-Type': 'application/json'};
    final key = keyOverride ?? apiKey;
    if (key.trim().isNotEmpty) {
      h['Authorization'] = 'Bearer ${key.trim()}';
    }
    return h;
  }

  bool get _canFallback {
    final primary = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    final local = localFallbackUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return local.isNotEmpty && primary != local;
  }

  String _fallbackWarning(String operation, Object error) {
    return 'Warning: hosted server ${baseUrl.trim()} was unreachable during $operation ($error). '
        'Fell back to local server ${localFallbackUrl.trim()}.';
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
      if (_canFallback) {
        try {
          resp = await http
              .get(_uri('/v1/models', localFallbackUrl), headers: _headers(''))
              .timeout(const Duration(seconds: 15));
        } catch (_) {
          throw TrilobiteException('Cannot reach server: $e');
        }
      } else {
        throw TrilobiteException('Cannot reach server: $e');
      }
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
      if (_canFallback) {
        try {
          resp = await http
              .get(_uri('/v1/trilobite/status', localFallbackUrl),
                  headers: _headers(''))
              .timeout(const Duration(seconds: 20));
        } catch (_) {
          throw TrilobiteException('Cannot reach server: $e');
        }
      } else {
        throw TrilobiteException('Cannot reach server: $e');
      }
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
  Future<String> chat(
    List<ChatMessage> messages, {
    String model = 'trilobite',
    String contextSize = '8192',
    String sessionId = '',
    String project = '',
  }) async {
    final body = jsonEncode({
      'model': model,
      'context_size': contextSize,
      if (sessionId.trim().isNotEmpty) 'session': sessionId.trim(),
      if (project.trim().isNotEmpty) 'project': project.trim(),
      'messages': messages
          .where((m) => !m.pending)
          .map((m) => m.toWire())
          .toList(),
      'stream': false,
    });

    late http.Response resp;
    String warning = '';
    try {
      resp = await http
          .post(_uri('/v1/chat/completions'),
              headers: _headers(), body: body)
          .timeout(const Duration(minutes: 5));
    } catch (e) {
      if (_canFallback) {
        try {
          resp = await http
              .post(_uri('/v1/chat/completions', localFallbackUrl),
                  headers: _headers(''), body: body)
              .timeout(const Duration(minutes: 5));
          warning = _fallbackWarning('chat', e);
        } catch (_) {
          throw TrilobiteException('Request failed: $e');
        }
      } else {
        throw TrilobiteException('Request failed: $e');
      }
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
      final reply = content.trimRight();
      return warning.isEmpty ? reply : '$warning\n\n$reply';
    } on TrilobiteException {
      rethrow;
    } catch (_) {
      throw TrilobiteException('Could not parse server response.');
    }
  }

  Future<String> register(String username, String password) async {
    return _accountAction('/v1/trilobite/register', username, password);
  }

  Future<String> login(String username, String password) async {
    late http.Response resp;
    try {
      resp = await http
          .post(
            _uri('/v1/trilobite/login'),
            headers: _headers(),
            body: jsonEncode({'username': username, 'password': password}),
          )
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      throw TrilobiteException('Login failed: $e');
    }
    final obj = jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
    if (resp.statusCode != 200 || obj['ok'] != true) {
      throw TrilobiteException(obj['message']?.toString() ?? 'Login failed.');
    }
    return obj['token']?.toString() ?? '';
  }

  Future<String> _accountAction(
      String path, String username, String password) async {
    late http.Response resp;
    try {
      resp = await http
          .post(
            _uri(path),
            headers: _headers(),
            body: jsonEncode({'username': username, 'password': password}),
          )
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      throw TrilobiteException('Account request failed: $e');
    }
    final obj = jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
    if (resp.statusCode != 200 || obj['ok'] != true) {
      throw TrilobiteException(obj['message']?.toString() ?? 'Account request failed.');
    }
    return obj['message']?.toString() ?? 'OK';
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
  final String improvements;
  final String dbPath;
  final String stateHome;
  final ContextHealth? context;
  final AgentStatus? agents;
  final ActivityStatus? activity;
  final List<SystemModel> models;

  const SystemInfo({
    required this.status,
    required this.stats,
    required this.learnTiers,
    required this.improvements,
    required this.dbPath,
    required this.stateHome,
    required this.context,
    required this.agents,
    required this.activity,
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
      improvements: json['improvements']?.toString() ?? '',
      dbPath: json['db_path']?.toString() ?? '',
      stateHome: json['state_home']?.toString() ?? '',
      context: json['context'] is Map<String, dynamic>
          ? ContextHealth.fromJson(json['context'] as Map<String, dynamic>)
          : null,
      agents: json['agents'] is Map<String, dynamic>
          ? AgentStatus.fromJson(json['agents'] as Map<String, dynamic>)
          : null,
      activity: json['activity'] is Map<String, dynamic>
          ? ActivityStatus.fromJson(json['activity'] as Map<String, dynamic>)
          : null,
      models: models,
    );
  }
}

class ActivityStatus {
  final int activeCount;
  final int totalToolCalls;
  final ActivityResponse? latest;
  final List<ActivityResponse> active;

  const ActivityStatus({
    required this.activeCount,
    required this.totalToolCalls,
    required this.latest,
    required this.active,
  });

  factory ActivityStatus.fromJson(Map<String, dynamic> json) {
    final active = (json['active'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ActivityResponse.fromJson)
        .toList();
    return ActivityStatus(
      activeCount: _asInt(json['active_count']),
      totalToolCalls: _asInt(json['total_tool_calls']),
      latest: json['latest'] is Map<String, dynamic>
          ? ActivityResponse.fromJson(json['latest'] as Map<String, dynamic>)
          : null,
      active: active,
    );
  }

  ActivityResponse? get displayResponse =>
      active.isNotEmpty ? active.last : latest;
}

class ActivityResponse {
  final String id;
  final String label;
  final String status;
  final int elapsedMs;
  final int toolCalls;
  final int modelCalls;
  final int fileCreates;
  final int fileEdits;
  final int fileDeletes;
  final int linesAdded;
  final int linesEdited;
  final int linesDeleted;
  final List<String> events;
  final List<String> files;

  const ActivityResponse({
    required this.id,
    required this.label,
    required this.status,
    required this.elapsedMs,
    required this.toolCalls,
    required this.modelCalls,
    required this.fileCreates,
    required this.fileEdits,
    required this.fileDeletes,
    required this.linesAdded,
    required this.linesEdited,
    required this.linesDeleted,
    required this.events,
    required this.files,
  });

  factory ActivityResponse.fromJson(Map<String, dynamic> json) {
    final events = (json['events'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map((e) {
          final kind = e['kind']?.toString() ?? 'event';
          final detail = e['summary']?.toString().trim().isNotEmpty == true
              ? e['summary'].toString()
              : (e['tool'] ?? e['path'] ?? e['model'] ?? '').toString();
          final ms = _asInt(e['elapsed_ms']);
          return '+${ms}ms $kind${detail.isEmpty ? '' : ' $detail'}';
        })
        .where((s) => s.trim().isNotEmpty)
        .toList();
    final files = (json['files'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map((f) {
          final action = f['action']?.toString() ?? 'file';
          final path = f['path']?.toString() ?? '';
          final added = _asInt(f['lines_added']);
          final edited = _asInt(f['lines_edited']);
          final deleted = _asInt(f['lines_deleted']);
          return '$action $path  lines +$added ~$edited -$deleted';
        })
        .where((s) => s.trim().isNotEmpty)
        .toList();
    return ActivityResponse(
      id: json['id']?.toString() ?? '',
      label: json['label']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      elapsedMs: _asInt(json['elapsed_ms']),
      toolCalls: _asInt(json['tool_calls']),
      modelCalls: _asInt(json['model_calls']),
      fileCreates: _asInt(json['file_creates']),
      fileEdits: _asInt(json['file_edits']),
      fileDeletes: _asInt(json['file_deletes']),
      linesAdded: _asInt(json['lines_added']),
      linesEdited: _asInt(json['lines_edited']),
      linesDeleted: _asInt(json['lines_deleted']),
      events: events,
      files: files,
    );
  }

  String get summary {
    final labelText = label.isEmpty ? id : label;
    return '$labelText $status | model $modelCalls | tools $toolCalls | files +$fileCreates ~$fileEdits -$fileDeletes | lines +$linesAdded ~$linesEdited -$linesDeleted';
  }
}

class AgentStatus {
  final int activeAgents;
  final int totalListed;
  final int tokensIn;
  final int tokensOut;
  final List<AgentActivity> agents;
  final List<String> events;

  const AgentStatus({
    required this.activeAgents,
    required this.totalListed,
    required this.tokensIn,
    required this.tokensOut,
    required this.agents,
    required this.events,
  });

  factory AgentStatus.fromJson(Map<String, dynamic> json) {
    final agents = (json['agents'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(AgentActivity.fromJson)
        .toList();
    final events = (json['events'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map((e) {
          final id = e['agent_id']?.toString() ?? '';
          final msg = e['message']?.toString() ?? '';
          return id.isEmpty ? msg : '$id: $msg';
        })
        .where((s) => s.trim().isNotEmpty)
        .toList();
    return AgentStatus(
      activeAgents: _asInt(json['active_agents']),
      totalListed: _asInt(json['total_listed']),
      tokensIn: _asInt(json['tokens_in']),
      tokensOut: _asInt(json['tokens_out']),
      agents: agents,
      events: events,
    );
  }
}

class AgentActivity {
  final String id;
  final String role;
  final String status;
  final String activity;
  final String task;
  final String summary;
  final int toolCalls;
  final int tokensIn;
  final int tokensOut;

  const AgentActivity({
    required this.id,
    required this.role,
    required this.status,
    required this.activity,
    required this.task,
    required this.summary,
    required this.toolCalls,
    required this.tokensIn,
    required this.tokensOut,
  });

  factory AgentActivity.fromJson(Map<String, dynamic> json) {
    return AgentActivity(
      id: json['id']?.toString() ?? '',
      role: json['role']?.toString() ?? '',
      status: json['status']?.toString() ?? '',
      activity: json['activity']?.toString() ?? '',
      task: json['task']?.toString() ?? '',
      summary: json['summary']?.toString() ?? '',
      toolCalls: _asInt(json['tool_calls']),
      tokensIn: _asInt(json['tokens_in']),
      tokensOut: _asInt(json['tokens_out']),
    );
  }
}

class ContextHealth {
  final String session;
  final String project;
  final String title;
  final String status;
  final int contextLimit;
  final int nativeContextLimit;
  final String contextMode;
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
    required this.nativeContextLimit,
    required this.contextMode,
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
      nativeContextLimit: _asInt(json['native_context_limit']),
      contextMode: json['context_mode']?.toString() ?? 'native',
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
      'native  ~$nativeContextLimit tokens ($contextMode mode)',
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
