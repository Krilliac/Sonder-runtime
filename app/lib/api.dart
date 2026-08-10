import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:http/http.dart' as http;

import 'models.dart';

class LauncherOperation {
  static const terminalPhases = {
    'succeeded',
    'failed',
    'cancelled',
    'interrupted',
  };

  final String id;
  final String action;
  final String phase;
  final String message;
  final String lastError;

  const LauncherOperation({
    required this.id,
    required this.action,
    required this.phase,
    this.message = '',
    this.lastError = '',
  });

  factory LauncherOperation.fromJson(Map<String, dynamic> json) =>
      LauncherOperation(
        id: json['id']?.toString() ?? '',
        action: json['action']?.toString() ?? '',
        phase: json['phase']?.toString().trim().toLowerCase() ?? '',
        message: json['message']?.toString() ?? '',
        lastError: json['last_error']?.toString() ?? '',
      );

  bool get isTerminal => terminalPhases.contains(phase);
  bool get succeeded => phase == 'succeeded';

  String get displayMessage {
    if (lastError.isNotEmpty) return lastError;
    if (message.isNotEmpty) return message;
    if (action.isEmpty || phase.isEmpty) return 'Host operation is pending.';
    return 'Host $action is $phase.';
  }
}

class LauncherStatus {
  final bool ok;
  final String launcher;
  final bool serverRunning;
  final String serverState;
  final String serverHost;
  final int serverPort;
  final String lastAction;
  final String lastError;
  final String message;
  final LauncherOperation? operation;
  final LauncherOperation? activeOperation;

  const LauncherStatus({
    required this.ok,
    required this.launcher,
    required this.serverRunning,
    required this.serverState,
    required this.serverHost,
    required this.serverPort,
    required this.lastAction,
    required this.lastError,
    this.message = '',
    this.operation,
    this.activeOperation,
  });

  factory LauncherStatus.fromJson(Map<String, dynamic> json) {
    final operationJson = json['operation'];
    final activeOperationJson = json['active_operation'];
    LauncherOperation? operation;
    if (operationJson is Map) {
      operation = LauncherOperation.fromJson(
        Map<String, dynamic>.from(operationJson),
      );
    } else if ((json['operation_id']?.toString() ?? '').isNotEmpty) {
      // Tolerate early async-launcher builds that returned only top-level
      // operation fields while still requiring a concrete operation id.
      operation = LauncherOperation(
        id: json['operation_id'].toString(),
        action: json['operation_action']?.toString() ??
            json['last_action']?.toString() ??
            '',
        phase: json['operation_phase']?.toString().trim().toLowerCase() ?? '',
        message: json['operation_message']?.toString() ??
            json['message']?.toString() ??
            '',
        lastError: json['operation_error']?.toString() ??
            json['last_error']?.toString() ??
            '',
      );
    }
    LauncherOperation? activeOperation;
    if (activeOperationJson is Map) {
      activeOperation = LauncherOperation.fromJson(
        Map<String, dynamic>.from(activeOperationJson),
      );
    }
    final serverRunning = json['server_running'] == true;
    return LauncherStatus(
      ok: json['ok'] == true,
      launcher: json['launcher']?.toString() ?? '',
      serverRunning: serverRunning,
      serverState: json['server_state']?.toString() ??
          (serverRunning ? 'healthy' : 'stopped'),
      serverHost: json['server_host']?.toString() ?? '',
      serverPort: _asInt(json['server_port']),
      lastAction: json['last_action']?.toString() ?? '',
      lastError: json['last_error']?.toString() ?? '',
      message: json['message']?.toString() ?? '',
      operation: operation,
      activeOperation: activeOperation,
    );
  }

  LauncherOperation? get currentOperation => operation ?? activeOperation;
}

class SonderLauncherApi {
  static const _actions = {'start', 'stop', 'restart'};
  static final Random _secureRandom = Random.secure();

  final String baseUrl;
  final String token;

  const SonderLauncherApi({required this.baseUrl, required this.token});

  Uri _uri(String path) {
    final base = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$base$path');
  }

  Map<String, String> _headers() => {
        'Accept': 'application/json',
        if (token.trim().isNotEmpty) 'Authorization': 'Bearer ${token.trim()}',
      };

  static String _newIdempotencyKey() {
    final bytes = List<int>.generate(16, (_) => _secureRandom.nextInt(256));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    final hex = bytes.map((byte) => byte.toRadixString(16).padLeft(2, '0'));
    final value = hex.join();
    return '${value.substring(0, 8)}-${value.substring(8, 12)}-'
        '${value.substring(12, 16)}-${value.substring(16, 20)}-'
        '${value.substring(20)}';
  }

  LauncherStatus _decode(http.Response response) {
    Map<String, dynamic> body;
    try {
      body =
          jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
    } catch (_) {
      throw SonderException('Could not parse host launcher response.');
    }
    if (response.statusCode == 401) {
      throw SonderException('Host launcher authentication failed.');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw SonderException(
        body['error']?.toString() ??
            body['message']?.toString() ??
            'Host launcher returned HTTP ${response.statusCode}.',
      );
    }
    return LauncherStatus.fromJson(body);
  }

