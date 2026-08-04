/// Core data model for a single chat turn.
enum Role { user, assistant, system }

class ChatMessage {
  final Role role;
  final String content;
  final bool pending; // true while the assistant reply is in-flight
  final bool error;

  const ChatMessage({
    required this.role,
    required this.content,
    this.pending = false,
    this.error = false,
  });

  ChatMessage copyWith({String? content, bool? pending, bool? error}) {
    return ChatMessage(
      role: role,
      content: content ?? this.content,
      pending: pending ?? this.pending,
      error: error ?? this.error,
    );
  }

  /// Wire format for the OpenAI-compatible /v1/chat/completions endpoint.
  Map<String, String> toWire() => {
        'role': role.name,
        'content': content,
      };

  Map<String, Object> toJson() => {
        'role': role.name,
        'content': content,
        'pending': pending,
        'error': error,
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
            json['active_release'] as Map<String, dynamic>?),
        previousRelease: UpdateRelease.fromJson(
            json['previous_release'] as Map<String, dynamic>?),
        plans: ((json['plans'] as List<dynamic>?) ?? [])
            .whereType<Map<String, dynamic>>()
            .map(UpdatePlan.fromJson)
            .toList(),
      );

  bool get canRollback => previousRelease != null;
}
