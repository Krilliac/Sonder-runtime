import 'dart:convert';
import 'package:http/http.dart' as http;

import 'models.dart';

class LauncherStatus {
  final bool ok;
  final String launcher;
  final bool serverRunning;
  final String serverHost;
  final int serverPort;
  final String lastAction;
  final String lastError;
  final String message;

  const LauncherStatus({
    required this.ok,
    required this.launcher,
    required this.serverRunning,
    required this.serverHost,
    required this.serverPort,
    required this.lastAction,
    required this.lastError,
    this.message = '',
  });

  factory LauncherStatus.fromJson(Map<String, dynamic> json) =>
      LauncherStatus(
        ok: json['ok'] == true,
        launcher: json['launcher']?.toString() ?? '',
        serverRunning: json['server_running'] == true,
        serverHost: json['server_host']?.toString() ?? '',
        serverPort: _asInt(json['server_port']),
        lastAction: json['last_action']?.toString() ?? '',
        lastError: json['last_error']?.toString() ?? '',
        message: json['message']?.toString() ?? '',
      );
}

class TrilobiteLauncherApi {
  static const _actions = {'start', 'stop', 'restart'};

  final String baseUrl;
  final String token;

  const TrilobiteLauncherApi({required this.baseUrl, required this.token});

  Uri _uri(String path) {
    final base = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$base$path');
  }

  Map<String, String> _headers() => {
        'Accept': 'application/json',
        if (token.trim().isNotEmpty)
          'Authorization': 'Bearer ${token.trim()}',
      };

  LauncherStatus _decode(http.Response response) {
    Map<String, dynamic> body;
    try {
      body = jsonDecode(utf8.decode(response.bodyBytes))
          as Map<String, dynamic>;
    } catch (_) {
      throw TrilobiteException('Could not parse host launcher response.');
    }
    if (response.statusCode == 401) {
      throw TrilobiteException('Host launcher authentication failed.');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw TrilobiteException(
        body['error']?.toString() ??
            body['message']?.toString() ??
            'Host launcher returned HTTP ${response.statusCode}.',
      );
    }
    return LauncherStatus.fromJson(body);
  }

  Future<LauncherStatus> status() async {
    if (baseUrl.trim().isEmpty) {
      throw TrilobiteException('Host launcher URL is not configured.');
    }
    try {
      final response = await http
          .get(_uri('/v1/launcher/status'), headers: _headers())
          .timeout(const Duration(seconds: 8));
      return _decode(response);
    } catch (error) {
      if (error is TrilobiteException) rethrow;
      throw TrilobiteException('Cannot reach host launcher: $error');
    }
  }