  Future<LauncherStatus> status() async {
    if (baseUrl.trim().isEmpty) {
      throw SonderException('Host launcher URL is not configured.');
    }
    try {
      final response = await http
          .get(_uri('/v1/launcher/status'), headers: _headers())
          .timeout(const Duration(seconds: 8));
      return _decode(response);
    } catch (error) {
      if (error is SonderException) rethrow;
      throw SonderException('Cannot reach host launcher: $error');
    }
  }

  Future<LauncherStatus> action(
    String action, {
    String contextSize = '8192',
    String? idempotencyKey,
    Duration maxWait = const Duration(minutes: 30),
    Duration pollInterval = const Duration(seconds: 1),
    void Function(LauncherStatus status)? onProgress,
    bool Function()? isCancelled,
  }) async {
    if (!_actions.contains(action)) {
      throw SonderException('Unsupported host launcher action.');
    }
    if (baseUrl.trim().isEmpty) {
      throw SonderException('Host launcher URL is not configured.');
    }
    final requestKey = (idempotencyKey ?? _newIdempotencyKey()).trim();
    if (!RegExp(r'^[A-Za-z0-9._:-]{8,128}$').hasMatch(requestKey)) {
      throw SonderException('Invalid host launcher idempotency key.');
    }
    try {
      final response = await http
          .post(
            _uri('/v1/launcher/$action'),
            headers: {
              ..._headers(),
              'Content-Type': 'application/json',
              'Idempotency-Key': requestKey,
            },
            body: jsonEncode({'context_size': contextSize}),
          )
          .timeout(const Duration(seconds: 8));
      final accepted = _decode(response);
      if (response.statusCode != 202) return accepted;

      final operation = accepted.operation;
      if (operation == null || operation.id.isEmpty) {
        throw SonderException(
          'Host launcher accepted the request without an operation ID.',
        );
      }
      onProgress?.call(accepted);
      return waitForOperation(
        operation.id,
        maxWait: maxWait,
        pollInterval: pollInterval,
        onProgress: onProgress,
        isCancelled: isCancelled,
      );
    } catch (error) {
      if (error is SonderException) rethrow;
      if (error is TimeoutException) {
        throw SonderException(
          'Host launcher did not acknowledge the action in time. It may have '
          'accepted it; refresh Host Launcher status before retrying.',
        );
      }
      throw SonderException('Host launcher request failed: $error');
    }
  }

  Future<LauncherStatus> waitForOperation(
    String operationId, {
    Duration maxWait = const Duration(minutes: 30),
    Duration pollInterval = const Duration(seconds: 1),
    void Function(LauncherStatus status)? onProgress,
    bool Function()? isCancelled,
  }) async {
    if (baseUrl.trim().isEmpty) {
      throw SonderException('Host launcher URL is not configured.');
    }
    if (operationId.trim().isEmpty) {
      throw SonderException('Host launcher operation ID is missing.');
    }
    final stopwatch = Stopwatch()..start();
    final boundedWait = maxWait.isNegative ? Duration.zero : maxWait;
    final boundedInterval =
        pollInterval.isNegative ? Duration.zero : pollInterval;
    String lastPollError = '';
    while (true) {
      if (isCancelled?.call() == true) {
        throw SonderException(
          'Stopped waiting for the host operation. It may still be running; '
          'refresh Host Launcher status before starting another action.',
        );
      }
      if (stopwatch.elapsed >= boundedWait) {
        final detail =
            lastPollError.isEmpty ? '' : ' Last status error: $lastPollError.';
        throw SonderException(
          'Timed out waiting for host operation $operationId. It may still be '
          'running; refresh Host Launcher status before retrying.$detail',
        );
      }
      if (boundedInterval > Duration.zero) {
        final remaining = boundedWait - stopwatch.elapsed;
        await Future<void>.delayed(
          boundedInterval < remaining ? boundedInterval : remaining,
        );
      }
      if (isCancelled?.call() == true) continue;
      if (stopwatch.elapsed >= boundedWait) continue;

      http.Response response;
      try {
        final remaining = boundedWait - stopwatch.elapsed;
        final requestTimeout = remaining < const Duration(seconds: 8)
            ? remaining
            : const Duration(seconds: 8);
        response = await http
            .get(
              _uri(
                '/v1/launcher/operations/'
                '${Uri.encodeComponent(operationId)}',
              ),
              headers: _headers(),
            )
            .timeout(requestTimeout);
      } catch (error) {
        if (error is SonderException) rethrow;
        final detail = error.toString();
        lastPollError =
            detail.length > 240 ? '${detail.substring(0, 240)}…' : detail;
        continue;
      }
      if (response.statusCode >= 500) {
        lastPollError = 'HTTP ${response.statusCode}';
        continue;
      }
      final status = _decode(response);
      lastPollError = '';
      final operation = status.operation;
      if (operation == null || operation.id != operationId) {
        throw SonderException(
          'Host launcher returned a mismatched operation status.',
        );
      }
      onProgress?.call(status);
      if (!operation.isTerminal) continue;
      if (operation.succeeded && status.ok) return status;
      throw SonderException(operation.displayMessage);
    }
  }
}

