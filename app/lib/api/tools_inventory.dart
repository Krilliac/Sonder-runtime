/// Host developer-tool inventory: `GET /v1/tools/inventory` and
/// `POST /v1/tools/inventory/refresh` (admin only).
///
/// The wire shape is `view_to_wire` in
/// `sonder_runtime/domain/host_tools/model.py`; paths arrive already
/// redacted by the server. The server admits at most two inventory requests
/// at a time and answers `429 TOOL_INVENTORY_BUSY` beyond that, `413
/// TOOL_INVENTORY_TOO_LARGE` when the unfiltered list would not fit (filter
/// by category), and `503 TOOL_INVENTORY_UNAVAILABLE` when the host has no
/// inventory service. `test/fixtures/server/tool_inventory_*.json` are
/// produced by that server code (`tests/test_app_tool_inventory_fixtures.py`).
library;

import 'transport.dart';

/// Server categories in the server's display order (`ToolCategory`).
const hostToolCategories = <String>[
  'compiler',
  'build_system',
  'test_runner',
  'linter_formatter',
  'debugger_profiler',
  'package_manager',
  'runtime',
  'container_vm',
  'vcs',
  'db_client',
  'media_doc',
  'cloud_cli',
  'editor_ide',
  'shell',
];

const _categoryLabels = <String, String>{
  'compiler': 'Compilers',
  'build_system': 'Build systems',
  'test_runner': 'Test runners',
  'linter_formatter': 'Linters & formatters',
  'debugger_profiler': 'Debuggers & profilers',
  'package_manager': 'Package managers',
  'runtime': 'Runtimes',
  'container_vm': 'Containers & VMs',
  'vcs': 'Version control',
  'db_client': 'Database clients',
  'media_doc': 'Media & documents',
  'cloud_cli': 'Cloud CLIs',
  'editor_ide': 'Editors & IDEs',
  'shell': 'Shells',
};

/// The heading for [category]; an unknown (newer) category shows as sent.
String hostToolCategoryLabel(String category) =>
    _categoryLabels[category] ?? category;

/// Sort rank of [category]; unknown categories sort after known ones.
int hostToolCategoryRank(String category) {
  final index = hostToolCategories.indexOf(category);
  return index < 0 ? hostToolCategories.length : index;
}

/// Server tool-name shape (`TOOL_NAME_PATTERN`).
final RegExp _toolName = RegExp(r'^[A-Za-z0-9+._-]{1,64}$');

/// Whether [name] is a tool name the server would accept as a filter.
bool isHostToolName(String name) => _toolName.hasMatch(name);

/// What the version probe reported (`VersionStatus`).
enum ToolVersionStatus {
  ok('ok'),
  fromMetadata('from_metadata'),
  outputLimit('output_limit'),
  notProbed('not_probed'),
  deferred('deferred'),
  projectLocal('project_local_not_probed'),
  alias('alias_not_probed'),
  timeout('timeout'),
  failed('failed'),
  unknown('');

  final String wire;
  const ToolVersionStatus(this.wire);

  static ToolVersionStatus parse(Object? value) {
    for (final status in values) {
      if (status != unknown && status.wire == value) return status;
    }
    return unknown;
  }

  /// The version text is evidence from a probe or install metadata.
  bool get hasVersion =>
      this == ok || this == fromMetadata || this == outputLimit;

  /// The probe ran and did not produce a version.
  bool get probeFailed => this == timeout || this == failed;

  /// Plain words for the row when no version is shown.
  String get description => switch (this) {
        ok || fromMetadata || outputLimit => 'version reported',
        notProbed => 'version not probed',
        deferred => 'version check deferred',
        projectLocal => 'project-local, not probed',
        alias => 'alias, not probed',
        timeout => 'version probe timed out',
        failed => 'version probe failed',
        unknown => 'version unknown',
      };
}

/// Where the server found a tool (`DiscoverySource`), as plain words.
String hostToolSourceLabel(String source) => switch (source) {
      'path' => 'on PATH',
      'known_prefix' => 'known install folder',
      'vswhere' => 'Visual Studio installer',
      'windows_sdk' => 'Windows SDK',
      'app_paths' => 'Windows App Paths',
      'py_launcher' => 'py launcher',
      'scoop' => 'Scoop',
      'choco' => 'Chocolatey',
      'winget' => 'winget',
      'brew' => 'Homebrew',
      'xcode' => 'Xcode',
      'app_bundle' => 'app bundle',
      _ => source,
    };