  Future<LauncherStatus> action(
    String action, {
    String contextSize = '8192',
  }) async {
    if (!_actions.contains(action)) {
      throw TrilobiteException('Unsupported host launcher action.');
    }
    if (baseUrl.trim().isEmpty) {
      throw TrilobiteException('Host launcher URL is not configured.');
    }
    try {
      final response = await http
          .post(
            _uri('/v1/launcher/$action'),
            headers: {..._headers(), 'Content-Type': 'application/json'},
            body: jsonEncode({'context_size': contextSize}),
          )
          .timeout(const Duration(seconds: 45));
      return _decode(response);
    } catch (error) {
      if (error is TrilobiteException) rethrow;
      throw TrilobiteException('Host launcher request failed: $error');
    }
  }
}

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

  static final RegExp _relativeLocationIntent = RegExp(
    r'\b(near me|nearby|my area|around me|where am i|my location|locate me)\b',
    caseSensitive: false,
  );
  static final RegExp _weatherIntent = RegExp(
    r'\b(weather|forecast|temperature|rain(?:ing)?|snow(?:ing)?|humidity|wind chill)\b',
    caseSensitive: false,
  );
  static final RegExp _postalCode = RegExp(r'(?<!\d)\d{5}(?:-\d{4})?(?!\d)');
  static final RegExp _weatherLocationSuffix = RegExp(
    r'\b(?:in|for|near|around)\s+(.+)$',
    caseSensitive: false,
  );
  static final RegExp _weatherLocationPrefix = RegExp(
    r'^\s*(.{2,80}?)\s+(?:weather|forecast|temperature)\b',
    caseSensitive: false,
  );
  static final RegExp _webCapability = RegExp(
    r'\b(internet|web|online|browser|tools?)\b',
    caseSensitive: false,
  );

  bool _promptNeedsApproximateLocation(String prompt) {
    if (_relativeLocationIntent.hasMatch(prompt)) return true;
    if (!_weatherIntent.hasMatch(prompt)) return false;
    if (_postalCode.hasMatch(prompt)) return false;

    final suffix = _weatherLocationSuffix.firstMatch(prompt)?.group(1)?.trim();
    if (suffix != null && suffix.isNotEmpty) {
      final normalized = suffix.toLowerCase();
      const relativePlaces = <String>{
        'here',
        'home',
        'me',
        'my area',
        'my city',
        'my location',
        'near me',
        'around me',
        'current location',
      };
      if (!relativePlaces.contains(normalized)) return false;
    }

    final prefix = _weatherLocationPrefix.firstMatch(prompt)?.group(1)?.trim();
    if (prefix != null && prefix.isNotEmpty) {
      final normalized = prefix.toLowerCase();
      if (!RegExp(r'\b(what|how|tell|show|check|current)\b')
          .hasMatch(normalized)) {
        return false;
      }
    }
    return true;
  }

  bool _needsApproximateLocation(List<ChatMessage> messages) {
    final userMessages = messages
        .where((message) => message.role == Role.user && !message.pending)
        .map((message) => message.content)
        .toList();
    if (userMessages.isEmpty) return false;
    final current = userMessages.last;
    if (_promptNeedsApproximateLocation(current)) return true;
    return _webCapability.hasMatch(current) &&
        userMessages
            .take(userMessages.length - 1)
            .toList()
            .reversed
            .take(4)
            .any(_promptNeedsApproximateLocation);
  }

  Future<Map<String, dynamic>?> _discoverApproximateLocation() async {
    const fields =
        'success,message,country,country_code,region,region_code,city,'
        'timezone';
    try {
      final response = await http
          .get(Uri.parse('https://ipwho.is/?fields=$fields'))
          .timeout(const Duration(seconds: 10));
      if (response.statusCode != 200) return null;
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is! Map<String, dynamic> || decoded['success'] == false) {
        return null;
      }
      const allowed = <String>{
        'success',
        'country',
        'country_code',
        'region',
        'region_code',
        'city',
        'timezone',
      };
      final minimized = <String, dynamic>{};
      for (final entry in decoded.entries) {
        if (!allowed.contains(entry.key)) continue;
        var value = entry.value;
        if (entry.key == 'timezone' && value is Map) {
          value = value['id'] ?? value['name'];
        }
        if (value is String || value is bool) {
          minimized[entry.key] = value;
        }
      }
      return minimized;
    } catch (_) {
      return null;
    }
  }

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
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
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
    bool allowApproximateLocation = false,
  }) async {
    final locationHint =
        allowApproximateLocation && _needsApproximateLocation(messages)
            ? await _discoverApproximateLocation()
            : null;
    final body = jsonEncode({
      'model': model,
      'context_size': contextSize,
      if (sessionId.trim().isNotEmpty) 'session': sessionId.trim(),
      if (project.trim().isNotEmpty) 'project': project.trim(),
      'location_consent': allowApproximateLocation,
      if (locationHint != null) 'location_hint': locationHint,
      'messages':
          messages.where((m) => !m.pending).map((m) => m.toWire()).toList(),
      'stream': false,
    });

    late http.Response resp;
    String warning = '';
    try {
      resp = await http
          .post(_uri('/v1/chat/completions'), headers: _headers(), body: body)
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
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
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
      throw TrilobiteException(
          obj['message']?.toString() ?? 'Account request failed.');
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
  final AutopilotStatus? autopilot;
  final RuntimePolicyInfo? runtimePolicy;
  final SelfmodInfo? selfmod;
  final McpRuntimeInfo? mcpRuntime;
  final LearningHealthInfo? learningHealth;
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
    required this.autopilot,
    this.runtimePolicy,
    this.selfmod,
    this.mcpRuntime,
    this.learningHealth,
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
      autopilot: json['autopilot'] is Map<String, dynamic>
          ? AutopilotStatus.fromJson(json['autopilot'] as Map<String, dynamic>)
          : null,
      runtimePolicy: json['runtime_policy'] is Map<String, dynamic>
          ? RuntimePolicyInfo.fromJson(
              json['runtime_policy'] as Map<String, dynamic>,
            )
          : null,
      selfmod: json['selfmod'] is Map<String, dynamic>
          ? SelfmodInfo.fromJson(json['selfmod'] as Map<String, dynamic>)
          : null,
      mcpRuntime: json['mcp_runtime'] is Map<String, dynamic>
          ? McpRuntimeInfo.fromJson(
              json['mcp_runtime'] as Map<String, dynamic>,
            )
          : null,
      learningHealth: json['learning_health'] is Map<String, dynamic>
          ? LearningHealthInfo.fromJson(
              json['learning_health'] as Map<String, dynamic>,
            )
          : null,
      activity: json['activity'] is Map<String, dynamic>
          ? ActivityStatus.fromJson(json['activity'] as Map<String, dynamic>)
          : null,
      models: models,
    );
  }
}