/// One declared argument of a slash command, as published by the server's
/// unified command catalog (`command_catalog.py`).
class CommandParam {
  final String name;
  final String type;
  final bool required;

  /// Server-supplied default. Left as a raw JSON value because a default can
  /// legitimately be a string, number, bool or null, and rendering it is the
  /// only thing the app does with it.
  final Object? defaultValue;

  const CommandParam({
    required this.name,
    this.type = '',
    this.required = false,
    this.defaultValue,
  });

  factory CommandParam.fromJson(Map<String, dynamic> json) => CommandParam(
        name: json['name']?.toString() ?? '',
        type: json['type']?.toString() ?? '',
        required: json['required'] == true,
        defaultValue: json['default'],
      );

  /// `title: str` / `[project: str = default]` — compact enough for a palette
  /// row while still saying whether the caller has to supply it.
  String get label {
    final typed = type.isEmpty ? name : '$name: $type';
    if (required) return typed;
    final shown = defaultValue == null ? typed : '$typed = $defaultValue';
    return '[$shown]';
  }
}

/// A single slash command in the server's command catalog.
///
/// The Python side (`command_catalog.py`) is the one source of truth for the
/// ~265 commands; this is its wire shape. Anything the server omits degrades
/// to an empty string rather than throwing, so an older server that publishes
/// a partial record still yields a usable palette row.
class SonderCommand {
  final String name;
  final List<String> aliases;
  final String tool;
  final String category;

  /// safe | ask | mutation | dangerous. Free-form on the wire; the UI treats
  /// anything it does not recognise as unlabelled rather than guessing.
  final String risk;
  final String summary;

  /// True when the command is handled by the serve layer itself rather than
  /// by dispatching to a registered tool.
  final bool native;
  final String usage;
  final List<CommandParam> params;

  const SonderCommand({
    required this.name,
    this.aliases = const [],
    this.tool = '',
    this.category = '',
    this.risk = '',
    this.summary = '',
    this.native = false,
    this.usage = '',
    this.params = const [],
  });

  factory SonderCommand.fromJson(Map<String, dynamic> json) {
    final rawAliases = json['aliases'];
    final rawParams = json['params'];
    return SonderCommand(
      name: json['name']?.toString() ?? '',
      aliases: rawAliases is List
          ? rawAliases
              .map((a) => a?.toString() ?? '')
              .where((a) => a.isNotEmpty)
              .toList(growable: false)
          : const [],
      tool: json['tool']?.toString() ?? '',
      category: json['category']?.toString() ?? '',
      risk: json['risk']?.toString().trim().toLowerCase() ?? '',
      summary: json['summary']?.toString() ?? '',
      native: json['native'] == true,
      usage: json['usage']?.toString() ?? '',
      params: rawParams is List
          ? rawParams
              .whereType<Map>()
              .map((p) => CommandParam.fromJson(Map<String, dynamic>.from(p)))
              .toList(growable: false)
          : const [],
    );
  }

  /// The token a user actually types. Fallback entries embed an example
  /// payload in the name (`/asset office-suite DOCX report, …`), so rows show
  /// the leading token and insertion keeps the whole string.
  String get displayName => name.split(' ').first;

  /// The usage line, synthesised from [params] when the server did not send
  /// one, so a row always tells the user what arguments it takes.
  String get usageLine {
    if (usage.trim().isNotEmpty) return usage.trim();
    if (params.isEmpty) return displayName;
    return '$displayName ${params.map((p) => p.label).join(' ')}';
  }

  /// Does this command answer to [query] (a lowercased "/foo" prefix)?
  /// Aliases count, so `/plan` still finds `/task_plan`.
  bool matchesPrefix(String query) {
    if (name.toLowerCase().startsWith(query)) return true;
    for (final alias in aliases) {
      if (alias.toLowerCase().startsWith(query)) return true;
    }
    return false;
  }

  /// Looser second pass: substring anywhere in the name, an alias, the
  /// summary or the category. [needle] is lowercased and has no leading slash.
  bool matchesLoose(String needle) {
    if (needle.isEmpty) return true;
    if (name.toLowerCase().contains(needle)) return true;
    if (summary.toLowerCase().contains(needle)) return true;
    if (category.toLowerCase().contains(needle)) return true;
    for (final alias in aliases) {
      if (alias.toLowerCase().contains(needle)) return true;
    }
    return false;
  }
}

/// The whole published command surface: every command, the category blurbs,
/// and the short "popular" list shown the moment a user types "/".
class CommandCatalog {
  final List<SonderCommand> commands;
  final Map<String, String> categories;
  final List<String> popular;

  const CommandCatalog({
    this.commands = const [],
    this.categories = const {},
    this.popular = const [],
  });

  static const empty = CommandCatalog();

  factory CommandCatalog.fromJson(Map<String, dynamic> json) {
    final rawCommands = json['commands'];
    final rawCategories = json['categories'];
    final rawPopular = json['popular'];
    return CommandCatalog(
      commands: rawCommands is List
          ? rawCommands
              .whereType<Map>()
              .map((c) => SonderCommand.fromJson(Map<String, dynamic>.from(c)))
              .where((c) => c.name.isNotEmpty)
              .toList(growable: false)
          : const [],
      categories: rawCategories is Map
          ? <String, String>{
              for (final entry in rawCategories.entries)
                entry.key.toString(): entry.value?.toString() ?? '',
            }
          : const {},
      popular: rawPopular is List
          ? rawPopular
              .map((p) => p?.toString() ?? '')
              .where((p) => p.isNotEmpty)
              .toList(growable: false)
          : const [],
    );
  }

