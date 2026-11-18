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

  /// Deterministic acknowledgement shown before a routed work run continues.
  final String acknowledgement;

  /// A permission-gate refusal carried by this turn, or null.
  final ChatRefusal? refusal;

  /// `sonder_receipt.overflow`: the long-context overflow decision for this
  /// turn (switched to the overflow model, or stayed), or null.
  final RouteOverflow? overflow;

  final AgentLaneReceipt? agentLane;
  final OrchestrationReceipt? orchestration;
  final List<ClientToolHandoff> clientToolHandoffs;

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
    this.acknowledgement = '',
    this.refusal,
    this.overflow,
    this.agentLane,
    this.orchestration,
    this.clientToolHandoffs = const [],
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
        acknowledgement: _boundedMetadataText(json['acknowledgement'], 2000),
        refusal: json['refusal'] is Map
            ? ChatRefusal.fromJson(
                Map<String, dynamic>.from(json['refusal'] as Map),
              )
            : null,
        overflow: RouteOverflow.fromJson(json['overflow']),
        agentLane: json['agent_lane'] is Map
            ? AgentLaneReceipt.fromJson(
                Map<String, dynamic>.from(json['agent_lane'] as Map),
              )
            : null,
        orchestration: json['orchestration'] is Map
            ? OrchestrationReceipt.fromJson(
                Map<String, dynamic>.from(json['orchestration'] as Map),
              )
            : null,
        clientToolHandoffs: ClientToolHandoff.listFromJson(
          json['client_tool_handoff'] ??
              json['client_tool_handoffs'] ??
              (json['receipt'] is Map
                  ? (json['receipt'] as Map)['client_tool_handoff']
                  : null),
        ),
      );

  /// A copy with the work-run fields replaced (after a refresh or cancel).
  ChatResponseMetadata withWork({
    String? workRunId,
    String? workStatus,
    String? status,
    ChatRefusal? refusal,
  }) =>
      ChatResponseMetadata(
        completionId: completionId,
        requestId: requestId,
        model: model,
        tier: tier,
        finishReason: finishReason,
        status: status ?? this.status,
        cache: cache,
        elapsedMs: elapsedMs,
        promptTokens: promptTokens,
        completionTokens: completionTokens,
        totalTokens: totalTokens,
        modelCalls: modelCalls,
        toolCalls: toolCalls,
        workRunId: workRunId ?? this.workRunId,
        workStatus: workStatus ?? this.workStatus,
        acknowledgement: acknowledgement,
        refusal: refusal ?? this.refusal,
        overflow: overflow,
        agentLane: agentLane,
        orchestration: orchestration,
        clientToolHandoffs: clientToolHandoffs,
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
      acknowledgement.isEmpty &&
      refusal == null &&
      overflow == null &&
      agentLane == null &&
      orchestration == null &&
      clientToolHandoffs.isEmpty;

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
        if (acknowledgement.isNotEmpty) 'acknowledgement': acknowledgement,
        if (refusal != null) 'refusal': refusal!.toJson(),
        if (overflow != null) 'overflow': overflow!.toJson(),
        if (agentLane != null) 'agent_lane': agentLane!.toJson(),
        if (orchestration != null) 'orchestration': orchestration!.toJson(),
        if (clientToolHandoffs.isNotEmpty)
          'client_tool_handoffs':
              clientToolHandoffs.map((h) => h.toJson()).toList(),
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
    if (overflow != null) lines.add(overflow!.notice);
    return lines.join('\n');
  }
}

class AgentLaneReceipt {
  final String laneId;
  final String folder;
  final String status;
  const AgentLaneReceipt({
    required this.laneId,
    required this.folder,
    this.status = '',
  });
  factory AgentLaneReceipt.fromJson(Map<String, dynamic> json) =>
      AgentLaneReceipt(
        laneId: _boundedMetadataText(json['lane_id'], 128),
        folder: _boundedMetadataText(json['folder'], 1024),
        status: _boundedMetadataText(json['status'], 32),
      );
  Map<String, Object> toJson() => {
        'lane_id': laneId,
        'folder': folder,
        if (status.isNotEmpty) 'status': status,
      };
}