class SelfmodInfo {
  final bool enabled;
  final String mode;
  final int active;
  final int deployed;
  final int rollbackPoints;
  final String backupRoot;
  final List<Map<String, dynamic>> runs;

  const SelfmodInfo({
    required this.enabled,
    required this.mode,
    required this.active,
    required this.deployed,
    required this.rollbackPoints,
    required this.backupRoot,
    required this.runs,
  });

  factory SelfmodInfo.fromJson(Map<String, dynamic> json) => SelfmodInfo(
        enabled: json['enabled'] == true,
        mode: json['mode']?.toString() ?? 'propose',
        active: _asInt(json['active']),
        deployed: _asInt(json['deployed']),
        rollbackPoints: _asInt(json['rollback_points']),
        backupRoot: json['backup_root']?.toString() ?? '',
        runs: (json['runs'] as List? ?? const [])
            .whereType<Map<String, dynamic>>()
            .toList(),
      );
}

class RuntimePolicyInfo {
  final int revision;
  final int updatedTs;
  final String path;
  final String source;
  final String error;
  final String inventoryError;
  final Map<String, String> localModels;
  final Map<String, String> routing;
  final List<String> missingModels;

  const RuntimePolicyInfo({
    required this.revision,
    required this.updatedTs,
    required this.path,
    required this.source,
    required this.error,
    required this.inventoryError,
    required this.localModels,
    required this.routing,
    required this.missingModels,
  });

  factory RuntimePolicyInfo.fromJson(Map<String, dynamic> json) {
    return RuntimePolicyInfo(
      revision: _asInt(json['revision']),
      updatedTs: _asInt(json['updated_ts']),
      path: json['path']?.toString() ?? '',
      source: json['source']?.toString() ?? '',
      error: json['error']?.toString() ?? '',
      inventoryError: json['inventory_error']?.toString() ?? '',
      localModels: _stringMap(json['local_models']),
      routing: _stringMap(json['routing']),
      missingModels: (json['missing_models'] as List? ?? const [])
          .map((value) => value.toString())
          .where((value) => value.isNotEmpty)
          .toList(growable: false),
    );
  }

  bool get hasWarning =>
      error.isNotEmpty || inventoryError.isNotEmpty || missingModels.isNotEmpty;

  String modelForLane(String lane) {
    final tier = routing[lane] ?? '';
    return localModels[tier] ?? '';
  }
}

class McpRuntimeInfo {
  final String status;
  final bool enabled;
  final String module;
  final String path;
  final String loadedDigest;
  final String currentDigest;
  final bool sourceChanged;
  final int registeredTools;
  final int refreshCount;
  final int lastRefreshTs;
  final bool lastSurfaceChanged;
  final String lastError;
  final String lastNotificationError;
  final bool protocolListChanged;

  const McpRuntimeInfo({
    required this.status,
    required this.enabled,
    required this.module,
    required this.path,
    required this.loadedDigest,
    required this.currentDigest,
    required this.sourceChanged,
    required this.registeredTools,
    required this.refreshCount,
    required this.lastRefreshTs,
    required this.lastSurfaceChanged,
    required this.lastError,
    required this.lastNotificationError,
    required this.protocolListChanged,
  });