  bool get isEmpty => commands.isEmpty;

  /// Commands named by [popular], in the server's order, skipping any name the
  /// catalog does not actually define. Falls back to the head of the full list
  /// so typing "/" is never answered with a blank panel.
  List<SonderCommand> get popularCommands {
    final index = <String, SonderCommand>{
      for (final c in commands) c.name.toLowerCase(): c,
    };
    final picked = <SonderCommand>[];
    for (final name in popular) {
      final hit = index[name.toLowerCase()];
      if (hit != null) picked.add(hit);
    }
    if (picked.isNotEmpty) return picked;
    return commands.take(12).toList(growable: false);
  }

  /// Category key -> its commands, ordered by the category blurb map when the
  /// server supplied one so the browser matches the server's own grouping.
  Map<String, List<SonderCommand>> get byCategory {
    final grouped = <String, List<SonderCommand>>{};
    for (final command in commands) {
      final key = command.category.isEmpty ? 'other' : command.category;
      grouped.putIfAbsent(key, () => <SonderCommand>[]).add(command);
    }
    if (categories.isEmpty) return grouped;
    final ordered = <String, List<SonderCommand>>{};
    for (final key in categories.keys) {
      final hit = grouped.remove(key);
      if (hit != null) ordered[key] = hit;
    }
    ordered.addAll(grouped);
    return ordered;
  }
}

/// Thin client for a hosted Sonder Runtime instance (sonder_serve.py).
///
/// The server speaks the OpenAI chat-completions dialect:
///   GET  <base>/v1/models
///   POST <base>/v1/chat/completions   { model, messages[], stream }
/// with an optional `Authorization: Bearer <key>` header when the host
/// enabled auth. This mirrors sonder_client.py, but for a GUI.
class SonderApi {
  final String baseUrl; // e.g. https://sonder.example.com
  final String apiKey; // empty when the server has auth disabled
  final String localFallbackUrl;

  const SonderApi({
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
  /// advertises (typically just ["sonder"]). Throws [SonderException]
  /// on any failure so the UI can show a precise reason.
  Future<List<String>> listModels() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/models'), headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      // A silent local retry made connection tests authenticate a different
      // machine, turning bad URLs and API keys into false green results.
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj = jsonDecode(resp.body) as Map<String, dynamic>;
      final data = (obj['data'] as List?) ?? const [];
      return data
          .map((m) => (m as Map<String, dynamic>)['id']?.toString() ?? '')
          .where((s) => s.isNotEmpty)
          .toList();
    } catch (_) {
      throw SonderException('Unexpected response from server.');
    }
  }

