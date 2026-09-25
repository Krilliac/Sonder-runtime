/// Core data model for a single chat turn.
enum Role { user, assistant, system }

String _boundedMetadataText(Object? value, [int limit = 256]) {
  final text = value?.toString().trim() ?? '';
  if (text.length <= limit) return text;
  return '${text.substring(0, limit)}...';
}

int _metadataCount(Object? value) {
  if (value is int) return value < 0 ? 0 : value;
  final parsed = int.tryParse(value?.toString() ?? '') ?? 0;
  return parsed < 0 ? 0 : parsed;
}

/// Privacy-safe response evidence returned alongside an OpenAI-compatible turn.
///
/// Sonder's HTTP API keeps request receipts, token counts and aggregate activity
/// outside assistant content so replaying a chat does not feed diagnostics back
/// to the model. The app follows the same contract: this record is persisted for
/// local history and display, but [ChatMessage.toWire] never serializes it.
class ChatResponseMetadata {
  final String completionId;
  final String requestId;
  final String model;
  final String tier;
  final String finishReason;
  final String status;
  final String cache;
  final int elapsedMs;
  final int promptTokens;
  final int completionTokens;
  final int totalTokens;
  final int modelCalls;
  final int toolCalls;

  /// `sonder_receipt.chat_work.work_run_id`: the routed work run behind this
  /// turn, or empty. With [workStatus] `running` the answer is not in the
  /// reply yet; fetch it with `WorkRunsApi.get`.
  final String workRunId;

  /// `sonder_receipt.chat_work.status` (`running`, `returned`, `refused`,
  /// `cancelled`, `budget_exceeded`, `interrupted`, `failed`, `unknown`).
  final String workStatus;

  /// A permission-gate refusal carried by this turn, or null.
  final ChatRefusal? refusal;

  const ChatResponseMetadata({
    this.completionId = '',
    this.requestId = '',
    this.model = '',
    this.tier = '',
    this.finishReason = '',
    this.status = '',
    this.cache = '',
    this.elapsedMs = 0,
    this.promptTokens = 0,
    this.completionTokens = 0,
    this.totalTokens = 0,
    this.modelCalls = 0,
    this.toolCalls = 0,
    this.workRunId = '',
    this.workStatus = '',
    this.refusal,
  });

  /// True while the answer is still being produced by a work run.
  bool get workRunning => workRunId.isNotEmpty && workStatus == 'running';

  factory ChatResponseMetadata.fromJson(Map<String, dynamic> json) =>
      ChatResponseMetadata(
        completionId: _boundedMetadataText(json['completion_id']),
        requestId: _boundedMetadataText(json['request_id']),
        model: _boundedMetadataText(json['model']),
        tier: _boundedMetadataText(json['tier']),
        finishReason: _boundedMetadataText(json['finish_reason'], 64),
        status: _boundedMetadataText(json['status'], 64),
        cache: _boundedMetadataText(json['cache'], 16),
        elapsedMs: _metadataCount(json['elapsed_ms']),
        promptTokens: _metadataCount(json['prompt_tokens']),
        completionTokens: _metadataCount(json['completion_tokens']),
        totalTokens: _metadataCount(json['total_tokens']),
        modelCalls: _metadataCount(json['model_calls']),
        toolCalls: _metadataCount(json['tool_calls']),
        workRunId: _workRunIdOrEmpty(json['work_run_id']),
        workStatus: _boundedMetadataText(json['work_status'], 32),
        refusal: json['refusal'] is Map
            ? ChatRefusal.fromJson(
                Map<String, dynamic>.from(json['refusal'] as Map))
            : null,
      );