  factory McpRuntimeInfo.fromJson(Map<String, dynamic> json) {
    return McpRuntimeInfo(
      status: json['status']?.toString() ?? 'unknown',
      enabled: _asBool(json['enabled']),
      module: json['module']?.toString() ?? '',
      path: json['path']?.toString() ?? '',
      loadedDigest: json['loaded_digest']?.toString() ?? '',
      currentDigest: json['current_digest']?.toString() ?? '',
      sourceChanged: _asBool(json['source_changed']),
      registeredTools: _asInt(json['registered_tools']),
      refreshCount: _asInt(json['refresh_count']),
      lastRefreshTs: _asInt(json['last_refresh_ts']),
      lastSurfaceChanged: _asBool(json['last_surface_changed']),
      lastError: json['last_error']?.toString() ?? '',
      lastNotificationError: json['last_notification_error']?.toString() ?? '',
      protocolListChanged: _asBool(json['protocol_list_changed']),
    );
  }

  bool get hasWarning =>
      sourceChanged || lastError.isNotEmpty || lastNotificationError.isNotEmpty;

  String get loadedShort =>
      loadedDigest.length <= 12 ? loadedDigest : loadedDigest.substring(0, 12);

  String get currentShort => currentDigest.length <= 12
      ? currentDigest
      : currentDigest.substring(0, 12);
}

class LearningHealthInfo {
  final String status;
  final int interactions;
  final int outcomes;
  final int outcomeInteractions;
  final int goodOutcomes;
  final int badOutcomes;
  final double outcomeCoveragePercent;
  final double positivePercent;
  final int lessons;
  final int facts;
  final int groundedLessons;
  final int syntheticLessons;
  final double lessonsPerInteraction;
  final double? distillationYield;
  final Map<String, int> lessonSources;
  final List<LearningSignalInfo> signals;
  final LearningQualityInfo quality;

  const LearningHealthInfo({
    required this.status,
    required this.interactions,
    required this.outcomes,
    required this.outcomeInteractions,
    required this.goodOutcomes,
    required this.badOutcomes,
    required this.outcomeCoveragePercent,
    required this.positivePercent,
    required this.lessons,
    required this.facts,
    required this.groundedLessons,
    required this.syntheticLessons,
    required this.lessonsPerInteraction,
    required this.distillationYield,
    required this.lessonSources,
    required this.signals,
    required this.quality,
  });

  factory LearningHealthInfo.fromJson(Map<String, dynamic> json) {
    return LearningHealthInfo(
      status: json['status']?.toString() ?? 'unknown',
      interactions: _asInt(json['interactions']),
      outcomes: _asInt(json['outcomes']),
      outcomeInteractions: _asInt(json['outcome_interactions']),
      goodOutcomes: _asInt(json['good_outcomes']),
      badOutcomes: _asInt(json['bad_outcomes']),
      outcomeCoveragePercent: _asDouble(json['outcome_coverage_percent']),
      positivePercent: _asDouble(json['positive_percent']),
      lessons: _asInt(json['lessons']),
      facts: _asInt(json['facts']),
      groundedLessons: _asInt(json['grounded_lessons']),
      syntheticLessons: _asInt(json['synthetic_lessons']),
      lessonsPerInteraction: _asDouble(json['lessons_per_interaction']),
      distillationYield: json['distillation_yield'] == null
          ? null
          : _asDouble(json['distillation_yield']),
      lessonSources: _intMap(json['lesson_sources']),
      signals: (json['signals'] as List? ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(LearningSignalInfo.fromJson)
          .toList(growable: false),
      quality: json['quality'] is Map<String, dynamic>
          ? LearningQualityInfo.fromJson(
              json['quality'] as Map<String, dynamic>,
            )
          : const LearningQualityInfo.empty(),
    );
  }

  bool get hasWarning => status == 'attention' || status == 'watch';
}

class LearningSignalInfo {
  final String signal;
  final int count;
  final double averageReward;
  final bool good;

  const LearningSignalInfo({
    required this.signal,
    required this.count,
    required this.averageReward,
    required this.good,
  });

  factory LearningSignalInfo.fromJson(Map<String, dynamic> json) {
    return LearningSignalInfo(
      signal: json['signal']?.toString() ?? '',
      count: _asInt(json['count']),
      averageReward: _asDouble(json['average_reward']),
      good: _asBool(json['good']),
    );
  }
}

class LearningQualityInfo {
  final int duplicateGroups;
  final int duplicateRows;
  final int missingEmbeddings;
  final int vagueLessons;
  final int privacyFlags;
  final int missingSources;
  final int missingFts;
  final int orphanFts;
  final double embeddingPercent;