  Future<SystemInfo> systemInfo() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/sonder/status'), headers: _headers())
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      // Status must stay bound to the configured host; otherwise polling can
      // silently replace a remote machine's diagnostics with this laptop's.
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      return SystemInfo.fromJson(obj);
    } catch (_) {
      throw SonderException('Could not parse system status.');
    }
  }

  /// Durable update state for the System page (SPEC-4 section 14).
  ///
  /// Admin-only on the server; a non-admin key gets 403 and the UI simply
  /// hides the update section rather than treating it as an error.
  Future<UpdateStatus?> fetchUpdateStatus() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/admin/updates/status'), headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 403 || resp.statusCode == 404) {
      // Not authorized for update control, or the route is unavailable on
      // this build: no update section, not a failure.
      return null;
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      return UpdateStatus.fromJson(obj);
    } catch (_) {
      throw SonderException('Could not parse update status.');
    }
  }

  /// The server's whole published command surface (GET /v1/commands).
  ///
  /// Fetched once and cached by the caller: the catalog is a few hundred
  /// entries, so filtering it client-side per keystroke costs nothing and a
  /// request per keystroke would cost a round trip.
  Future<CommandCatalog> fetchCommands() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/commands'), headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      return CommandCatalog.fromJson(obj);
    } catch (_) {
      throw SonderException('Could not parse the command catalog.');
    }
  }

  /// Server-side completion for [q] (GET /v1/commands/complete).
  ///
  /// The palette filters the cached catalog instead of calling this on every
  /// keypress; this exists as the parity/fallback path — it is what to use
  /// when the catalog could not be cached, or to check the client filter
  /// against the server's own ranking.
  Future<List<SonderCommand>> completeCommands(
    String q, {
    int limit = 20,
  }) async {
    final uri = _uri('/v1/commands/complete').replace(queryParameters: {
      'q': q,
      'limit': '$limit',
    });
    late http.Response resp;
    try {
      resp = await http
          .get(uri, headers: _headers())
          .timeout(const Duration(seconds: 10));
    } catch (e) {
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      final matches = obj['matches'];
      if (matches is! List) return const [];
      return matches
          .whereType<Map>()
          .map((c) => SonderCommand.fromJson(Map<String, dynamic>.from(c)))
          .where((c) => c.name.isNotEmpty)
          .toList(growable: false);
    } catch (_) {
      throw SonderException('Could not parse command completions.');
    }
  }

  /// Rendered help for one command or topic (GET /v1/commands/help).
  Future<String> commandHelp(String topic) async {
    final uri = _uri('/v1/commands/help').replace(queryParameters: {
      'topic': topic,
    });
    late http.Response resp;
    try {
      resp = await http
          .get(uri, headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      throw SonderException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized - check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      return obj['text']?.toString() ?? '';
    } catch (_) {
      throw SonderException('Could not parse command help.');
    }
  }

  /// Send the full conversation and return the assistant's reply text.
  ///
  /// The serve layer threads history from the messages we send, and also
  /// handles slash-commands (/stats, /train, /pass, …), passive feedback,
  /// and natural-language control — so we simply forward whatever the user
  /// typed as the last user message.
  ///
  /// Callers that want the model's reasoning alongside the answer should use
  /// [chatDetailed]; this returns the answer only.
  Future<String> chat(
    List<ChatMessage> messages, {
    String model = 'sonder',
    String contextSize = '8192',
    String sessionId = '',
    String project = '',
    bool allowApproximateLocation = false,
  }) async {
    final reply = await chatDetailed(
      messages,
      model: model,
      contextSize: contextSize,
      sessionId: sessionId,
      project: project,
      allowApproximateLocation: allowApproximateLocation,
    );
    return reply.text;
  }

  /// Send the conversation and return the reply plus any model reasoning.
  ///
  /// `sonder_reasoning` is present only when the server has been configured to
  /// expose it (SONDER_EXPOSE_REASONING, and the caller clears
  /// SONDER_REASONING_AUDIENCE). Absence is the normal case, and means this
  /// deployment does not expose reasoning — not that the model had none.
  Future<ChatReply> chatDetailed(
    List<ChatMessage> messages, {
    String model = 'sonder',
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
          throw SonderException.transport(e, baseUrl);
        }
      } else {
        throw SonderException.transport(e, baseUrl);
      }
    }

    if (resp.statusCode == 401) {
      throw SonderException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw SonderException('Server returned HTTP ${resp.statusCode}.');
    }

    try {
      final obj =
          jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
      final choices = (obj['choices'] as List?) ?? const [];
      if (choices.isEmpty) {
        throw SonderException('Empty response from server.');
      }
      final msg = (choices.first as Map<String, dynamic>)['message']
          as Map<String, dynamic>?;
      final content = msg?['content']?.toString() ?? '';
      final reply = content.trimRight();
      final reasoning = obj['sonder_reasoning']?.toString().trim() ?? '';
      return ChatReply(
        text: warning.isEmpty ? reply : '$warning\n\n$reply',
        reasoning: reasoning,
      );
    } on SonderException {
      rethrow;
    } catch (_) {
      throw SonderException('Could not parse server response.');
    }
  }

  Future<String> register(String username, String password) async {
    return _accountAction('/v1/sonder/register', username, password);
  }

  Future<String> login(String username, String password) async {
    late http.Response resp;
    try {
      resp = await http
          .post(
            _uri('/v1/sonder/login'),
            headers: _headers(),
            body: jsonEncode({'username': username, 'password': password}),
          )
          .timeout(const Duration(seconds: 20));
    } catch (e) {
      throw SonderException('Login failed: $e');
    }
    final obj = jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
    if (resp.statusCode != 200 || obj['ok'] != true) {
      throw SonderException(obj['message']?.toString() ?? 'Login failed.');
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
      throw SonderException('Account request failed: $e');
    }
    final obj = jsonDecode(utf8.decode(resp.bodyBytes)) as Map<String, dynamic>;
    if (resp.statusCode != 200 || obj['ok'] != true) {
      throw SonderException(
          obj['message']?.toString() ?? 'Account request failed.');
    }
    return obj['message']?.toString() ?? 'OK';
  }
}

class SonderException implements Exception {
  final String message;

  /// The underlying error, kept so a details view can show it without the
  /// chat bubble having to lead with it.
  final Object? cause;

  SonderException(this.message, {this.cause});

  /// Turn a transport failure into something a person can act on.
  ///
  /// These used to reach the chat bubble verbatim, so the first thing a new
  /// user saw was "ClientException with SocketException: The remote computer
  /// refused the network connection (OS Error: ..., errno = 1225), address =
  /// 127.0.0.1, port = 56249". Every part of that is either noise (an
  /// ephemeral local port number) or jargon (errno 1225), and none of it
  /// says the one thing that matters: the server is not running.
  factory SonderException.transport(Object error, String baseUrl) {
    final text = error.toString();
    final refused = text.contains('refused') ||
        text.contains('errno = 1225') ||
        text.contains('errno = 111') ||
        text.contains('Connection closed before full header');
    final timedOut =
        text.contains('TimeoutException') || text.contains('timed out');

    if (refused) {
      return SonderException(
        'Cannot reach the Sonder server at $baseUrl.\n\n'
        "It does not look like it is running. Open the System page and use "
        'Start server, or check the server URL in Settings.',
        cause: error,
      );
    }
    if (timedOut) {
      return SonderException(
        'The Sonder server at $baseUrl did not respond in time.\n\n'
        'A model loading for the first time can take a while — the System '
        'page shows whether the server is up.',
        cause: error,
      );
    }
    return SonderException('Could not reach $baseUrl.', cause: error);
  }

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
  final ExecutionStatus? execution;
  final ExecutionFeed? executionFeed;
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
    required this.execution,
    this.executionFeed,
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
      execution: json['execution'] is Map<String, dynamic>
          ? ExecutionStatus.fromJson(json['execution'] as Map<String, dynamic>)
          : null,
      executionFeed: json['execution'] is Map<String, dynamic> &&
              (json['execution'] as Map<String, dynamic>)['feed']
                  is Map<String, dynamic>
          ? ExecutionFeed.fromJson(
              (json['execution'] as Map<String, dynamic>)['feed']
                  as Map<String, dynamic>,
            )
          : null,
      models: models,
    );
  }

  String get executionSummary =>
      execution?.summary ?? 'lanes unknown | agents unknown';
}

