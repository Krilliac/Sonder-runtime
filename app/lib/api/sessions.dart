/// Durable server sessions (plan P1-7): replay a thread started elsewhere,
/// export it, and (once server S7 lands) list sessions.
///
/// * `GET /v1/sessions/<id>/replay`  schema `sonder.http-session-replay.v1`
/// * `GET /v1/sessions/<id>/export`  schema `sonder.http-session-export.v1`
/// * `GET /v1/sessions?limit&after`  server S7; 404 on older servers
///
/// All session routes are administrator-only on the server.
library;

import 'dart:convert';

import '../models.dart';
import 'transport.dart';

/// One transcript entry of a replayed session.
class ReplayMessage {
  final String role;
  final String content;
  final int sequence;
  final String turnId;

  const ReplayMessage({
    required this.role,
    required this.content,
    this.sequence = 0,
    this.turnId = '',
  });

  factory ReplayMessage.fromJson(Map<String, dynamic> json) => ReplayMessage(
        role: boundedResponseMetadata(json['role'], 16),
        content: json['content'] is String ? json['content'] as String : '',
        sequence:
            json['sequence'] is num ? (json['sequence'] as num).round() : 0,
        turnId: boundedResponseMetadata(json['turn_id'], 64),
      );

  /// A local [ChatMessage], or null for roles the chat does not show.
  ChatMessage? toChatMessage() => switch (role) {
        'user' => ChatMessage(role: Role.user, content: content),
        'assistant' => ChatMessage(role: Role.assistant, content: content),
        _ => null,
      };
}

/// `GET /v1/sessions/<id>/replay`.
class SessionReplay {
  final String sessionId;
  final bool integrityValid;
  final bool crashSafe;
  final List<ReplayMessage> transcript;

  const SessionReplay({
    required this.sessionId,
    this.integrityValid = false,
    this.crashSafe = false,
    this.transcript = const [],
  });

  factory SessionReplay.fromJson(Map<String, dynamic> json) {
    if (json['schema'] != 'sonder.http-session-replay.v1') {
      throw SonderException('This server sent an unknown replay format.');
    }
    final rows = json['transcript'] is List
        ? (json['transcript'] as List).whereType<Map>()
        : const <Map>[];
    return SessionReplay(
      sessionId: boundedResponseMetadata(json['session_id'], 256),
      integrityValid: json['integrity_valid'] == true,
      crashSafe: json['crash_safe'] == true,
      transcript: rows
          .map((r) => ReplayMessage.fromJson(Map<String, dynamic>.from(r)))
          .toList(growable: false),
    );
  }

  /// The user/assistant turns as local chat messages.
  List<ChatMessage> get chatMessages => transcript
      .map((m) => m.toChatMessage())
      .whereType<ChatMessage>()
      .toList(growable: false);
}

/// `GET /v1/sessions/<id>/export`: the raw JSON document and a file name.
class SessionExport {
  final String fileName;
  final String json;

  const SessionExport({required this.fileName, required this.json});
}

/// One row of `GET /v1/sessions` (server S7).
class SessionSummary {
  final String id;
  final String title;
  final int turns;
  final DateTime? updatedAt;

  const SessionSummary(
      {required this.id, this.title = '', this.turns = 0, this.updatedAt});

  factory SessionSummary.fromJson(Map<String, dynamic> json) {
    final updated = json['updated'] ?? json['updated_at'];
    final n = updated is num ? updated.toDouble() : double.tryParse('$updated');
    return SessionSummary(
      id: boundedResponseMetadata(json['id'], 256),
      title: boundedResponseMetadata(json['title'], 256),
      turns: json['turns'] is num ? (json['turns'] as num).round() : 0,
      updatedAt: n != null && n > 0
          ? DateTime.fromMillisecondsSinceEpoch((n * 1000).round())
          : (updated is String ? DateTime.tryParse(updated) : null),
    );
  }
}

/// A page of [SessionSummary] rows.
class SessionPage {
  final List<SessionSummary> sessions;
  final String next;
  const SessionPage({this.sessions = const [], this.next = ''});
}

/// Client for the session routes.
class SessionsApi {
  final SonderEndpoint endpoint;
  final Duration timeout;

  const SessionsApi(this.endpoint,
      {this.timeout = const Duration(seconds: 30)});

  Future<(int, Map<String, dynamic>?, String)> _get(Uri uri) async {
    final response = await () async {
      try {
        return await requestGet(uri,
            headers: endpoint.headers(), timeout: timeout);
      } catch (error) {
        throw SonderException.transport(error, endpoint.baseUrl);
      }
    }();
    if (response.statusCode == 404) return (404, null, '');
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw describeServerError(
        responseException(response, httpStatusFallback(response.statusCode)),
        endpoint.serverUri,
        action: 'open server sessions',
      );
    }
    final text = utf8.decode(response.bodyBytes, allowMalformed: true);
    return (
      response.statusCode,
      decodeJsonObject(response, 'the session'),
      text
    );
  }

  String _id(String sessionId) {
    final id = sessionId.trim();
    if (id.isEmpty || id.contains('/') || id.contains('\\')) {
      throw SonderException('That is not a session id.', code: 'INVALID_ID');
    }
    return Uri.encodeComponent(id);
  }

  /// Replay a session's redacted transcript, or null when it has none.
  Future<SessionReplay?> replay(String sessionId) async {
    final (_, body, _) =
        await _get(endpoint.uri('/v1/sessions/${_id(sessionId)}/replay'));
    return body == null ? null : SessionReplay.fromJson(body);
  }

  /// Export a session as the server's JSON document.
  Future<SessionExport?> export(String sessionId) async {
    final (_, body, text) =
        await _get(endpoint.uri('/v1/sessions/${_id(sessionId)}/export'));
    if (body == null) return null;
    final safe = sessionId.trim().replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    return SessionExport(fileName: 'sonder-session-$safe.json', json: text);
  }

  /// List sessions (server S7). Null when the server has no list route.
  Future<SessionPage?> list({int limit = 50, String after = ''}) async {
    final uri = endpoint.uri('/v1/sessions').replace(queryParameters: {
      'limit': '$limit',
      if (after.isNotEmpty) 'after': after,
    });
    final (_, body, _) = await _get(uri);
    if (body == null) return null;
    final rows = body['sessions'] ?? body['data'];
    return SessionPage(
      sessions: rows is List
          ? rows
              .whereType<Map>()
              .map((r) => SessionSummary.fromJson(Map<String, dynamic>.from(r)))
              .where((s) => s.id.isNotEmpty)
              .toList(growable: false)
          : const [],
      next: boundedResponseMetadata(body['next'] ?? body['next_cursor'], 256),
    );
  }
}