String _text(Object? value, int limit) =>
    value is String ? boundedResponseMetadata(value, limit) : '';

int _count(Object? value) =>
    value is num && value.isFinite && value >= 0 ? value.toInt() : 0;

/// One discovered tool.
class HostTool {
  final String name;
  final String category;
  final String version;
  final ToolVersionStatus versionStatus;
  final String source;
  final bool onPath;

  /// Redacted by the server (home folder shown as `~`).
  final String path;
  final List<String> alternatives;
  final Map<String, String> details;

  const HostTool({
    required this.name,
    required this.category,
    this.version = '',
    this.versionStatus = ToolVersionStatus.unknown,
    this.source = '',
    this.onPath = false,
    this.path = '',
    this.alternatives = const [],
    this.details = const {},
  });

  factory HostTool.fromJson(Map<String, dynamic> json) {
    final alternatives = json['alternatives'];
    final details = json['details'];
    return HostTool(
      name: _text(json['name'], 64),
      category: _text(json['category'], 32),
      version: _text(json['version'], 64),
      versionStatus: ToolVersionStatus.parse(json['version_status']),
      source: _text(json['source'], 32),
      onPath: json['on_path'] == true,
      path: _text(json['path'], 4096),
      alternatives: alternatives is List
          ? alternatives
              .whereType<String>()
              .take(4)
              .map((a) => boundedResponseMetadata(a, 4096))
              .toList(growable: false)
          : const [],
      details: details is Map
          ? {
              for (final entry in details.entries.take(8))
                if (entry.key is String && entry.value is String)
                  boundedResponseMetadata(entry.key, 64):
                      boundedResponseMetadata(entry.value, 200),
            }
          : const {},
    );
  }
}

/// One inventory read, possibly filtered.
class ToolInventory {
  final String snapshotDigest;
  final DateTime? createdAt;

  /// Snapshot age when the server answered.
  final Duration age;

  /// Older than the server's refresh window; a Rediscover updates it.
  final bool stale;
  final String os;
  final String machine;

  /// Tools per category across the whole snapshot, not just [tools].
  final Map<String, int> counts;
  final List<HostTool> tools;

  /// `category=compiler`, `name=gcc` or both; empty when unfiltered.
  final String filteredBy;
  final List<String> notes;

  /// The server hit its tool limit and dropped the rest.
  final bool truncated;

  const ToolInventory({
    this.snapshotDigest = '',
    this.createdAt,
    this.age = Duration.zero,
    this.stale = false,
    this.os = '',
    this.machine = '',
    this.counts = const {},
    this.tools = const [],
    this.filteredBy = '',
    this.notes = const [],
    this.truncated = false,
  });

  /// Every tool in the snapshot, whatever the filter.
  int get total => counts.values.fold(0, (sum, n) => sum + n);

  /// [tools] grouped by category, in the server's category order.
  List<(String, List<HostTool>)> get byCategory {
    final groups = <String, List<HostTool>>{};
    for (final tool in tools) {
      groups.putIfAbsent(tool.category, () => []).add(tool);
    }
    final keys = groups.keys.toList()
      ..sort((a, b) {
        final rank = hostToolCategoryRank(a) - hostToolCategoryRank(b);
        return rank != 0 ? rank : a.compareTo(b);
      });
    return [for (final key in keys) (key, groups[key]!)];
  }