class ExecutionStatus {
  final bool known;
  final int? runningLanes;
  final int? runningAgents;
  final int? queuedAgents;
  final int? activeAgents;
  final String semantics;
  final String error;

  const ExecutionStatus({
    required this.known,
    required this.runningLanes,
    required this.runningAgents,
    required this.queuedAgents,
    required this.activeAgents,
    required this.semantics,
    required this.error,
  });

  factory ExecutionStatus.fromJson(Map<String, dynamic> json) {
    final known = json['known'] == true;
    int? count(String key) =>
        known && json[key] != null ? _asInt(json[key]) : null;
    return ExecutionStatus(
      known: known,
      runningLanes: count('running_lanes'),
      runningAgents: count('running_agents'),
      queuedAgents: count('queued_agents'),
      activeAgents: count('active_agents'),
      semantics: json['semantics']?.toString() ?? '',
      error: json['error']?.toString() ?? '',
    );
  }

  String get summary {
    if (!known || runningLanes == null || runningAgents == null) {
      return 'lanes unknown | agents unknown';
    }
    final queued = (queuedAgents ?? 0) > 0 ? ' +$queuedAgents queued' : '';
    return 'lanes $runningLanes | agents $runningAgents$queued';
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

  /// Blended positive rate. Dominated by autograded outcomes -- the runtime
  /// marking its own curriculum -- so it is NOT an accuracy figure and must
  /// never be shown on its own. Use [reviewedPositivePercent] for the number
  /// that says whether delegated work was any good.
  final double positivePercent;
  final double reviewedPositivePercent;
  final int reviewedOutcomes;
  final double autogradedPositivePercent;
  final int autogradedOutcomes;
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
    required this.reviewedPositivePercent,
    required this.reviewedOutcomes,
    required this.autogradedPositivePercent,
    required this.autogradedOutcomes,
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
      reviewedPositivePercent: _asDouble(json['reviewed_positive_percent']),
      reviewedOutcomes: _asInt(json['reviewed_outcomes']),
      autogradedPositivePercent: _asDouble(json['autograded_positive_percent']),
      autogradedOutcomes: _asInt(json['autograded_outcomes']),
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
  final int embeddingDefects;
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
    required this.embeddingDefects,
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
        embeddingDefects = 0,
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
      // Ignoring invalid/provenance-mismatched vectors let the UI declare
      // hygiene clean while semantic search had known embedding defects.
      embeddingDefects: _asInt(json['embedding_legacy']) +
          _asInt(json['embedding_model_mismatch']) +
          _asInt(json['embedding_revision_mismatch']) +
          _asInt(json['embedding_dimension_missing']) +
          _asInt(json['embedding_dimension_invalid']) +
          _asInt(json['embedding_dimension_mismatch']) +
          _asInt(json['embedding_vector_invalid']) +
          (_asBool(json['embedding_mixed_dimensions']) ? 1 : 0),
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
      orphanFts +
      embeddingDefects;
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

/// One assistant turn: the answer, plus the model's reasoning when the
/// deployment exposes it. [reasoning] is empty in the normal case.
class ChatReply {
  final String text;
  final String reasoning;

  const ChatReply({required this.text, this.reasoning = ''});

  bool get hasReasoning => reasoning.trim().isNotEmpty;
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

class ExecutionFeed {
  static const hardMaxEvents = 32;

  final bool known;
  final int schemaVersion;
  final String runtimeId;
  final int? activeResponses;
  final bool truncated;
  final bool redactionApplied;
  final int? oldestSeq;
  final int? nextSeq;
  final int? droppedEvents;
  final int? sequenceGap;
  final int eventLimit;
  final int previewCharLimit;
  final String error;
  final int bytes;
  final List<ExecutionFeedEvent> events;

  const ExecutionFeed({
    required this.known,
    required this.schemaVersion,
    required this.runtimeId,
    required this.activeResponses,
    required this.truncated,
    required this.redactionApplied,
    required this.oldestSeq,
    required this.nextSeq,
    required this.droppedEvents,
    required this.sequenceGap,
    required this.eventLimit,
    required this.previewCharLimit,
    required this.error,
    required this.bytes,
    required this.events,
  });

  factory ExecutionFeed.fromJson(Map<String, dynamic> json) {
    final limits = json['limits'] is Map<String, dynamic>
        ? json['limits'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final advertisedLimit = _asInt(limits['events']);
    final eventLimit = advertisedLimit <= 0
        ? 20
        : advertisedLimit.clamp(1, hardMaxEvents).toInt();
    final parsed = (json['events'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ExecutionFeedEvent.fromJson)
        .toList();
    final events = parsed.length <= eventLimit
        ? parsed
        : parsed.sublist(parsed.length - eventLimit);
    return ExecutionFeed(
      known: json['known'] == true,
      schemaVersion: _asInt(json['schema_version']),
      runtimeId: json['runtime_id']?.toString() ?? '',
      activeResponses: json['active_responses'] == null
          ? null
          : _asInt(json['active_responses']),
      truncated: json['truncated'] == true || parsed.length > eventLimit,
      redactionApplied: json['redaction_applied'] == true,
      oldestSeq:
          json['oldest_seq'] == null ? null : _asInt(json['oldest_seq']),
      nextSeq: json['next_seq'] == null ? null : _asInt(json['next_seq']),
      droppedEvents: json['dropped_events'] == null
          ? null
          : _asInt(json['dropped_events']),
      sequenceGap: json['sequence_gap'] == null
          ? null
          : _asInt(json['sequence_gap']),
      eventLimit: eventLimit,
      previewCharLimit: _asInt(limits['preview_chars']),
      error: json['error']?.toString() ?? '',
      bytes: _asInt(json['bytes']),
      events: events,
    );
  }

  bool get hasGap {
    if (sequenceGap != null || droppedEvents != null) {
      return (sequenceGap ?? 0) > 0 || (droppedEvents ?? 0) > 0;
    }
    final lastByResponse = <String, int>{};
    for (final event in events) {
      final previous = lastByResponse[event.responseId];
      if (previous != null && event.seq > previous + 1) return true;
      if (previous == null || event.seq > previous) {
        lastByResponse[event.responseId] = event.seq;
      }
    }
    return false;
  }

  bool get detailsDisabled =>
      events.any((event) => event.previewState == 'disabled') &&
      events.every((event) => event.previewState != 'available');
}

class ExecutionFeedEvent {
  final String responseId;
  final String responseStatus;
  final int seq;
  final DateTime? timestamp;
  final String kind;
  final String phase;
  final int elapsedMs;
  final String model;
  final int promptChars;
  final int historyMessages;
  final int tokensIn;
  final int tokensOut;
  final String tool;
  final String title;
  final String fileOperation;
  final String path;
  final int linesAdded;
  final int linesEdited;
  final int linesDeleted;
  final int bytes;
  final bool dryRun;
  final String previewKind;
  final bool? ok;
  final ExecutionPreview requestPreview;
  final ExecutionPreview responsePreview;
  final ExecutionPreview argsPreview;
  final ExecutionPreview commandPreview;
  final ExecutionPreview resultPreview;
  final ExecutionPreview contentPreview;
  final ExecutionPreview summaryPreview;

  const ExecutionFeedEvent({
    required this.responseId,
    required this.responseStatus,
    required this.seq,
    required this.timestamp,
    required this.kind,
    required this.phase,
    required this.elapsedMs,
    required this.model,
    required this.promptChars,
    required this.historyMessages,
    required this.tokensIn,
    required this.tokensOut,
    required this.tool,
    required this.title,
    required this.fileOperation,
    required this.path,
    required this.linesAdded,
    required this.linesEdited,
    required this.linesDeleted,
    required this.bytes,
    required this.dryRun,
    required this.previewKind,
    required this.ok,
    required this.requestPreview,
    required this.responsePreview,
    required this.argsPreview,
    required this.commandPreview,
    required this.resultPreview,
    required this.contentPreview,
    required this.summaryPreview,
  });

  factory ExecutionFeedEvent.fromJson(Map<String, dynamic> json) {
    return ExecutionFeedEvent(
      responseId: _safeExecutionText(json['response_id']?.toString() ?? '', 128),
      responseStatus: _safeExecutionText(
          json['response_status']?.toString() ?? 'unknown', 64),
      seq: _asInt(json['seq']),
      timestamp: _executionTimestamp(json['ts']),
      kind: _safeExecutionText(json['kind']?.toString() ?? '', 64).isNotEmpty
          ? _safeExecutionText(json['kind']?.toString() ?? '', 64)
          : 'unknown',
      phase: _safeExecutionText(json['phase']?.toString() ?? '', 64),
      elapsedMs: _asInt(json['elapsed_ms']),
      model: _safeExecutionText(json['model']?.toString() ?? '', 160),
      promptChars: _asInt(json['prompt_chars']),
      historyMessages: _asInt(json['history_messages']),
      tokensIn: _asInt(json['tokens_in']),
      tokensOut: _asInt(json['tokens_out']),
      tool: _safeExecutionText(json['tool']?.toString() ?? '', 160),
      title: _safeExecutionText(json['title']?.toString() ?? '', 160),
      fileOperation: _safeExecutionText(json['action']?.toString() ?? '', 80),
      path: _safeExecutionText(json['path']?.toString() ?? '', 200),
      linesAdded: _asInt(json['lines_added']),
      linesEdited: _asInt(json['lines_edited']),
      linesDeleted: _asInt(json['lines_deleted']),
      bytes: _asInt(json['bytes']),
      dryRun: json['dry_run'] == true,
      previewKind: _safeExecutionText(
          json['preview_kind']?.toString() ?? '', 80),
      ok: json['ok'] is bool ? json['ok'] as bool : null,
      requestPreview: ExecutionPreview.fromJson(json['request_preview']),
      responsePreview: ExecutionPreview.fromJson(json['response_preview']),
      argsPreview: ExecutionPreview.fromJson(json['args_preview']),
      commandPreview: ExecutionPreview.fromJson(json['command_preview']),
      resultPreview: ExecutionPreview.fromJson(json['result_preview']),
      contentPreview: ExecutionPreview.fromJson(json['content_preview']),
      summaryPreview: ExecutionPreview.fromJson(json['summary_preview']),
    );
  }

  String get status {
    if (ok == true) return 'ok';
    if (ok == false) return 'error';
    return phase.isEmpty ? responseStatus : phase;
  }

  String get deltaLabel {
    if (linesAdded == 0 && linesEdited == 0 && linesDeleted == 0) return '';
    return 'lines +$linesAdded ~$linesEdited -$linesDeleted';
  }

  List<ExecutionPreview> get previews => [
        responsePreview,
        resultPreview,
        contentPreview,
        commandPreview,
        argsPreview,
        requestPreview,
        summaryPreview,
      ];

  ExecutionPreview get displayPreview => previews.firstWhere(
        (preview) => preview.state == 'available' && preview.text.isNotEmpty,
        orElse: () => previews.firstWhere(
          (preview) => preview.state != 'unavailable',
          orElse: () => ExecutionPreview.unavailable,
        ),
      );

  String get previewState => displayPreview.state;
  String get preview => displayPreview.text;

  String get summary {
    if ((kind == 'model' || kind == 'model_call') && model.isNotEmpty) {
      return 'Model $model';
    }
    if (kind == 'tool' || kind == 'tool_call') {
      return title.isNotEmpty ? title : tool;
    }
    if (kind == 'file' || kind == 'file_op' || kind == 'file_change') {
      return [fileOperation, path].where((value) => value.isNotEmpty).join(' ');
    }
    return summaryPreview.text;
  }
}

class ExecutionPreview {
  static const maxTextRunes = 300;
  static const unavailable = ExecutionPreview(
    state: 'unavailable',
    text: '',
    chars: null,
    truncated: false,
    redacted: false,
  );

  final String state;
  final String text;
  final int? chars;
  final bool truncated;
  final bool redacted;

  const ExecutionPreview({
    required this.state,
    required this.text,
    required this.chars,
    required this.truncated,
    required this.redacted,
  });

  factory ExecutionPreview.fromJson(dynamic value) {
    if (value is! Map<String, dynamic>) return unavailable;
    final reportedState = value['state']?.toString() ?? 'unavailable';
    final state = const {'available', 'unavailable', 'disabled'}
            .contains(reportedState)
        ? reportedState
        : 'unavailable';
    final rawText = value['text']?.toString() ?? '';
    final text = state == 'available'
        ? _safeExecutionText(rawText, maxTextRunes)
        : '';
    return ExecutionPreview(
      state: state,
      text: text,
      chars: value['chars'] == null ? null : _asInt(value['chars']),
      truncated: value['truncated'] == true || rawText.runes.length > maxTextRunes,
      redacted: value['redacted'] == true,
    );
  }
}

DateTime? _executionTimestamp(dynamic value) {
  if (value is num) {
    final raw = value.toInt();
    final milliseconds = raw.abs() < 100000000000 ? raw * 1000 : raw;
    return DateTime.fromMillisecondsSinceEpoch(milliseconds, isUtc: true);
  }
  final raw = value?.toString().trim() ?? '';
  return raw.isEmpty ? null : DateTime.tryParse(raw)?.toUtc();
}

String _safeExecutionText(String value, int maxRunes) {
  var safe = value
      .replaceAll(
        RegExp(r'[\x00-\x1F\x7F-\x9F\u202A-\u202E\u2066-\u2069]+'),
        ' ',
      )
      .trim();
  safe = safe.replaceAllMapped(
    RegExp(
      r'\b(authorization\s*:\s*bearer|bearer|api[_-]?key|token|password|secret)\b(\s*[:=]\s*|\s+)[^\s,;]+',
      caseSensitive: false,
    ),
    (match) => '${match.group(1)}=<redacted>',
  );
  safe = safe.replaceAll(
    RegExp(r'\b(?:sk-proj|sk)-[A-Za-z0-9_-]{8,}\b'),
    '<redacted>',
  );
  safe = safe.replaceAll(
    RegExp(
      r'(?:[A-Za-z]:\\Users\\|/home/)[^\\/\s]+',
      caseSensitive: false,
    ),
    '<user-home>',
  );
  final runes = safe.runes.toList(growable: false);
  if (runes.length <= maxRunes) return safe;
  return '${String.fromCharCodes(runes.take(maxRunes - 1))}…';
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