  const LearningQualityInfo({
    required this.duplicateGroups,
    required this.duplicateRows,
    required this.missingEmbeddings,
    required this.vagueLessons,
    required this.privacyFlags,
    required this.missingSources,
    required this.missingFts,
    required this.orphanFts,
    required this.embeddingPercent,
  });

  const LearningQualityInfo.empty()
      : duplicateGroups = 0,
        duplicateRows = 0,
        missingEmbeddings = 0,
        vagueLessons = 0,
        privacyFlags = 0,
        missingSources = 0,
        missingFts = 0,
        orphanFts = 0,
        embeddingPercent = 0;

  factory LearningQualityInfo.fromJson(Map<String, dynamic> json) {
    return LearningQualityInfo(
      duplicateGroups: _asInt(json['exact_duplicate_groups']),
      duplicateRows: _asInt(json['exact_duplicate_prunable']),
      missingEmbeddings: _asInt(json['no_embedding']),
      vagueLessons: _asInt(json['vague_without_anchor']),
      privacyFlags: _asInt(json['path_or_secret_like']),
      missingSources: _asInt(json['missing_source_interaction']),
      missingFts: _asInt(json['missing_fts']),
      orphanFts: _asInt(json['orphan_fts']),
      embeddingPercent: _asDouble(json['embedding_percent']),
    );
  }

  int get issueCount =>
      duplicateRows +
      missingEmbeddings +
      vagueLessons +
      privacyFlags +
      missingSources +
      missingFts +
      orphanFts;
}

class AutopilotStatus {
  final int activeRuns;
  final int resumableRuns;
  final int totalRuns;
  final int totalListed;
  final String database;
  final List<AutopilotRun> runs;
  final AutopilotRun? latest;
  final List<AutopilotEvent> events;

  const AutopilotStatus({
    required this.activeRuns,
    required this.resumableRuns,
    required this.totalRuns,
    required this.totalListed,
    required this.database,
    required this.runs,
    required this.latest,
    required this.events,
  });