  /// A copy with the work-run fields replaced (after a refresh or cancel).
  ChatResponseMetadata withWork({String? workRunId, String? workStatus}) =>
      ChatResponseMetadata(
        completionId: completionId,
        requestId: requestId,
        model: model,
        tier: tier,
        finishReason: finishReason,
        status: status,
        cache: cache,
        elapsedMs: elapsedMs,
        promptTokens: promptTokens,
        completionTokens: completionTokens,
        totalTokens: totalTokens,
        modelCalls: modelCalls,
        toolCalls: toolCalls,
        workRunId: workRunId ?? this.workRunId,
        workStatus: workStatus ?? this.workStatus,
        refusal: refusal,
      );

  bool get isEmpty =>
      completionId.isEmpty &&
      requestId.isEmpty &&
      model.isEmpty &&
      tier.isEmpty &&
      finishReason.isEmpty &&
      status.isEmpty &&
      cache.isEmpty &&
      elapsedMs == 0 &&
      promptTokens == 0 &&
      completionTokens == 0 &&
      totalTokens == 0 &&
      modelCalls == 0 &&
      toolCalls == 0 &&
      workRunId.isEmpty &&
      workStatus.isEmpty &&
      refusal == null;

  Map<String, Object> toJson() => {
        'completion_id': completionId,
        'request_id': requestId,
        'model': model,
        'tier': tier,
        'finish_reason': finishReason,
        'status': status,
        'cache': cache,
        'elapsed_ms': elapsedMs,
        'prompt_tokens': promptTokens,
        'completion_tokens': completionTokens,
        'total_tokens': totalTokens,
        'model_calls': modelCalls,
        'tool_calls': toolCalls,
        if (workRunId.isNotEmpty) 'work_run_id': workRunId,
        if (workStatus.isNotEmpty) 'work_status': workStatus,
        if (refusal != null) 'refusal': refusal!.toJson(),
      };

  /// Compact, content-free evidence suitable for a collapsed diagnostics row.
  String get diagnosticText {
    final lines = <String>[];
    if (status.isNotEmpty) lines.add('status: $status');
    if (requestId.isNotEmpty) lines.add('request: $requestId');
    if (completionId.isNotEmpty) lines.add('completion: $completionId');
    if (model.isNotEmpty) lines.add('model: $model');
    if (tier.isNotEmpty) lines.add('tier: $tier');
    if (finishReason.isNotEmpty) lines.add('finish: $finishReason');
    if (elapsedMs > 0) lines.add('elapsed: ${elapsedMs}ms');
    if (totalTokens > 0 || promptTokens > 0 || completionTokens > 0) {
      lines.add(
        'tokens: $totalTokens total ($promptTokens prompt, '
        '$completionTokens completion)',
      );
    }
    if (modelCalls > 0 || toolCalls > 0) {
      lines.add('calls: $modelCalls model, $toolCalls tool');
    }
    if (cache.isNotEmpty) {
      lines.add(cache == 'hit' ? 'cache: hit (replayed)' : 'cache: $cache');
    }
    if (workRunId.isNotEmpty) {
      lines.add(
          'work run: $workRunId${workStatus.isEmpty ? '' : ' ($workStatus)'}');
    }
    if (refusal?.callId.isNotEmpty == true) {
      lines.add('refused call: ${refusal!.callId}');
    }
    return lines.join('\n');
  }
}

String _workRunIdOrEmpty(Object? value) {
  final text = value?.toString().trim() ?? '';
  return RegExp(r'^wr-[0-9a-f]{32}$').hasMatch(text) ? text : '';
}

/// A permission gate refused a call in this turn.
///
/// Read from `sonder_receipt.refusal` once the server publishes it (server
/// S1); until then [ChatRefusal.fromText] recognises the refusal wording.
/// [callId] is non-empty only when the call can be approved once.
class ChatRefusal {
  final String callId;
  final String tool;
  final String reason;
  final List<String> remedies;

  const ChatRefusal({
    this.callId = '',
    this.tool = '',
    this.reason = '',
    this.remedies = const [],
  });

  static final RegExp _callId = RegExp(r'/approve ([0-9a-f]{8,64})\b');
  // Case-sensitive: the server's gates always write lowercase `refused`,
  // while a model answer that merely opens with "Refused connections …"
  // must not lose its rating chips.
  static final RegExp _refusedPrefix = RegExp(r'^\s*refused(?:[:\s]|$)');

