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
}