  factory AutopilotStatus.fromJson(Map<String, dynamic> json) {
    final runs = (json['runs'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(AutopilotRun.fromJson)
        .toList();
    return AutopilotStatus(
      activeRuns: _asInt(json['active_runs']),
      resumableRuns: _asInt(json['resumable_runs']),
      totalRuns: json.containsKey('total_runs')
          ? _asInt(json['total_runs'])
          : _asInt(json['total_listed']),
      totalListed: _asInt(json['total_listed']),
      database: json['database']?.toString() ?? '',
      runs: runs,
      latest: json['latest'] is Map<String, dynamic>
          ? AutopilotRun.fromJson(json['latest'] as Map<String, dynamic>)
          : (runs.isEmpty ? null : runs.first),
      events: (json['events'] as List? ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(AutopilotEvent.fromJson)
          .toList(),
    );
  }
}

class AutopilotRun {
  final String id;
  final String objective;
  final String project;
  final String tier;
  final String policy;
  final bool allowWeb;
  final String status;
  final String phase;
  final int cycles;
  final int failures;
  final int checkpoints;
  final int replans;
  final int maxFailures;
  final int maxTasks;
  final int maxReplans;
  final bool adaptive;
  final String summary;
  final String finalReport;
  final String lastError;
  final List<String> criteria;
  final List<AutopilotTask> tasks;

  const AutopilotRun({
    required this.id,
    required this.objective,
    required this.project,
    required this.tier,
    required this.policy,
    required this.allowWeb,
    required this.status,
    required this.phase,
    required this.cycles,
    required this.failures,
    required this.checkpoints,
    required this.replans,
    required this.maxFailures,
    required this.maxTasks,
    required this.maxReplans,
    required this.adaptive,
    required this.summary,
    required this.finalReport,
    required this.lastError,
    required this.criteria,
    required this.tasks,
  });

  factory AutopilotRun.fromJson(Map<String, dynamic> json) {
    return AutopilotRun(
      id: json['id']?.toString() ?? '',
      objective: json['objective']?.toString() ?? '',
      project: json['project']?.toString() ?? '',
      tier: json['tier']?.toString() ?? '',
      policy: json['policy']?.toString() ?? '',
      allowWeb: json['allow_web'] is bool
          ? json['allow_web'] as bool
          : _asInt(json['allow_web']) != 0,
      status: json['status']?.toString() ?? '',
      phase: json['phase']?.toString() ?? '',
      cycles: _asInt(json['cycles']),
      failures: _asInt(json['failures']),
      checkpoints: _asInt(json['checkpoints']),
      replans: _asInt(json['replans']),
      maxFailures: _asInt(json['max_failures']),
      maxTasks: _asInt(json['max_tasks']),
      maxReplans:
          json.containsKey('max_replans') ? _asInt(json['max_replans']) : 2,
      adaptive: json.containsKey('adaptive')
          ? (json['adaptive'] is bool
              ? json['adaptive'] as bool
              : _asInt(json['adaptive']) != 0)
          : true,
      summary: json['summary']?.toString() ?? '',
      finalReport: json['final_report']?.toString() ?? '',
      lastError: json['last_error']?.toString() ?? '',
      criteria: (json['criteria'] as List? ?? const [])
          .map((value) => value.toString())
          .toList(),
      tasks: (json['plan'] as List? ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(AutopilotTask.fromJson)
          .toList(),
    );
  }

  bool get isActive => status == 'planning' || status == 'running';
  bool get isResumable => const {
        'ready',
        'paused',
        'blocked',
        'interrupted',
      }.contains(status);
  bool get isTerminal => const {
        'completed',
        'failed',
        'cancelled',
      }.contains(status);
}

class AutopilotTask {
  final String id;
  final String title;
  final String instruction;
  final String kind;
  final String status;
  final int attempts;
  final String output;
  final String error;

  const AutopilotTask({
    required this.id,
    required this.title,
    required this.instruction,
    required this.kind,
    required this.status,
    required this.attempts,
    required this.output,
    required this.error,
  });

  factory AutopilotTask.fromJson(Map<String, dynamic> json) {
    return AutopilotTask(
      id: json['id']?.toString() ?? '',
      title: json['title']?.toString() ?? '',
      instruction: json['instruction']?.toString() ?? '',
      kind: json['kind']?.toString() ?? '',
      status: json['status']?.toString() ?? 'pending',
      attempts: _asInt(json['attempts']),
      output: json['output']?.toString() ?? '',
      error: json['error']?.toString() ?? '',
    );
  }
}

class AutopilotEvent {
  final int id;
  final String kind;
  final String message;

  const AutopilotEvent({
    required this.id,
    required this.kind,
    required this.message,
  });

  factory AutopilotEvent.fromJson(Map<String, dynamic> json) {
    return AutopilotEvent(
      id: _asInt(json['event_id']),
      kind: json['kind']?.toString() ?? '',
      message: json['message']?.toString() ?? '',
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
  final List<ActivityEvent> actions;
  final List<ActivityChecklistItem> checklist;
  final String checklistTitle;
  final String checklistStatus;
  final String resultSummary;

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
    required this.actions,
    required this.checklist,
    required this.checklistTitle,
    required this.checklistStatus,
    required this.resultSummary,
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
    final actions = (json['events'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .where((event) => event['kind']?.toString() == 'tool_call')
        .map(ActivityEvent.fromJson)
        .toList();
    final checklistJson = json['checklist'] is Map<String, dynamic>
        ? json['checklist'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final checklist = (checklistJson['items'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ActivityChecklistItem.fromJson)
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
      actions: actions,
      checklist: checklist,
      checklistTitle: checklistJson['title']?.toString() ?? '',
      checklistStatus: checklistJson['status']?.toString() ?? '',
      resultSummary: json['result_summary']?.toString() ?? '',
    );
  }

  String get summary {
    final labelText = label.isEmpty ? id : label;
    return '$labelText $status | model $modelCalls | tools $toolCalls | files +$fileCreates ~$fileEdits -$fileDeletes | lines +$linesAdded ~$linesEdited -$linesDeleted';
  }
}

class ActivityEvent {
  final String kind;
  final String tool;
  final String title;
  final String command;
  final String output;
  final String summary;
  final int elapsedMs;
  final bool ok;

  const ActivityEvent({
    required this.kind,
    required this.tool,
    required this.title,
    required this.command,
    required this.output,
    required this.summary,
    required this.elapsedMs,
    required this.ok,
  });

  factory ActivityEvent.fromJson(Map<String, dynamic> json) {
    final tool = json['tool']?.toString() ?? '';
    final fallbackTitle = tool
        .split('_')
        .where((part) => part.isNotEmpty)
        .map((part) => '${part[0].toUpperCase()}${part.substring(1)}')
        .join(' ');
    return ActivityEvent(
      kind: json['kind']?.toString() ?? '',
      tool: tool,
      title: json['title']?.toString().trim().isNotEmpty == true
          ? json['title'].toString()
          : fallbackTitle,
      command: json['command']?.toString() ?? '',
      output: json['output']?.toString() ?? '',
      summary: json['summary']?.toString() ?? '',
      elapsedMs: _asInt(json['elapsed_ms']),
      ok: json['ok'] is bool ? json['ok'] as bool : true,
    );
  }

  String get evidence {
    final lines = <String>[command, output.isNotEmpty ? output : summary]
        .where((value) => value.trim().isNotEmpty)
        .toList();
    return lines.join('\n');
  }
}

class ActivityChecklistItem {
  final String id;
  final String title;
  final String status;

  const ActivityChecklistItem({
    required this.id,
    required this.title,
    required this.status,
  });

  factory ActivityChecklistItem.fromJson(Map<String, dynamic> json) {
    return ActivityChecklistItem(
      id: json['id']?.toString() ?? '',
      title: json['title']?.toString() ?? '',
      status: json['status']?.toString() ?? 'pending',
    );
  }
}

class AgentStatus {
  final int activeAgents;
  final int cancelPending;
  final int interruptedAgents;
  final int totalAgents;
  final int totalListed;
  final int tokensIn;
  final int tokensOut;
  final List<AgentActivity> agents;
  final List<String> events;
  final AgentCapacity? capacity;

  const AgentStatus({
    required this.activeAgents,
    required this.cancelPending,
    required this.interruptedAgents,
    required this.totalAgents,
    required this.totalListed,
    required this.tokensIn,
    required this.tokensOut,
    required this.agents,
    required this.events,
    required this.capacity,
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
    final totalListed = _asInt(json['total_listed']);
    return AgentStatus(
      activeAgents: _asInt(json['active_agents']),
      cancelPending: _asInt(json['cancel_pending']),
      interruptedAgents: _asInt(json['interrupted_agents']),
      totalAgents: json.containsKey('total_agents')
          ? _asInt(json['total_agents'])
          : totalListed,
      totalListed: totalListed,
      tokensIn: _asInt(json['tokens_in']),
      tokensOut: _asInt(json['tokens_out']),
      agents: agents,
      events: events,
      capacity: json['capacity'] is Map<String, dynamic>
          ? AgentCapacity.fromJson(json['capacity'] as Map<String, dynamic>)
          : null,
    );
  }
}

class AgentCapacity {
  final int logicalCpus;
  final int agentCeiling;
  final int workerSlots;
  final int automaticWorkerSlots;
  final int totalMemoryBytes;
  final int availableMemoryBytes;
  final String source;

  const AgentCapacity({
    required this.logicalCpus,
    required this.agentCeiling,
    required this.workerSlots,
    required this.automaticWorkerSlots,
    required this.totalMemoryBytes,
    required this.availableMemoryBytes,
    required this.source,
  });

  factory AgentCapacity.fromJson(Map<String, dynamic> json) {
    return AgentCapacity(
      logicalCpus: _asInt(json['logical_cpus']),
      agentCeiling: _asInt(json['agent_ceiling']),
      workerSlots: _asInt(json['worker_slots']),
      automaticWorkerSlots: _asInt(json['automatic_worker_slots']),
      totalMemoryBytes: _asInt(json['total_memory_bytes']),
      availableMemoryBytes: _asInt(json['available_memory_bytes']),
      source: json['source']?.toString() ?? 'auto',
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

bool _asBool(Object? value) {
  if (value is bool) return value;
  if (value is num) return value != 0;
  return const {'1', 'true', 'yes', 'on'}
      .contains(value?.toString().trim().toLowerCase());
}

Map<String, String> _stringMap(Object? value) {
  if (value is! Map) return const {};
  return Map.unmodifiable({
    for (final entry in value.entries)
      entry.key.toString(): entry.value?.toString() ?? '',
  });
}

Map<String, int> _intMap(Object? value) {
  if (value is! Map) return const {};
  return Map.unmodifiable({
    for (final entry in value.entries)
      entry.key.toString(): _asInt(entry.value),
  });
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