  factory ChatRefusal.fromJson(Map<String, dynamic> json) {
    final id = json['call_id']?.toString().trim() ?? '';
    final remedies = json['remedies'] is List
        ? (json['remedies'] as List)
            .map((r) => _boundedMetadataText(r, 512))
            .where((r) => r.isNotEmpty)
            .take(8)
            .toList(growable: false)
        : const <String>[];
    return ChatRefusal(
      callId: RegExp(r'^[0-9a-f]{8,64}$').hasMatch(id) ? id : '',
      tool: _boundedMetadataText(json['tool'], 128),
      reason: _boundedMetadataText(json['reason'], 1024),
      remedies: remedies,
    );
  }

  /// Text fallback until server S1: the reply starts with `refused` and may
  /// name `/approve <call id>`. Returns null for any other reply.
  static ChatRefusal? fromText(String text) {
    if (!_refusedPrefix.hasMatch(text)) return null;
    final id = _callId.firstMatch(text)?.group(1) ?? '';
    final firstLine = text.trim().split('\n').first;
    return ChatRefusal(
      callId: id,
      reason: _boundedMetadataText(firstLine, 1024),
      remedies: id.isEmpty ? const [] : ['/approve $id'],
    );
  }

  Map<String, Object> toJson() => {
        if (callId.isNotEmpty) 'call_id': callId,
        if (tool.isNotEmpty) 'tool': tool,
        if (reason.isNotEmpty) 'reason': reason,
        if (remedies.isNotEmpty) 'remedies': remedies,
      };
}

class ChatMessage {
  final Role role;
  final String content;
  final bool pending; // true while the assistant reply is in-flight
  final bool error;

  /// The model's reasoning for this turn, when the server exposes it.
  ///
  /// Deliberately a field rather than a marker inside [content]: the activity
  /// block rides in the content and so is replayed to the model as history,
  /// which is fine for a short evidence summary but wasteful for reasoning.
  /// Keeping it out of [content] keeps it out of [toWire] for free.
  final String reasoning;

  /// Structured, content-free receipt/usage evidence for this assistant turn.
  final ChatResponseMetadata? responseMetadata;

  /// Client/server diagnostics kept out of assistant content and model history.
  final String diagnostic;

  const ChatMessage({
    required this.role,
    required this.content,
    this.pending = false,
    this.error = false,
    this.reasoning = '',
    this.responseMetadata,
    this.diagnostic = '',
  });

  ChatMessage copyWith({
    String? content,
    bool? pending,
    bool? error,
    String? reasoning,
    ChatResponseMetadata? responseMetadata,
    String? diagnostic,
  }) {
    return ChatMessage(
      role: role,
      content: content ?? this.content,
      pending: pending ?? this.pending,
      error: error ?? this.error,
      reasoning: reasoning ?? this.reasoning,
      responseMetadata: responseMetadata ?? this.responseMetadata,
      diagnostic: diagnostic ?? this.diagnostic,
    );
  }

  /// Wire format for the OpenAI-compatible /v1/chat/completions endpoint.
  ///
  /// Intentionally omits [reasoning]: replaying a model's own thoughts back to
  /// it as history is not something the server asked for.
  Map<String, String> toWire() => {'role': role.name, 'content': content};

  Map<String, Object> toJson() => {
        'role': role.name,
        'content': content,
        'pending': pending,
        'error': error,
        'reasoning': reasoning,
        if (responseMetadata != null)
          'response_metadata': responseMetadata!.toJson(),
        'diagnostic': diagnostic,
      };

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    final roleName = json['role']?.toString() ?? 'assistant';
    return ChatMessage(
      role: Role.values.firstWhere(
        (r) => r.name == roleName,
        orElse: () => Role.assistant,
      ),
      content: json['content']?.toString() ?? '',
      pending: json['pending'] == true,
      error: json['error'] == true,
      reasoning: json['reasoning']?.toString() ?? '',
      responseMetadata: json['response_metadata'] is Map
          ? ChatResponseMetadata.fromJson(
              Map<String, dynamic>.from(json['response_metadata'] as Map),
            )
          : null,
      diagnostic: json['diagnostic']?.toString() ?? '',
    );
  }
}

