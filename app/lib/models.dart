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