  factory ToolInventory.fromJson(Map<String, dynamic> json) {
    if (json['object'] != 'tool_inventory') {
      throw SonderException('Could not parse the tool inventory.');
    }
    final created = json['created_at'];
    final counts = json['counts'];
    final tools = json['tools'];
    final notes = json['notes'];
    return ToolInventory(
      snapshotDigest: _text(json['snapshot_digest'], 64),
      createdAt: created is num && created.isFinite && created > 0
          ? DateTime.fromMillisecondsSinceEpoch((created * 1000).round())
          : null,
      age: Duration(seconds: _count(json['age_seconds'])),
      stale: json['stale'] == true,
      os: _text(json['os'], 64),
      machine: _text(json['machine'], 64),
      counts: counts is Map
          ? {
              for (final entry in counts.entries)
                if (entry.key is String)
                  entry.key as String: _count(entry.value),
            }
          : const {},
      tools: tools is List
          ? tools
              .whereType<Map>()
              .take(512)
              .map((t) => HostTool.fromJson(Map<String, dynamic>.from(t)))
              .where((t) => t.name.isNotEmpty)
              .toList(growable: false)
          : const [],
      filteredBy: _text(json['filtered_by'], 128),
      notes: notes is List
          ? notes
              .whereType<String>()
              .take(16)
              .map((n) => boundedResponseMetadata(n, 200))
              .toList(growable: false)
          : const [],
      truncated: json['truncated'] == true,
    );
  }
}

/// Client for the inventory routes.
/// Default [ToolInventoryApi.timeout]: comfortably above the server's
/// discovery bound, since a GET can discover as well as a refresh.
const defaultToolInventoryTimeout = Duration(seconds: 90);

class ToolInventoryApi {
  final SonderEndpoint endpoint;

  /// Budget for either route. Both may run a full discovery (every version
  /// probe): Rediscover always does, and the server's GET discovers on first
  /// use and whenever its snapshot is older than the refresh window. Server
  /// discovery is bounded at about 35 s (30 s budget, 3 s probe timeout, 2 s
  /// backstop), so a short read timeout would fail while it is still working.
  final Duration timeout;

  const ToolInventoryApi(
    this.endpoint, {
    this.timeout = defaultToolInventoryTimeout,
  });

  SonderException _error(SonderException error) {
    final described = describeServerError(error, endpoint.serverUri,
        action: 'read the host tool inventory');
    return switch (error.code) {
      'TOOL_INVENTORY_BUSY' => described.copyWith(
          message: 'The server is already reading its tool inventory. '
              'Try again in a moment.'),
      'TOOL_INVENTORY_TOO_LARGE' => described.copyWith(
          message: 'Too many tools to list at once. Pick a category.'),
      'TOOL_INVENTORY_UNAVAILABLE' => described.copyWith(
          message: 'This server has no host tool inventory right now.'),
      'INVALID_TOOL_INVENTORY_QUERY' => described.copyWith(
          message: 'The server did not accept that tool filter.'),
      _ => described,
    };
  }

  Future<ToolInventory> _call(String method, String path,
      {Map<String, String>? query, CancelToken? cancel}) async {
    var uri = endpoint.uri(path);
    if (query != null && query.isNotEmpty) {
      uri = uri.replace(queryParameters: query);
    }
    final headers = endpoint.headers();
    final response = await () async {
      try {
        return method == 'GET'
            ? await requestGet(uri,
                headers: headers, timeout: timeout, cancel: cancel)
            : await requestPost(uri,
                headers: headers, body: '{}', timeout: timeout, cancel: cancel);
      } on SonderException {
        rethrow;
      } catch (error) {
        throw SonderException.transport(error, endpoint.baseUrl);
      }
    }();
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw _error(
          responseException(response, httpStatusFallback(response.statusCode)));
    }
    return ToolInventory.fromJson(
        decodeJsonObject(response, 'the tool inventory'));
  }

  /// `GET /v1/tools/inventory`: the cached snapshot (discovered on first
  /// use), optionally narrowed to one [category] and/or one tool [name].
  Future<ToolInventory> get(
      {String? category, String? name, CancelToken? cancel}) async {
    if (category != null && !hostToolCategories.contains(category)) {
      throw SonderException('That is not a tool category.',
          code: 'INVALID_TOOL_CATEGORY');
    }
    if (name != null && !isHostToolName(name)) {
      throw SonderException('That is not a tool name.',
          code: 'INVALID_TOOL_NAME');
    }
    return _call('GET', '/v1/tools/inventory',
        query: {
          if (category != null) 'category': category,
          if (name != null) 'name': name,
        },
        cancel: cancel);
  }

  /// `POST /v1/tools/inventory/refresh`: rediscover now and return the whole
  /// new snapshot. Version probes run under the server's fixed budget.
  Future<ToolInventory> refresh({CancelToken? cancel}) =>
      _call('POST', '/v1/tools/inventory/refresh', cancel: cancel);
}