class ChatThread {
  final String id;
  final String title;
  final String project;
  final DateTime createdAt;
  final DateTime updatedAt;
  final List<ChatMessage> messages;

  const ChatThread({
    required this.id,
    required this.title,
    required this.project,
    required this.createdAt,
    required this.updatedAt,
    required this.messages,
  });

  factory ChatThread.fresh({String project = 'default'}) {
    final now = DateTime.now();
    return ChatThread(
      id: 'chat-${now.microsecondsSinceEpoch}',
      title: 'New chat',
      project: project.trim().isEmpty ? 'default' : project.trim(),
      createdAt: now,
      updatedAt: now,
      messages: const [],
    );
  }

  ChatThread copyWith({
    String? title,
    String? project,
    DateTime? updatedAt,
    List<ChatMessage>? messages,
  }) {
    return ChatThread(
      id: id,
      title: title ?? this.title,
      project: project ?? this.project,
      createdAt: createdAt,
      updatedAt: updatedAt ?? this.updatedAt,
      messages: messages ?? this.messages,
    );
  }

  Map<String, Object> toJson() => {
        'id': id,
        'title': title,
        'project': project,
        'created_at': createdAt.toIso8601String(),
        'updated_at': updatedAt.toIso8601String(),
        'messages': messages.map((m) => m.toJson()).toList(),
      };