class OrchestrationChoice {
  final String label;
  final String command;
  const OrchestrationChoice({required this.label, required this.command});
  factory OrchestrationChoice.fromJson(Map<String, dynamic> json) =>
      OrchestrationChoice(
        label: _boundedMetadataText(json['label'], 128),
        // A command is actionable data. Never turn a truncated task into a
        // different request; omit oversized actions instead.
        command: json['command'] is String && (json['command'] as String).length <= 32768
            ? json['command'] as String : '',
      );
  Map<String, Object> toJson() => {'label': label, 'command': command};
}

class OrchestrationReceipt {
  final String task;
  final int workerSlots;
  final int fleetAgents;
  final int delegateAgents;
  final List<OrchestrationChoice> choices;
  const OrchestrationReceipt({
    this.task = '',
    this.workerSlots = 0,
    this.fleetAgents = 0,
    this.delegateAgents = 0,
    this.choices = const [],
  });
  factory OrchestrationReceipt.fromJson(Map<String, dynamic> json) =>
      OrchestrationReceipt(
        task: _boundedMetadataText(json['task'], 1024),
        workerSlots: _metadataCount(json['worker_slots']),
        fleetAgents: _metadataCount(json['fleet_agents']),
        delegateAgents: _metadataCount(json['delegate_agents']),
        choices: (json['choices'] as List? ?? const [])
            .whereType<Map>()
            .map(
              (e) => OrchestrationChoice.fromJson(Map<String, dynamic>.from(e)),
            )
            .where((e) => e.command.isNotEmpty)
            .take(8)
            .toList(growable: false),
      );
  Map<String, Object> toJson() => {
        'task': task,
        'worker_slots': workerSlots,
        'fleet_agents': fleetAgents,
        'delegate_agents': delegateAgents,
        'choices': choices.map((e) => e.toJson()).toList(),
      };
}

/// The long-context overflow decision the server made for one turn.
///
/// Read from `sonder_receipt.overflow`: [status] is `switched` (the turn ran
/// on the overflow model) or `unavailable` (it stayed on its route), and
/// [notice] is the server's one-line statement, rendered as sent.
class RouteOverflow {
  final String status;
  final String notice;

  const RouteOverflow({required this.status, required this.notice});

  bool get switched => status == 'switched';

  /// Null unless [value] is a receipt entry with a status and a notice.
  static RouteOverflow? fromJson(Object? value) {
    if (value is! Map) return null;
    final status = _boundedMetadataText(value['status'], 32);
    final notice = _boundedMetadataText(value['notice'], 300);
    if (status.isEmpty || notice.isEmpty) return null;
    return RouteOverflow(status: status, notice: notice);
  }

  Map<String, Object> toJson() => {'status': status, 'notice': notice};
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
  final String risk;

  /// The permission mode that refused the call, as the server reported it.
  final String mode;
  final String modeLabel;
  final List<String> remedies;

