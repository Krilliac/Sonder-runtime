/// HTTP approvals (plan P1-2; server S2):
///
/// * `GET  /v1/approvals`                 pending refused calls + open approvals
/// * `POST /v1/approvals/<call_id>`       `{ttl_seconds}` approve one call once
/// * `POST /v1/approvals/revoke/<nonce>`  revoke an open approval
///
/// A server without S2 answers 404. [ApprovalsApi.list] then returns null and
/// the mutations throw [SonderException] with code
/// [ApprovalsApi.unavailableCode], whose message is the console fallback
/// (`/approve <call id>`), so the sheet can show it with a Copy action.
library;

import 'transport.dart';

final RegExp _callId = RegExp(r'^[0-9a-f]{8,64}$');
final RegExp _nonce = RegExp(r'^[A-Za-z0-9_.:-]{4,128}$');

DateTime? _ts(Object? value) {
  final n = value is num ? value.toDouble() : double.tryParse('$value');
  if (n != null && n > 0) {
    return DateTime.fromMillisecondsSinceEpoch((n * 1000).round());
  }
  if (value is String) return DateTime.tryParse(value);
  return null;
}

/// A call a permission gate refused because nobody could be asked.
class PendingApproval {
  final String callId;
  final String tool;

  /// Redacted argument preview, as the server renders it.
  final String preview;
  final String mode;
  final DateTime? refusedAt;

  const PendingApproval({
    required this.callId,
    this.tool = '',
    this.preview = '',
    this.mode = '',
    this.refusedAt,
  });

  factory PendingApproval.fromJson(Map<String, dynamic> json) =>
      PendingApproval(
        callId: boundedResponseMetadata(json['call_id'], 64),
        tool: boundedResponseMetadata(json['tool'], 128),
        preview: boundedResponseMetadata(json['preview'], 2048),
        mode: boundedResponseMetadata(json['mode'], 32),
        refusedAt: _ts(json['refused_at'] ?? json['created_at']),
      );
}

/// An approval issued for one exact call, spendable once until [expiresAt].
class IssuedApproval {
  final String nonce;
  final String callId;
  final String tool;
  final DateTime? expiresAt;
  final int ttlSeconds;

  const IssuedApproval({
    required this.nonce,
    this.callId = '',
    this.tool = '',
    this.expiresAt,
    this.ttlSeconds = 0,
  });

  factory IssuedApproval.fromJson(Map<String, dynamic> json) {
    final inner = json['approval'] is Map
        ? Map<String, dynamic>.from(json['approval'] as Map)
        : json;
    final ttl = inner['ttl_seconds'];
    return IssuedApproval(
      nonce: boundedResponseMetadata(inner['nonce'], 128),
      callId: boundedResponseMetadata(inner['call_id'], 64),
      tool: boundedResponseMetadata(inner['tool'], 128),
      expiresAt: _ts(inner['expires_at']),
      ttlSeconds: ttl is num ? ttl.round() : int.tryParse('$ttl') ?? 0,
    );
  }
}

/// `GET /v1/approvals`.
class ApprovalsSnapshot {
  final List<PendingApproval> pending;
  final List<IssuedApproval> open;

  const ApprovalsSnapshot({this.pending = const [], this.open = const []});

  factory ApprovalsSnapshot.fromJson(Map<String, dynamic> json) {
    List<Map<String, dynamic>> rows(Object? v) => v is List
        ? v.whereType<Map>().map(Map<String, dynamic>.from).toList()
        : const [];
    return ApprovalsSnapshot(
      pending: rows(json['pending'])
          .map(PendingApproval.fromJson)
          .where((p) => p.callId.isNotEmpty)
          .toList(growable: false),
      open: rows(json['approvals'] ?? json['open'])
          .map(IssuedApproval.fromJson)
          .where((a) => a.nonce.isNotEmpty)
          .toList(growable: false),
    );
  }
}

/// Client for the approval routes.
class ApprovalsApi {
  final SonderEndpoint endpoint;
  final Duration timeout;

  const ApprovalsApi(this.endpoint,
      {this.timeout = const Duration(seconds: 20)});

  static const unavailableCode = 'APPROVALS_UNAVAILABLE';

  /// The console instruction for [callId] when the server has no S2 route.
  static String consoleFallback(String callId) =>
      'Approve from the console: `/approve $callId`';

  Future<Map<String, dynamic>?> _call(String method, String path,
      {Map<String, String>? extraHeaders, String body = '{}'}) async {
    final uri = endpoint.uri(path);
    final headers = {...endpoint.headers(), ...?extraHeaders};
    final response = await () async {
      try {
        return method == 'GET'
            ? await requestGet(uri, headers: headers, timeout: timeout)
            : await requestPost(uri,
                headers: headers, body: body, timeout: timeout);
      } catch (error) {
        throw SonderException.transport(error, endpoint.baseUrl);
      }
    }();
    if (response.statusCode == 404 || response.statusCode == 405) return null;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final error =
          responseException(response, httpStatusFallback(response.statusCode));
      if (response.statusCode == 403) {
        throw error.copyWith(
          message: 'Approvals need a developer or admin account.',
          remedy: 'Sign in with a developer or admin account.',
        );
      }
      throw describeServerError(error, endpoint.serverUri);
    }
    return decodeJsonObject(response, 'the approval response');
  }

  /// Pending calls and open approvals, or null when the server has no
  /// approvals route (approve from the console instead).
  Future<ApprovalsSnapshot?> list() async {
    final body = await _call('GET', '/v1/approvals');
    return body == null ? null : ApprovalsSnapshot.fromJson(body);
  }

  /// Approve exactly [callId] once, valid for [ttl]. Sends one POST with a
  /// fresh Idempotency-Key; never retried automatically.
  Future<IssuedApproval> approve(String callId,
      {Duration ttl = const Duration(minutes: 15)}) async {
    if (!_callId.hasMatch(callId)) {
      throw SonderException('That is not a call id.', code: 'INVALID_ID');
    }
    final body = await _call(
      'POST',
      '/v1/approvals/$callId',
      extraHeaders: {'Idempotency-Key': newIdempotencyKey('approve')},
      body: '{"ttl_seconds": ${ttl.inSeconds}}',
    );
    if (body == null) {
      throw SonderException(consoleFallback(callId),
          code: unavailableCode, httpStatus: 404);
    }
    return IssuedApproval.fromJson(body);
  }

  /// Revoke an open approval by its nonce.
  Future<void> revoke(String nonce) async {
    if (!_nonce.hasMatch(nonce)) {
      throw SonderException('That is not an approval nonce.',
          code: 'INVALID_ID');
    }
    final body = await _call(
      'POST',
      '/v1/approvals/revoke/$nonce',
      extraHeaders: {'Idempotency-Key': newIdempotencyKey('revoke')},
    );
    if (body == null) {
      throw SonderException('Revoke from the console: `/approve revoke $nonce`',
          code: unavailableCode, httpStatus: 404);
    }
  }
}