  factory ChatThread.fromJson(Map<String, dynamic> json) {
    final messages = (json['messages'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ChatMessage.fromJson)
        .where((m) => !m.pending)
        .toList();
    return ChatThread(
      id: json['id']?.toString() ?? ChatThread.fresh().id,
      title: json['title']?.toString() ?? 'New chat',
      project: json['project']?.toString() ?? 'default',
      createdAt: DateTime.tryParse(json['created_at']?.toString() ?? '') ??
          DateTime.now(),
      updatedAt: DateTime.tryParse(json['updated_at']?.toString() ?? '') ??
          DateTime.now(),
      messages: messages,
    );
  }

  String get displayTitle {
    final trimmed = title.trim();
    if (trimmed.isNotEmpty && trimmed != 'New chat') return trimmed;
    final firstUser = messages.where((m) => m.role == Role.user).firstOrNull;
    if (firstUser == null) return 'New chat';
    final oneLine = firstUser.content.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (oneLine.length <= 42) return oneLine;
    return '${oneLine.substring(0, 42)}...';
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull {
    final iterator = this.iterator;
    if (!iterator.moveNext()) return null;
    return iterator.current;
  }
}

/// Durable update state (SPEC-4 section 14) as returned by
/// GET /v1/admin/updates/status. All fields are best-effort: the System
/// page renders whatever the runtime reports.
class UpdateRelease {
  final String version;
  final String releaseId;
  final String platform;
  final String architecture;

  const UpdateRelease({
    required this.version,
    required this.releaseId,
    required this.platform,
    required this.architecture,
  });

  static UpdateRelease? fromJson(Map<String, dynamic>? json) {
    if (json == null) return null;
    return UpdateRelease(
      version: (json['version'] ?? '').toString(),
      releaseId: (json['release_id'] ?? '').toString(),
      platform: (json['platform'] ?? '').toString(),
      architecture: (json['architecture'] ?? '').toString(),
    );
  }
}

class UpdatePlan {
  final String updateId;
  final String status;
  final String channel;
  final String targetVersion;
  final String createdAt;
  final String? errorCode;
  final String? confirmNonce;

  const UpdatePlan({
    required this.updateId,
    required this.status,
    required this.channel,
    required this.targetVersion,
    required this.createdAt,
    this.errorCode,
    this.confirmNonce,
  });

  factory UpdatePlan.fromJson(Map<String, dynamic> json) => UpdatePlan(
        updateId: (json['update_id'] ?? '').toString(),
        status: (json['status'] ?? '').toString(),
        channel: (json['channel'] ?? '').toString(),
        targetVersion: (json['target_version'] ?? '').toString(),
        createdAt: (json['created_at_utc'] ?? '').toString(),
        errorCode: json['error_code']?.toString(),
        confirmNonce: json['confirm_nonce']?.toString(),
      );

  bool get isAvailable => status == 'available';
  bool get isTerminal => const {
        'committed',
        'rolled_back',
        'blocked',
        'failed',
        'cancelled',
      }.contains(status);
}

class UpdateStatus {
  final String runningVersion;
  final String runningCommit;
  final String platform;
  final String architecture;
  final String? currentTarget;
  final UpdateRelease? activeRelease;
  final UpdateRelease? previousRelease;
  final List<UpdatePlan> plans;

  const UpdateStatus({
    required this.runningVersion,
    required this.runningCommit,
    required this.platform,
    required this.architecture,
    required this.currentTarget,
    required this.activeRelease,
    required this.previousRelease,
    required this.plans,
  });

  factory UpdateStatus.fromJson(Map<String, dynamic> json) => UpdateStatus(
        runningVersion: (json['running_version'] ?? '').toString(),
        runningCommit: (json['running_commit'] ?? '').toString(),
        platform: (json['platform'] ?? '').toString(),
        architecture: (json['architecture'] ?? '').toString(),
        currentTarget: json['current_target']?.toString(),
        activeRelease: UpdateRelease.fromJson(
          json['active_release'] as Map<String, dynamic>?,
        ),
        previousRelease: UpdateRelease.fromJson(
          json['previous_release'] as Map<String, dynamic>?,
        ),
        plans: ((json['plans'] as List<dynamic>?) ?? [])
            .whereType<Map<String, dynamic>>()
            .map(UpdatePlan.fromJson)
            .toList(),
      );

  bool get canRollback => previousRelease != null;
}

/// Bounded administrator projection of the extension registry.
class ExtensionRecord {
  final String extensionId;
  final String scope;
  final String version;
  final bool enabled;
  final String healthState;
  final int? memoryLimitBytes;
  final String? artifactDigest;

  const ExtensionRecord({
    required this.extensionId,
    required this.scope,
    required this.version,
    required this.enabled,
    required this.healthState,
    this.memoryLimitBytes,
    this.artifactDigest,
  });

  factory ExtensionRecord.fromJson(Map<String, dynamic> json) {
    final resources = json['resources'];
    final artifact = json['artifact'];
    final memory = resources is Map ? resources['memory_limit_bytes'] : null;
    return ExtensionRecord(
      extensionId: json['extension_id']?.toString() ?? '',
      scope: json['scope']?.toString() ?? '',
      version: json['version']?.toString() ?? '',
      enabled: json['enabled'] == true,
      healthState: json['health_state']?.toString() ?? 'unknown',
      memoryLimitBytes: memory is int ? memory : null,
      artifactDigest:
          artifact is Map ? artifact['artifact_digest']?.toString() : null,
    );
  }
}

class ExtensionRegistryStatus {
  final String persistence;
  final List<ExtensionRecord> records;

  const ExtensionRegistryStatus({
    required this.persistence,
    required this.records,
  });

  factory ExtensionRegistryStatus.fromJson(Map<String, dynamic> json) =>
      ExtensionRegistryStatus(
        persistence: json['persistence']?.toString() ?? 'unknown',
        records: ((json['records'] as List<dynamic>?) ?? [])
            .whereType<Map<String, dynamic>>()
            .map(ExtensionRecord.fromJson)
            .toList(growable: false),
      );
}