  const ChatRefusal({
    this.callId = '',
    this.tool = '',
    this.reason = '',
    this.risk = '',
    this.mode = '',
    this.modeLabel = '',
    this.remedies = const [],
  });

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
      risk: _boundedMetadataText(json['risk'], 32),
      mode: _boundedMetadataText(json['mode'], 32),
      modeLabel: _boundedMetadataText(json['mode_label'], 32),
      remedies: remedies,
    );
  }

  /// Text fallback for servers without S1: the reply starts with `refused`.
  /// Returns null for any other reply.
  ///
  /// Reply text is model-authored, so it never carries approval authority:
  /// a `/approve <id>` inside it is ignored and [callId] stays empty. Only
  /// the server's structured `sonder_receipt.refusal` can offer "Approve
  /// this call once".
  static ChatRefusal? fromText(String text) {
    if (!_refusedPrefix.hasMatch(text)) return null;
    final firstLine = text.trim().split('\n').first;
    return ChatRefusal(reason: _boundedMetadataText(firstLine, 1024));
  }

  Map<String, Object> toJson() => {
        if (callId.isNotEmpty) 'call_id': callId,
        if (tool.isNotEmpty) 'tool': tool,
        if (reason.isNotEmpty) 'reason': reason,
        if (risk.isNotEmpty) 'risk': risk,
        if (mode.isNotEmpty) 'mode': mode,
        if (modeLabel.isNotEmpty) 'mode_label': modeLabel,
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

  /// The last timestamp [ChatThread.fresh] issued as an id.
  static int _lastIssuedMicros = 0;

  /// A `chat-<microseconds>` id that is unique within this process.
  ///
  /// The wall clock's resolution is far coarser than a microsecond on some
  /// platforms (Windows advances it in ticks of a millisecond or more), so
  /// two threads created in the same tick -- the startup thread and an
  /// immediate New chat -- would otherwise share one id and one transcript.
  static String _uniqueId(DateTime now) {
    var micros = now.microsecondsSinceEpoch;
    if (micros <= _lastIssuedMicros) micros = _lastIssuedMicros + 1;
    _lastIssuedMicros = micros;
    return 'chat-$micros';
  }

  factory ChatThread.fresh({String project = 'default'}) {
    final now = DateTime.now();
    return ChatThread(
      id: _uniqueId(now),
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

/// Role-based feature flags advertised by `/v1/sonder/capabilities`,
/// `/v1/sonder/whoami`, `/v1/sonder/status`, `/v1/sonder/login`, and
/// `/v1/sonder/register`.
class AllowedFeatures {
  final bool chat;
  final bool registration;
  final bool clientExec;
  final bool hostTools;
  final bool hostBypass;
  final bool hostAdmin;

  const AllowedFeatures({
    this.chat = true,
    this.registration = false,
    this.clientExec = true,
    this.hostTools = true,
    this.hostBypass = false,
    this.hostAdmin = true,
  });

  factory AllowedFeatures.fromJson(Object? raw) {
    if (raw is! Map) return const AllowedFeatures();
    final map = Map<String, dynamic>.from(raw);
    return AllowedFeatures(
      chat: map['chat'] != false,
      registration: map['registration'] == true,
      clientExec: map['client_exec'] == true,
      hostTools: map['host_tools'] == true,
      hostBypass: map['host_bypass'] == true,
      hostAdmin: map['host_admin'] == true,
    );
  }
}

/// Server discovery & policy capabilities (`GET /v1/sonder/capabilities`).
class ServerCapabilities {
  final String authMode;
  final bool publicHosting;
  final bool registrationEnabled;
  final bool requireAccount;
  final bool clientExecSupported;
  final String clientExecHeader;
  final String clientResultEndpoint;
  final List<String> clientDelegatableTools;
  final bool clientOwnsToolPolicy;
  final bool hostBypassAllowed;
  final AllowedFeatures allowedFeatures;
  final String role;
  final String username;

  const ServerCapabilities({
    this.authMode = 'local-open',
    this.publicHosting = false,
    this.registrationEnabled = false,
    this.requireAccount = false,
    this.clientExecSupported = false,
    this.clientExecHeader = 'X-Sonder-Client-Exec',
    this.clientResultEndpoint = '/v1/client-tools/{call_id}/result',
    this.clientDelegatableTools = const [],
    this.clientOwnsToolPolicy = true,
    this.hostBypassAllowed = false,
    this.allowedFeatures = const AllowedFeatures(),
    this.role = '',
    this.username = '',
  });

  factory ServerCapabilities.fromJson(Map<String, dynamic> json) {
    final tools = (json['client_delegatable_tools'] as List? ?? const [])
        .map((e) => _boundedMetadataText(e, 64))
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
    return ServerCapabilities(
      authMode: _boundedMetadataText(json['auth_mode'], 32).isEmpty
          ? 'local-open'
          : _boundedMetadataText(json['auth_mode'], 32),
      publicHosting: json['public_hosting'] == true,
      registrationEnabled: json['registration_enabled'] == true,
      requireAccount: json['require_account'] == true,
      clientExecSupported: json['client_exec_supported'] == true,
      clientExecHeader:
          _boundedMetadataText(json['client_exec_header'], 64).isEmpty
              ? 'X-Sonder-Client-Exec'
              : _boundedMetadataText(json['client_exec_header'], 64),
      clientResultEndpoint:
          _boundedMetadataText(json['client_result_endpoint'], 128).isEmpty
              ? '/v1/client-tools/{call_id}/result'
              : _boundedMetadataText(json['client_result_endpoint'], 128),
      clientDelegatableTools: List.unmodifiable(tools),
      clientOwnsToolPolicy: json['client_owns_tool_policy'] != false,
      hostBypassAllowed: json['host_bypass_allowed'] == true,
      allowedFeatures: AllowedFeatures.fromJson(json['allowed_features']),
      role: _boundedMetadataText(json['role'], 32).toLowerCase(),
      username: _boundedMetadataText(json['username'], 128),
    );
  }
}

/// Authenticated principal identity & permissions (`GET /v1/sonder/whoami`).
class WhoamiInfo {
  final bool ok;
  final String username;
  final String role;
  final String authMode;
  final bool publicHosting;
  final bool clientExecSupported;
  final bool hostBypassAllowed;
  final AllowedFeatures allowedFeatures;

  const WhoamiInfo({
    this.ok = true,
    this.username = '',
    this.role = '',
    this.authMode = '',
    this.publicHosting = false,
    this.clientExecSupported = false,
    this.hostBypassAllowed = false,
    this.allowedFeatures = const AllowedFeatures(),
  });

  bool get isRestrictedUser =>
      role == 'user' || (publicHosting && !allowedFeatures.hostAdmin);

  factory WhoamiInfo.fromJson(Map<String, dynamic> json) {
    final account = json['account'] is Map
        ? Map<String, dynamic>.from(json['account'] as Map)
        : const <String, dynamic>{};
    final rawName = json['username'] ?? account['username'];
    final rawRole = json['role'] ?? account['role'];
    return WhoamiInfo(
      ok: json['ok'] != false,
      username: _boundedMetadataText(rawName, 128),
      role: _boundedMetadataText(rawRole, 32).toLowerCase(),
      authMode: _boundedMetadataText(json['auth_mode'], 32),
      publicHosting: json['public_hosting'] == true,
      clientExecSupported: json['client_exec_supported'] == true,
      hostBypassAllowed: json['host_bypass_allowed'] == true,
      allowedFeatures: AllowedFeatures.fromJson(json['allowed_features']),
    );
  }
}

/// Non-stream handoff metadata emitted in `sonder_receipt.client_tool_handoff`.
class ClientToolHandoff {
  final String executionTarget;
  final String phase;
  final String callId;
  final String tool;
  final Map<String, dynamic> args;
  final String risk;
  final String resultEndpoint;
  final int expiresAt;
  final String session;

  const ClientToolHandoff({
    this.executionTarget = 'client',
    this.phase = 'awaiting_client_result',
    required this.callId,
    required this.tool,
    this.args = const {},
    this.risk = 'execute',
    this.resultEndpoint = '',
    this.expiresAt = 0,
    this.session = '',
  });

  static List<ClientToolHandoff> listFromJson(Object? raw) {
    if (raw is Map) {
      final parsed = ClientToolHandoff.fromJson(Map<String, dynamic>.from(raw));
      return parsed.callId.isNotEmpty && parsed.tool.isNotEmpty
          ? [parsed]
          : const [];
    }
    if (raw is List) {
      final items = <ClientToolHandoff>[];
      for (final item in raw) {
        if (item is Map) {
          final parsed =
              ClientToolHandoff.fromJson(Map<String, dynamic>.from(item));
          if (parsed.callId.isNotEmpty && parsed.tool.isNotEmpty) {
            items.add(parsed);
          }
        }
      }
      return List.unmodifiable(items);
    }
    return const [];
  }

  factory ClientToolHandoff.fromJson(Map<String, dynamic> json) {
    final rawCallId = json['call_id']?.toString().trim().toLowerCase() ?? '';
    final callId =
        RegExp(r'^[0-9a-f]{8,64}$').hasMatch(rawCallId) ? rawCallId : '';
    final rawArgs = json['args'];
    final args = rawArgs is Map
        ? Map<String, dynamic>.from(rawArgs)
        : const <String, dynamic>{};
    return ClientToolHandoff(
      executionTarget: _boundedMetadataText(json['execution_target'], 32),
      phase: _boundedMetadataText(json['phase'], 48),
      callId: callId,
      tool: _boundedMetadataText(json['tool'], 128),
      args: Map.unmodifiable(args),
      risk: _boundedMetadataText(json['risk'], 32).isEmpty
          ? 'execute'
          : _boundedMetadataText(json['risk'], 32),
      resultEndpoint: _boundedMetadataText(json['result_endpoint'], 160),
      expiresAt: _metadataCount(json['expires_at']),
      session: _boundedMetadataText(json['session'], 128),
    );
  }

  Map<String, Object> toJson() => {
        'execution_target': executionTarget,
        'phase': phase,
        'call_id': callId,
        'tool': tool,
        'args': args,
        'risk': risk,
        if (resultEndpoint.isNotEmpty) 'result_endpoint': resultEndpoint,
        if (expiresAt > 0) 'expires_at': expiresAt,
        if (session.isNotEmpty) 'session': session,
      };
}
